"""统一评分的离线规则、证据和安全边界测试。"""
import copy
import importlib.util
import json
import math
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from esa.score import evaluate, score_reply, score_tools


def call(name="lookup", arguments=None, status="success", **extra):
    return {"name": name, "arguments": arguments or {}, "status": status, **extra}


def evidence(calls=None, events=None, reply="完成", **extra):
    return {"calls": calls or [], "events": events or [], "truncated": False,
            "reply": reply, "initial": {"state": {}}, "final": {"state": {}},
            "dialogue": [], **extra}


class ToolTests(unittest.TestCase):
    def test_any_tool_requires_correct_parameters_and_success(self):
        rubric = {"weights": {"tool_use": 1}, "tool_checks": [{"any_tool": ["lookup", "search"], "arguments_contain": ["company"]}]}
        for calls, expected in [([call(arguments={"kind": "home"})], False),
                                ([call(arguments={"kind": "company"}, status="error")], False),
                                ([call(arguments={"kind": "home"}), call("search", {"kind": "company"})], True)]:
            with self.subTest(calls=calls):
                result = evaluate(rubric, evidence(calls))
                self.assertTrue(result["valid"])
                self.assertEqual(expected, result["passed"])

    def test_structured_argument_subset(self):
        rubric = {"weights": {"tool_use": 1}, "tool_checks": [{"tool": "lookup", "arguments_match": {"filter": {"kind": "company"}}}]}
        self.assertTrue(evaluate(rubric, evidence([call(arguments={"filter": {"kind": "company", "extra": 2}})]))["passed"])
        self.assertFalse(evaluate(rubric, evidence([call(arguments={"kind": "company"})]))["passed"])

    def test_optional_checks_do_not_reduce_required_score(self):
        rubric = {"weights": {"tool_use": 1}, "tool_checks": [{"tool": "lookup"}, {"tool": "optional", "required": False}]}
        result = evaluate(rubric, evidence([call()]))
        self.assertTrue(result["passed"])
        self.assertEqual(1, result["rewards"]["reward"])
        self.assertFalse(result["detail"]["tool_use"]["optional"][0]["passed"])

    def test_zero_weight_does_not_disable_required_check(self):
        rubric = {"weights": {"tool_use": 0, "reply": 1}, "tool_checks": [{"tool": "lookup"}]}
        result = evaluate(rubric, evidence())
        self.assertFalse(result["passed"])
        self.assertTrue(result["valid"])

    def test_unknown_status_does_not_satisfy_required(self):
        rubric = {"weights": {"tool_use": 1}, "tool_checks": [{"tool": "lookup"}]}
        for status in (None, "pending", "failed", "error", "response_lost"):
            with self.subTest(status=status):
                self.assertFalse(evaluate(rubric, evidence([call(status=status)]))["passed"])
        for status in ("success", "ok", "completed"):
            with self.subTest(status=status):
                self.assertTrue(evaluate(rubric, evidence([call(status=status)]))["passed"])

    def test_error_output_overrides_success(self):
        rubric = {"weights": {"tool_use": 1}, "tool_checks": [{"tool": "lookup"}]}
        self.assertFalse(evaluate(rubric, evidence([call(output={"isError": True})]))["passed"])
        self.assertFalse(evaluate(rubric, evidence([call(output=json.dumps({"result": {"isError": True}}))]))["passed"])
        self.assertTrue(evaluate(rubric, evidence([call(status=None, output={"isError": False, "structuredContent": {}})]))["passed"])

    def test_event_only_evidence_uses_full_arguments(self):
        rubric = {"weights": {"tool_use": 1}, "tool_checks": [{"tool": "lookup", "arguments_contain": ["company"]}]}
        result = evaluate(rubric, evidence(events=[{"tool": "lookup", "status": "ok", "arguments": {"kind": "company"}}]))
        self.assertTrue(result["passed"])

    def test_before_check_allows_call_after_successful_matching_check(self):
        rubric = {"weights": {"tool_use": 1}, "tool_checks": [{"id": "found", "tool": "lookup", "arguments_match": {"kind": "company"}}],
                  "tool_forbidden": [{"tool": "start", "before_check": "found"}]}
        good = [call(arguments={"kind": "home"}), call(arguments={"kind": "company"}), call("start")]
        bad = [call(arguments={"kind": "home"}), call("start"), call(arguments={"kind": "company"})]
        self.assertTrue(evaluate(rubric, evidence(good))["passed"])
        result = evaluate(rubric, evidence(bad))
        self.assertFalse(result["passed"])
        self.assertEqual(0, result["rewards"]["reward"])
        self.assertTrue(result["detail"]["hard_violations"])
        failed_lookup = [call(arguments={"kind": "company"}, status="error"), call("start")]
        self.assertEqual(0, evaluate(rubric, evidence(failed_lookup))["rewards"]["reward"])

    def test_before_turn_from_calls(self):
        rubric = {"weights": {"reply": 1}, "tool_forbidden": [{"tool": "control", "before_turn": 1}]}
        self.assertTrue(evaluate(rubric, evidence([call("control", turn_index=1)]))["passed"])
        self.assertEqual(0, evaluate(rubric, evidence([call("control", turn_index=0)]))["rewards"]["reward"])
        self.assertFalse(evaluate(rubric, evidence([call("control")]))["valid"])

    def test_before_turn_from_round_events(self):
        rubric = {"weights": {"reply": 1}, "tool_forbidden": [{"tool": "control", "before_turn": 1}]}
        events = [{"tool": "session/round", "status": "ok", "sequence": 1},
                  {"tool": "session/round", "status": "ok", "sequence": 2},
                  {"tool": "control", "status": "ok", "call_id": "c", "arguments": {}, "sequence": 3}]
        self.assertTrue(evaluate(rubric, evidence([call("control", call_id="c")], events))["passed"])
        events.pop(1)
        self.assertFalse(evaluate(rubric, evidence([call("control", call_id="c")], events))["passed"])

    def test_failed_forbidden_attempt_is_still_prohibited(self):
        rubric = {"weights": {"reply": 1}, "tool_forbidden": [{"tool": "control"}]}
        self.assertEqual(0, evaluate(rubric, evidence([call("control", status="error")]))["rewards"]["reward"])

    def test_forbidden_parameters(self):
        rubric = {"weights": {"reply": 1}, "tool_forbidden": [{"tool": "control", "arguments_contain_any": ["window", "door"]}]}
        self.assertTrue(evaluate(rubric, evidence([call("control", {"device": "temperature"})]))["passed"])
        self.assertFalse(evaluate(rubric, evidence([call("control", {"device": "door"})]))["passed"])

    def test_unknown_timing_and_reference_rejected(self):
        rules = [{"tool": "control", "before_chek": "found"},
                 {"tool": "control", "before_check": "missing"},
                 {"tool": "control", "before_turn": "1"},
                 {"tool": "control", "before_turn": 1, "before_check": "found"}]
        for rule in rules:
            with self.subTest(rule=rule):
                result = evaluate({"weights": {"reply": 1}, "tool_forbidden": [rule]}, evidence())
                self.assertFalse(result["valid"])
                self.assertFalse(result["passed"])
                self.assertTrue(result["detail"]["validation_errors"])

    def test_duplicate_check_ids_rejected(self):
        result = evaluate({"weights": {"tool_use": 1}, "tool_checks": [{"id": "same", "tool": "a"}, {"id": "same", "tool": "b"}]}, evidence())
        self.assertFalse(result["valid"])

    def test_truncated_tool_history_is_invalid(self):
        result = evaluate({"weights": {"tool_use": 1}, "tool_checks": [{"tool": "lookup"}]}, evidence([call()], truncated=True))
        self.assertFalse(result["valid"])
        self.assertEqual(0, result["rewards"]["reward"])


class ReplyTests(unittest.TestCase):
    def test_turn_filter_excludes_user_and_other_assistant_turn(self):
        rubric = {"weights": {"reply": 1}, "reply_checks": [{"pattern": "确认", "turn_index": 0}]}
        dialogue = [{"role": "user", "content": "确认", "turn_index": 0},
                    {"role": "assistant", "content": "完成", "turn_index": 0},
                    {"role": "assistant", "content": "确认", "turn_index": 1}]
        self.assertFalse(evaluate(rubric, evidence(reply="确认", dialogue=dialogue))["passed"])
        dialogue[1]["content"] = "请确认"
        self.assertTrue(evaluate(rubric, evidence(dialogue=dialogue))["passed"])

    def test_missing_dialogue_for_scoped_reply_is_invalid(self):
        result = evaluate({"weights": {"reply": 1}, "reply_checks": [{"pattern": "完成", "turn_index": 0}]}, evidence())
        self.assertFalse(result["valid"])

    def test_missing_selected_turn_fails_even_negative_only(self):
        rubric = {"weights": {"reply": 1}, "reply_checks": [{"forbidden_pattern": "保证", "turn_index": 2}]}
        dialogue = [{"role": "assistant", "content": "完成", "turn_index": 0}]
        result = evaluate(rubric, evidence(dialogue=dialogue))
        self.assertFalse(result["passed"])

    def test_facts_pattern_and_forbidden_are_conjunctive(self):
        rubric = {"weights": {"reply": 1}, "reply_checks": [{"facts": ["上海", "24"], "pattern": "完成", "forbidden_pattern": "保证"}]}
        self.assertTrue(evaluate(rubric, evidence(reply="完成，上海24度"))["passed"])
        for reply in ("上海24度", "完成，上海", "保证完成，上海24度"):
            with self.subTest(reply=reply):
                self.assertFalse(evaluate(rubric, evidence(reply=reply))["passed"])

    def test_optional_reply_and_zero_weight(self):
        rubric = {"weights": {"tool_use": 1}, "reply_checks": [{"pattern": "完成", "required": False}], "tool_checks": [{"tool": "lookup"}]}
        self.assertTrue(evaluate(rubric, evidence([call()], reply=""))["passed"])
        rubric["reply_checks"][0]["required"] = True
        self.assertFalse(evaluate(rubric, evidence([call()], reply=""))["passed"])

    def test_min_matches_and_empty_reply(self):
        rubric = {"weights": {"reply": 1}, "reply_checks": [{"pattern": "候选", "min_matches": 2}]}
        self.assertTrue(evaluate(rubric, evidence(reply="候选A，候选B"))["passed"])
        self.assertFalse(evaluate(rubric, evidence(reply="候选A"))["passed"])
        self.assertFalse(evaluate({"weights": {"reply": 1}}, evidence(reply=""))["passed"])

    def test_invalid_reply_rule_and_dialogue(self):
        for check in ({"pattern": "["}, {"pattern": "完成", "before_turn": 1}, {"facts": "完成"}, {}):
            self.assertFalse(evaluate({"weights": {"reply": 1}, "reply_checks": [check]}, evidence())["valid"])
        for dialogue in ([{"role": "system", "content": "x", "turn_index": 0}],
                         [{"role": "assistant", "content": "x", "turn_index": -1}]):
            self.assertFalse(evaluate({"weights": {"reply": 1}}, evidence(dialogue=dialogue))["valid"])


class StateAndProcessTests(unittest.TestCase):
    def test_state_only_without_reply_or_judge(self):
        rubric = {"weights": {"state": 1}, "expected_state": {"navigation.active": True}}
        result = evaluate(rubric, evidence(reply="", final={"state": {"navigation": {"active": True}}}))
        self.assertTrue(result["passed"])
        self.assertEqual(1, result["rewards"]["reward"])

    def test_predicate_list_and_optional_state(self):
        rubric = {"weights": {"state": 1}, "expected_state": [{"path": "value", "op": "gte", "value": 2},
                                                              {"path": "optional", "required": False, "value": True}]}
        result = evaluate(rubric, evidence(final={"state": {"value": 3}}))
        self.assertTrue(result["passed"])
        self.assertEqual(1, result["rewards"]["reward"])

    def test_global_hard_violation_despite_zero_state_weight(self):
        rubric = {"weights": {"state": 0, "reply": 1}, "forbidden_changes": ["vehicle.volume"]}
        result = evaluate(rubric, evidence(events=[{"tool": "control", "status": "ok", "changes": [{"path": "/vehicle/volume"}]}]))
        self.assertTrue(result["valid"])
        self.assertFalse(result["passed"])
        self.assertEqual(0, result["rewards"]["reward"])
        self.assertEqual("state", result["detail"]["hard_violations"][0]["component"])

    def test_global_order_and_failed_call_constraints(self):
        rubric = {"weights": {"reply": 1}, "process_constraints": [{"type": "ordered_tools", "tools": ["lookup", "start"]}]}
        events = [{"tool": "start", "status": "ok"}, {"tool": "lookup", "status": "ok"}]
        self.assertEqual(0, evaluate(rubric, evidence(events=events))["rewards"]["reward"])
        rubric["process_constraints"] = [{"type": "no_failed_calls"}]
        self.assertEqual(0, evaluate(rubric, evidence([call(status="error")]))["rewards"]["reward"])

    def test_event_count_does_not_double_count_cached_calls(self):
        rubric = {"weights": {"state": 1}, "process_constraints": [{"type": "max_calls", "tool": "play", "count": 1}]}
        calls = [call("play", call_id="p"), call("play", call_id="p")]
        events = [{"tool": "play", "call_id": "p", "status": "ok", "arguments": {}, "sequence": 1}]
        self.assertTrue(evaluate(rubric, evidence(calls, events))["passed"])

    def test_recover_response_from_explicit_call_status(self):
        rubric = {"weights": {"state": 1}, "process_constraints": [{"type": "recover_response", "tool": "lookup", "mode": "retry"}]}
        loss = call(arguments={"query": "song"}, status="response_lost", call_id="one")
        retry = call(arguments={"query": "song"}, call_id="two")
        self.assertTrue(evaluate(rubric, evidence([loss, retry]))["passed"])
        for calls in ([loss], [retry, loss], [loss, call(arguments={"query": "other"})], [loss, call(status="error")]):
            with self.subTest(calls=calls):
                self.assertFalse(evaluate(rubric, evidence(calls))["passed"])

    def test_recover_response_from_environment_event(self):
        rubric = {"weights": {"state": 1}, "process_constraints": [{"type": "recover_response", "tool": "lookup", "mode": "retry"}]}
        calls = [call(arguments={"query": "song"}, status="error", call_id="one"),
                 call(arguments={"query": "song"}, call_id="two")]
        events = [{"tool": "lookup", "arguments": {"query": "song"}, "call_id": "one", "status": "ok", "response_lost": True, "sequence": 1},
                  {"tool": "lookup", "arguments": {"query": "song"}, "call_id": "two", "status": "ok", "sequence": 2}]
        self.assertTrue(evaluate(rubric, evidence(calls, events))["passed"])
        self.assertFalse(evaluate(rubric, evidence(calls[:1], events[:1]))["passed"])

    def test_unknown_process_fields_and_operator_rejected(self):
        for rule in ({"type": "unknown"}, {"type": "max_calls", "tool": "lookup", "count": 1, "before_turn": 1},
                     {"type": "recover_response", "tool": "lookup", "mode": "same_id"}):
            self.assertFalse(evaluate({"weights": {"state": 1}, "process_constraints": [rule]}, evidence())["valid"])
        self.assertFalse(evaluate({"weights": {"state": 1}, "expected_state": [{"path": "absent", "op": "unknown"}]}, evidence())["valid"])

    def test_missing_or_truncated_state_evidence_invalid(self):
        rubric = {"weights": {"state": 1}, "forbidden_changes": ["vehicle"]}
        for extra in ({"initial": None}, {"final": None}, {"truncated": True}):
            self.assertFalse(evaluate(rubric, evidence(**extra))["valid"])


class JudgeTests(unittest.TestCase):
    def setUp(self):
        self.rubric = {"weights": {"reply": 0.5, "judge": 0.5}, "judge_criteria": ["如实完成"]}

    def test_required_missing_judge_invalid_no_renormalization(self):
        result = evaluate(self.rubric, evidence())
        self.assertFalse(result["valid"])
        self.assertFalse(result["passed"])
        self.assertEqual(0, result["rewards"]["reward"])
        self.assertEqual(0.5, result["detail"]["weights"]["judge"])

    def test_runner_is_explicit_and_full_score_passes(self):
        seen = []
        def runner(rubric, data):
            seen.append((rubric, data))
            return {"scores": [{"score": 1, "reason": "满足"}]}
        result = evaluate(self.rubric, evidence(), runner)
        self.assertTrue(result["passed"])
        self.assertEqual(1, result["rewards"]["reward"])
        self.assertEqual(1, len(seen))

    def test_partial_judge_valid_but_not_passed(self):
        result = evaluate(self.rubric, evidence(), lambda r, e: {"scores": [{"score": 0.5}]})
        self.assertTrue(result["valid"])
        self.assertFalse(result["passed"])
        self.assertEqual(0.75, result["rewards"]["reward"])

    def test_zero_judge_weight_never_calls_runner(self):
        rubric = {"weights": {"reply": 1, "judge": 0}, "judge_criteria": ["unused"]}
        def fail(*args):
            self.fail("judge 不应执行")
        self.assertTrue(evaluate(rubric, evidence(), fail)["passed"])

    def test_judge_errors_are_invalid(self):
        for output in ({"scores": []}, {"scores": [{"score": math.nan}]}, {"scores": [{"score": True}]},
                       {"scores": [{"score": 2}]}, (None, {"available": False})):
            with self.subTest(output=output):
                result = evaluate(self.rubric, evidence(), lambda r, e: output)
                self.assertFalse(result["valid"])
                self.assertEqual(0, result["rewards"]["reward"])
        def fail(*args):
            raise RuntimeError("unavailable")
        self.assertFalse(evaluate(self.rubric, evidence(), fail)["valid"])

    def test_judge_weight_without_criteria_invalid(self):
        self.assertFalse(evaluate({"weights": {"judge": 1}}, evidence())["valid"])

    def test_tuple_adapter_and_multiple_items(self):
        self.assertTrue(evaluate(self.rubric, evidence(), lambda r, e: (1.0, {"available": True}))["passed"])
        rubric = {"weights": {"judge": 1}, "judge_criteria": ["one", "two"]}
        result = evaluate(rubric, evidence(), lambda r, e: {"scores": [{"score": 1}, {"score": 0}]})
        self.assertTrue(result["valid"])
        self.assertEqual(0.5, result["rewards"]["judge"])
        self.assertFalse(result["passed"])


class OracleFailureTests(unittest.TestCase):
    def setUp(self):
        self.rubric = {"weights": {"state": 1}, "expected_state": {"done": True}}
        self.data = evidence(final={"state": {"done": True}})

    def test_completed_state_cannot_hide_program_failure(self):
        result = evaluate(self.rubric, {**self.data, "program_failure": "assertion failed"})
        self.assertFalse(result["valid"])
        self.assertFalse(result["passed"])
        self.assertEqual(0, result["rewards"]["reward"])
        self.assertIn("program_failure", result["detail"]["validation_errors"][0])

    def test_evidence_and_cleanup_error_fields_always_invalidate(self):
        for key in ("evidence_error", "cleanup_error"):
            for value in ("TransportError", None, ""):
                with self.subTest(key=key, value=value):
                    result = evaluate(self.rubric, {**self.data, key: value})
                    self.assertFalse(result["valid"])
                    self.assertFalse(result["passed"])
                    self.assertEqual(0, result["rewards"]["reward"])
                    self.assertIn(key, result["detail"]["validation_errors"][0])

    def test_old_evidence_and_empty_program_failure_remain_valid(self):
        self.assertTrue(evaluate(self.rubric, self.data)["passed"])
        for value in (None, ""):
            self.assertTrue(evaluate(self.rubric, {**self.data, "program_failure": value})["passed"])

    def test_failure_short_circuits_external_judge(self):
        rubric = {"weights": {"judge": 1}, "judge_criteria": ["done"]}
        def prohibited_runner(*args):
            self.fail("失败证据不能调用judge")
        result = evaluate(rubric, {**self.data, "program_failure": "failed"}, prohibited_runner)
        self.assertFalse(result["valid"])
        self.assertEqual(0, result["rewards"]["reward"])


class APITests(unittest.TestCase):
    def test_incomplete_user_script_is_global_failure(self):
        rubric = {"weights": {"reply": 1}, "reply_checks": [{"pattern": "完成"}]}
        result = evaluate(rubric, evidence(user_script_complete=False))
        self.assertTrue(result["valid"])
        self.assertFalse(result["passed"])
        self.assertEqual(0, result["rewards"]["reward"])
        self.assertEqual("dialogue", result["detail"]["hard_violations"][0]["component"])
        for value in (True, None):
            self.assertTrue(evaluate(rubric, evidence(user_script_complete=value))["passed"])
        self.assertFalse(evaluate(rubric, evidence(user_script_complete="false"))["valid"])

    def test_evaluate_does_not_mutate_inputs(self):
        rubric = {"weights": {"tool_use": 1}, "tool_checks": [{"tool": "lookup"}]}
        data = evidence([call(call_id="a")], [{"tool": "lookup", "call_id": "a", "arguments": {}, "status": "ok", "sequence": 1}])
        before = copy.deepcopy((rubric, data))
        evaluate(rubric, data)
        self.assertEqual(before, (rubric, data))

    def test_weights_validate(self):
        for weights in ({"unknown": 1}, {"reply": -1}, {"reply": math.nan}, {"reply": True}, {}):
            self.assertFalse(evaluate({"weights": weights}, evidence())["valid"])

    def test_package_and_standalone_import(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([sys.executable, "-B", "-c", "from esa.score import evaluate; assert callable(evaluate)"], cwd=root, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            for name in ("score.py", "state_score.py"):
                (directory / name).write_text((root / "esa" / name).read_text("utf8"), "utf8")
            result = subprocess.run([sys.executable, "-B", "-c", "from score import evaluate; assert callable(evaluate)"], cwd=directory, capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr)

    def test_evaluate_does_not_call_network_or_read_judge_configuration(self):
        with patch("esa.score.score_judge", side_effect=AssertionError("不可调用外部judge")), patch("esa.score.os.environ", {}):
            result = evaluate({"weights": {"state": 1}, "expected_state": {"done": True}}, evidence(final={"state": {"done": True}}))
        self.assertTrue(result["passed"])


if __name__ == "__main__":
    unittest.main()
