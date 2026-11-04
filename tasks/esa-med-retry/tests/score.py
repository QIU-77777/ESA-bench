#!/usr/bin/env python3
"""统一评分：参考执行与正式 verifier 使用相同的证据和规则。"""
from __future__ import annotations

import json
import math
import os
import re
import sys
import urllib.request
from pathlib import Path
from typing import Any

try:
    from .state_score import SUCCESS, score_state
except ImportError:  # /tests/score.py 的独立脚本入口。
    from state_score import SUCCESS, score_state

AGENT_DIR = Path("/logs/agent")
VERIFIER_DIR = Path("/logs/verifier")
RUBRIC_PATH = Path(os.environ.get("ESA_RUBRIC_PATH", "/tests/rubric.json"))
TOOL_CALLS_PATH = AGENT_DIR / "tool-calls.json"
REPLY_PATH = AGENT_DIR / "reply.txt"
JUDGE_TIMEOUT_SEC = 120


def _validate_fields(rule, allowed, where):
    if not isinstance(rule, dict):
        raise ValueError(f"{where} 必须为对象")
    unknown = set(rule) - allowed
    if unknown:
        raise ValueError(f"{where} 含未知规则字段：{sorted(unknown)}")
    if "required" in rule and type(rule["required"]) is not bool:
        raise ValueError(f"{where}.required 必须为布尔值")
    for key in ("turn_index", "before_turn"):
        if key in rule and (type(rule[key]) is not int or rule[key] < 0):
            raise ValueError(f"{where}.{key} 必须为非负整数")


def _array(container, key):
    value = container.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{key} 必须为数组")
    return value


def _json_value(value):
    if isinstance(value, str):
        try: return json.loads(value)
        except (ValueError, TypeError): pass
    return value


def _text(value):
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)


def normalize_call(call: dict[str, Any]) -> dict[str, Any]:
    arguments = call.get("arguments", call.get("arguments_summary", {}))
    output = _json_value(call.get("output", call.get("result")))
    status = call.get("status")
    envelope = output.get("result", output) if isinstance(output, dict) else None
    if isinstance(envelope, dict) and envelope.get("isError"):
        status = "error"
    elif status is None and isinstance(envelope, dict) and envelope.get("isError") is False:
        status = "success"
    if call.get("response_lost") is True:
        status = "response_lost"
    return {"name": call.get("name", call.get("tool", "")), "arguments": _text(arguments),
            "arguments_value": _json_value(arguments), "output": output, "status": status,
            "turn_index": call.get("turn_index"), "call_id": call.get("call_id"),
            "event_sequence": call.get("event_sequence", call.get("sequence"))}


def _invocations(evidence):
    calls = _array(evidence, "calls")
    events = _array(evidence, "events")
    event_calls = []
    turn = None
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("工具事件必须为对象")
        if event.get("tool", event.get("name")) == "session/round":
            turn = 0 if turn is None else turn + 1
        item = normalize_call(event)
        if item["turn_index"] is None:
            item["turn_index"] = turn
        event_calls.append(item)
    invoked, used = [], set()
    for call in calls:
        if not isinstance(call, dict):
            raise ValueError("工具调用必须为对象")
        item = normalize_call(call)
        match = next((i for i, event in enumerate(event_calls) if i not in used and (
            (item["call_id"] is not None and item["call_id"] == event["call_id"]) or
            (item["call_id"] is None and item["name"] == event["name"] and item["arguments_value"] == event["arguments_value"])
        )), None)
        if match is not None:
            used.add(match)
            event = event_calls[match]
            if event["status"] == "response_lost":
                item["status"] = "response_lost"
            for key in ("turn_index", "event_sequence", "status", "output"):
                if item[key] is None:
                    item[key] = event[key]
        invoked.append(item)
    invoked.extend(item for i, item in enumerate(event_calls) if i not in used)
    # 事件提供可比较的序号时使用提交顺序，否则保留调用记录顺序。
    if invoked and all(type(item["event_sequence"]) is int for item in invoked):
        invoked.sort(key=lambda item: item["event_sequence"])
    for item in invoked:
        for key in ("turn_index", "event_sequence"):
            if item[key] is not None and (type(item[key]) is not int or item[key] < 0):
                raise ValueError(f"调用 {key} 必须为非负整数")
    return invoked


def _subset(expected, actual):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and _subset(v, actual[k]) for k, v in expected.items())
    return actual == expected


def _tool_rule(rule, forbidden=False):
    allowed = {"id", "description", "tool", "any_tool", "arguments_contain", "arguments_match", "turn_index"}
    allowed |= {"arguments_contain_any", "before_check", "before_turn"} if forbidden else {"required"}
    _validate_fields(rule, allowed, "tool_forbidden" if forbidden else "tool_checks")
    names = rule.get("any_tool")
    tool = rule.get("tool")
    if names is not None:
        if tool is not None or not isinstance(names, list) or not names or any(not isinstance(n, str) or not n for n in names):
            raise ValueError("any_tool 需要非空工具数组，并且不能同时指定 tool")
    elif not isinstance(tool, str) or not tool:
        raise ValueError("工具规则需要 tool 或 any_tool")
    for key in ("arguments_contain", "arguments_contain_any"):
        if key in rule and (not isinstance(rule[key], list) or any(not isinstance(x, str) for x in rule[key])):
            raise ValueError(f"{key} 必须为字符串数组")
    if "arguments_match" in rule and not isinstance(rule["arguments_match"], dict):
        raise ValueError("arguments_match 必须为对象")
    if "before_check" in rule and (not isinstance(rule["before_check"], str) or not rule["before_check"]):
        raise ValueError("before_check 必须为非空检查 ID")
    if "before_check" in rule and "before_turn" in rule:
        raise ValueError("同一禁止规则不能同时指定 before_check 和 before_turn")


def _matches(rule, item, success=True):
    names = rule.get("any_tool") or [rule.get("tool")]
    if item["name"] not in names or (success and item["status"] not in SUCCESS):
        return False
    if "turn_index" in rule:
        if item["turn_index"] is None:
            raise ValueError("轮次工具检查缺少 turn_index 或 session/round 事件")
        if item["turn_index"] != rule["turn_index"]:
            return False
    if not all(x in item["arguments"] for x in rule.get("arguments_contain", [])):
        return False
    needles = rule.get("arguments_contain_any")
    if needles and not any(x in item["arguments"] for x in needles):
        return False
    return "arguments_match" not in rule or _subset(rule["arguments_match"], item["arguments_value"])


def evaluate_tool_check(check, invoked):
    _tool_rule(check)
    return any(_matches(check, item) for item in invoked)


def evaluate_forbidden(rule, invoked, checks=None):
    _tool_rule(rule, forbidden=True)
    boundary = len(invoked)
    if "before_check" in rule:
        check = (checks or {}).get(rule["before_check"])
        if check is None:
            raise ValueError(f"before_check 引用未知检查：{rule['before_check']}")
        boundary = next((i for i, item in enumerate(invoked) if _matches(check, item)), len(invoked))
    for i, item in enumerate(invoked):
        if not _matches(rule, item, success=False):
            continue
        if "before_turn" in rule:
            if item["turn_index"] is None:
                raise ValueError("before_turn 缺少 turn_index 或 session/round 事件")
            if item["turn_index"] >= rule["before_turn"]:
                continue
        if "before_check" not in rule or i < boundary:
            return True
    return False


def score_tools(rubric, tool_data):
    checks, forbidden = _array(rubric, "tool_checks"), _array(rubric, "tool_forbidden")
    check_ids = {}
    for check in checks:
        _tool_rule(check)
        if "id" in check:
            if not isinstance(check["id"], str) or not check["id"] or check["id"] in check_ids:
                raise ValueError("tool_check ID 必须为不重复的非空字符串")
            check_ids[check["id"]] = check
    for rule in forbidden:
        _tool_rule(rule, forbidden=True)
        if "before_check" in rule and rule["before_check"] not in check_ids:
            raise ValueError(f"before_check 引用未知检查：{rule['before_check']}")
    invoked = _invocations(tool_data)
    passed, failed, optional = [], [], []
    for check in checks:
        okay = evaluate_tool_check(check, invoked)
        record = {"id": check.get("id"), "description": check.get("description"), "passed": okay}
        if not check.get("required", True):
            optional.append(record)
        else:
            (passed if okay else failed).append(record)
    violations = [{"id": rule.get("id"), "description": rule.get("description"), "rule": rule}
                  for rule in forbidden if evaluate_forbidden(rule, invoked, check_ids)]
    total = len(passed) + len(failed)
    value = len(passed) / total if total else 1.0
    return (0.0 if violations else value), {"score": round(value if not violations else 0.0, 4),
        "passed": passed, "failed": failed, "optional": optional, "violations": violations,
        "invoked_tools": sorted({item["name"] for item in invoked if item["name"]})}


def _dialogue(evidence):
    dialogue = _array(evidence, "dialogue")
    for item in dialogue:
        if not isinstance(item, dict) or item.get("role") not in {"user", "assistant"} or not isinstance(item.get("content"), str):
            raise ValueError("dialogue 需要合法 role 与字符串 content")
        if type(item.get("turn_index")) is not int or item["turn_index"] < 0:
            raise ValueError("dialogue.turn_index 必须为非负整数")
        if "event_sequence" in item and (type(item["event_sequence"]) is not int or item["event_sequence"] < 0):
            raise ValueError("dialogue.event_sequence 必须为非负整数")
    return dialogue


def _reply_rule(check):
    _validate_fields(check, {"id", "description", "pattern", "forbidden_pattern", "min_matches", "facts", "contains", "required", "turn_index"}, "reply_checks")
    if not any(key in check for key in ("pattern", "forbidden_pattern", "facts", "contains")):
        raise ValueError("回复规则需要 pattern、forbidden_pattern、facts 或 contains")
    for key in ("pattern", "forbidden_pattern"):
        if key in check:
            if not isinstance(check[key], str):
                raise ValueError(f"{key} 必须为字符串")
            re.compile(check[key])
    if "min_matches" in check and ("pattern" not in check or type(check["min_matches"]) is not int or check["min_matches"] < 0):
        raise ValueError("min_matches 必须为非负整数，并且需要 pattern")
    for key in ("facts", "contains"):
        if key in check and (not isinstance(check[key], list) or any(not isinstance(x, str) for x in check[key])):
            raise ValueError(f"{key} 必须为字符串数组")


def evaluate_reply_check(check, reply):
    _reply_rule(check)
    if not reply.strip():
        return False
    if "forbidden_pattern" in check and re.search(check["forbidden_pattern"], reply) is not None:
        return False
    if "pattern" in check and len(re.findall(check["pattern"], reply)) < check.get("min_matches", 1):
        return False
    return all(x in reply for key in ("facts", "contains") for x in check.get(key, []))


def score_reply(rubric, reply, dialogue=None):
    checks = _array(rubric, "reply_checks")
    if not isinstance(reply, str):
        raise ValueError("reply 必须为字符串")
    dialogue = _dialogue({"dialogue": dialogue})
    assistant = [item for item in dialogue if item["role"] == "assistant"]
    all_text = "\n".join(item["content"] for item in assistant) if dialogue else reply
    passed, failed, optional = [], [], []
    for check in checks:
        _reply_rule(check)
        text = all_text
        if "turn_index" in check:
            if not dialogue:
                raise ValueError("轮次回复检查缺少 dialogue")
            text = "\n".join(item["content"] for item in assistant if item["turn_index"] == check["turn_index"])
        okay = evaluate_reply_check(check, text)
        record = {"id": check.get("id"), "description": check.get("description"), "passed": okay}
        if not check.get("required", True):
            optional.append(record)
        else:
            (passed if okay else failed).append(record)
    total = len(passed) + len(failed)
    value = len(passed) / total if total else (1.0 if all_text.strip() else 0.0)
    return value, {"score": round(value, 4), "passed": passed, "failed": failed,
                   "optional": optional, "reply_chars": len(all_text)}


def _judge_result(result, criteria):
    if isinstance(result, tuple) and len(result) == 2:
        value, detail = result
        if value is None:
            return None, detail
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1 or not isinstance(detail, dict):
            raise ValueError("judge 返回非法分数或明细")
        return float(value), {**detail, "available": True}
    if not isinstance(result, dict) or not isinstance(result.get("scores"), list) or len(result["scores"]) != len(criteria):
        raise ValueError("judge 返回的条目数量与评审要点不一致")
    items = []
    for criterion, item in zip(criteria, result["scores"]):
        value = item.get("score") if isinstance(item, dict) else None
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError("judge 分数必须为 0 到 1 的有限数值")
        items.append({"criterion": criterion, "score": value, "reason": item.get("reason", "")})
    return sum(item["score"] for item in items) / len(items), {"available": True, "items": items}


def evaluate(rubric, evidence, judge_runner=None):
    """返回 rewards/detail/passed/valid；judge_runner(rubric, evidence) 可返回 scores 或 (分数, 明细)。

    必需的显式检查不受权重零影响；optional 检查仅保存明细。judge 权重为零时不执行。
    缺少必要证据、未知规则或必需 judge 缺席时 valid=False，reward=0。
    """
    detail = {"case_id": rubric.get("case_id") if isinstance(rubric, dict) else None,
              "dimension": rubric.get("dimension") if isinstance(rubric, dict) else None,
              "validation_errors": [], "hard_violations": []}
    rewards = {"reward": 0.0, "tool_use": 0.0, "reply": 0.0}
    try:
        if not isinstance(rubric, dict) or not isinstance(evidence, dict):
            raise ValueError("rubric 与 evidence 必须为对象")
        execution_errors = [key for key in ("evidence_error", "cleanup_error") if key in evidence]
        if evidence.get("program_failure"):
            execution_errors.insert(0, "program_failure")
        if execution_errors:
            raise ValueError(f"执行证据包含失败标记：{', '.join(execution_errors)}")
        criteria = _array(rubric, "judge_criteria")
        if any(not isinstance(c, str) or not c for c in criteria):
            raise ValueError("judge_criteria 必须为非空字符串组成的数组")
        defaults = {"tool_use": 0.2 if rubric.get("tool_checks") or rubric.get("tool_forbidden") else 0.0,
                    "reply": 0.15 if rubric.get("reply_checks") else 0.0,
                    "state": 0.5 if rubric.get("expected_state") or rubric.get("forbidden_changes") or rubric.get("process_constraints") else 0.0,
                    "judge": 0.15 if criteria else 0.0}
        weights = rubric.get("weights", defaults)
        if not isinstance(weights, dict) or set(weights) - set(defaults):
            raise ValueError("weights 含未知分量或结构非法")
        weights = {key: weights.get(key, 0.0) for key in defaults}
        if any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in weights.values()):
            raise ValueError("评分权重必须为非负有限数值")
        detail["weights"] = weights
        dialogue = _dialogue(evidence)
        events = _array(evidence, "events")
        # 验证调用结构，即使当前题目没有工具规则。
        invoked = _invocations(evidence)
        if type(evidence.get("truncated", False)) is not bool:
            raise ValueError("truncated 必须为布尔值")
        initial, final = evidence.get("initial"), evidence.get("final")
        if any(x is not None and not isinstance(x, dict) for x in (initial, final)):
            raise ValueError("initial 与 final 必须为对象或 None")
        tool_value, tool_detail = score_tools(rubric, evidence)
        reply_value, reply_detail = score_reply(rubric, evidence.get("reply", ""), dialogue)
        state_value, state_detail = score_state(rubric, initial, final, events, evidence.get("truncated", False), invoked)
        detail.update(tool_use=tool_detail, reply=reply_detail, state=state_detail)
        if not state_detail["valid"]:
            detail["validation_errors"].append("状态或过程证据不完整")
        if evidence.get("truncated") and (rubric.get("tool_checks") or rubric.get("tool_forbidden")):
            detail["validation_errors"].append("工具事件记录被截断")
        detail["hard_violations"] = [{"component": "state", "description": x} for x in state_detail["hard_violations"]]
        detail["hard_violations"] += [{"component": "tool_use", **x} for x in tool_detail["violations"]]
        script_complete = evidence.get("user_script_complete")
        if script_complete is not None and type(script_complete) is not bool:
            raise ValueError("user_script_complete 必须为布尔值或 None")
        if script_complete is False:
            detail["hard_violations"].append({"component": "dialogue", "description": "用户应答脚本未完成"})
        judge_value, judge_detail = None, {"available": False, "required": weights["judge"] > 0}
        if weights["judge"] > 0:
            if not criteria:
                detail["validation_errors"].append("judge 有权重但缺少 judge_criteria")
            elif judge_runner is None:
                detail["validation_errors"].append("必需 judge 不可用")
            else:
                try:
                    judge_value, judge_detail = _judge_result(judge_runner(rubric, evidence), criteria)
                    if judge_value is None:
                        detail["validation_errors"].append("必需 judge 不可用")
                except Exception as exc:
                    detail["validation_errors"].append(f"judge 无效：{exc}")
        detail["judge"] = judge_detail
        parts = {"tool_use": tool_value, "reply": reply_value, "state": state_value, "judge": judge_value or 0.0}
        rewards.update(tool_use=round(tool_value, 4), reply=round(reply_value, 4))
        if state_detail["applicable"]:
            rewards["state"] = round(state_value, 4)
        elif weights["state"] > 0:
            detail["validation_errors"].append("state 有权重但没有状态或过程规则")
        if judge_value is not None:
            rewards["judge"] = round(judge_value, 4)
        if weights["reply"] > 0 and not reply_detail["reply_chars"]:
            reply_detail["failed"].append({"id": "nonempty_reply", "description": "回复为空", "passed": False})
        total = sum(weights.values())
        if total <= 0:
            detail["validation_errors"].append("至少需要一个正权重评分分量")
        valid = not detail["validation_errors"]
        passed = valid and not detail["hard_violations"] and not state_detail["failed"] and not tool_detail["failed"] and not reply_detail["failed"]
        if weights["judge"] > 0:
            passed = passed and judge_value == 1.0
        reward = sum(parts[key] * weights[key] for key in weights) / total if total else 0.0
        rewards["reward"] = round(reward, 4) if valid and not detail["hard_violations"] else 0.0
    except (ValueError, TypeError, KeyError, AttributeError, re.error) as exc:
        detail["validation_errors"].append(str(exc))
        valid, passed = False, False
    detail.update(valid=valid, passed=bool(passed))
    return {"rewards": rewards, "detail": detail, "passed": bool(passed), "valid": valid}


def main() -> int:
    VERIFIER_DIR.mkdir(parents=True, exist_ok=True)
    try:
        rubric = read_json(RUBRIC_PATH, {})
        tool_data = read_json(TOOL_CALLS_PATH, {"calls": [], "events": []})
        evidence = {**tool_data, "reply": REPLY_PATH.read_text("utf8") if REPLY_PATH.is_file() else "",
                    "initial": read_json(AGENT_DIR / "snapshot-initial.json", None),
                    "final": read_json(AGENT_DIR / "snapshot-final.json", None),
                    "dialogue": tool_data.get("dialogue", read_json(AGENT_DIR / "dialogue.json", []))}
        result = evaluate(rubric, evidence, judge_runner=lambda r, e: score_judge(r, e.get("reply", ""), e))
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        result = {"rewards": {"reward": 0.0}, "valid": False, "passed": False,
                  "detail": {"valid": False, "passed": False, "hard_violations": [],
                             "validation_errors": [f"评分输入无效：{exc}"]}}
    (VERIFIER_DIR / "reward.json").write_text(json.dumps(result["rewards"], indent=2), "utf8")
    (VERIFIER_DIR / "score-detail.json").write_text(json.dumps(result["detail"], ensure_ascii=False, indent=2), "utf8")
    print(json.dumps(result["rewards"], indent=2))
    return 0 if result["valid"] else 1


def score_judge(rubric, reply, tool_data):
    """正式 CLI 的外部 judge 适配器；evaluate 本身不读取凭据或访问网络。"""
    criteria = rubric.get("judge_criteria") or []
    if not criteria:
        return None, {"available": False, "reason": "该用例没有评审要点"}
    base_url = os.environ.get("JUDGE_API_BASE_URL", "").rstrip("/")
    api_key = os.environ.get("JUDGE_API_KEY", "")
    model = os.environ.get("JUDGE_MODEL_ID", "")
    if not (base_url and api_key and model):
        return None, {"available": False, "reason": "缺少 judge 配置"}
    try:
        value, detail = _judge_result(call_judge(base_url, api_key, model, build_judge_prompt(rubric, reply, tool_data, criteria)), criteria)
        return value, {**detail, "model": model}
    except Exception as exc:
        return None, {"available": False, "reason": f"评审调用失败：{exc}"}


def build_judge_prompt(rubric, reply, tool_data, criteria):
    lines = ["你在评测车机智能助手。根据完整对话与工具证据，按给定要点逐条打分。",
             f"用例编号：{rubric.get('case_id')}", f"评测维度：{rubric.get('dimension')}",
             "完整对话：", json.dumps(tool_data.get("dialogue", []), ensure_ascii=False),
             "工具调用记录：", json.dumps(tool_data.get("calls", []), ensure_ascii=False),
             "助手回复：", reply or "（空）", "评审要点："]
    lines.extend(f"{i}. {criterion}" for i, criterion in enumerate(criteria, 1))
    lines += ["每项评分为 0 到 1。只输出 JSON：", '{"scores": [{"score": 0.0, "reason": "理由"}]}',
              f"scores 长度必须等于 {len(criteria)}，顺序与评审要点一致。"]
    return "\n".join(lines)


def call_judge(base_url, api_key, model, prompt):
    payload = {"model": model, "messages": [{"role": "user", "content": prompt}],
               "temperature": 0, "response_format": {"type": "json_object"}}
    request = urllib.request.Request(f"{base_url}/chat/completions", data=json.dumps(payload).encode("utf8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}, method="POST")
    with urllib.request.urlopen(request, timeout=JUDGE_TIMEOUT_SEC) as response:
        body = json.loads(response.read().decode("utf8"))
    return json.loads(strip_code_fence(body["choices"][0]["message"]["content"]))


def strip_code_fence(text):
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\n?", "", stripped)
        stripped = re.sub(r"\n?```$", "", stripped)
    return stripped


def read_json(path, default):
    if not path.is_file():
        return default
    return json.loads(path.read_text("utf8"))


def format_detail(tool_detail, reply_detail, judge_detail):
    lines = ["工具调用："]
    lines.extend(f"  未通过：{item.get('description')}" for item in tool_detail.get("failed", []))
    lines.extend(f"  触发禁止动作：{item.get('description')}" for item in tool_detail.get("violations", []))
    lines.append("回复文本：")
    lines.extend(f"  未通过：{item.get('description')}" for item in reply_detail.get("failed", []))
    if not judge_detail.get("available"):
        lines.append(f"模型评审不可用：{judge_detail.get('reason', '未执行')}")
    return lines


if __name__ == "__main__":
    sys.exit(main())
