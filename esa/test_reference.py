"""Reference admission tests over actual HTTP/MCP, without model credentials."""
import copy
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from esa.reference import (
    ReferenceFailure, TransportError, audit_source, load_programs, load_task,
    run_program, validate_task,
)
from scripts.validate_task import local_http_environment

ROOT = Path(__file__).resolve().parents[1]
TASK_NAMES = (
    'esa-navi-execute', 'esa-chg-select', 'esa-veh-clarify',
    'esa-inf-knowledge', 'esa-veh-boundary', 'esa-med-retry',
    'esa-med-response-loss',
)


class RecordingTransport:
    """Record real requests; never substitute tool outputs."""
    def __init__(self, transport):
        self.transport = transport
        self.requests = []

    def request(self, method, path, body=None):
        self.requests.append((method, path, copy.deepcopy(body)))
        return self.transport.request(method, path, body)


class EvidenceFaultTransport(RecordingTransport):
    """Inject response/transport faults after real HTTP; tool results stay unchanged."""
    def __init__(self, transport, hook):
        super().__init__(transport)
        self.hook = hook

    def request(self, method, path, body=None):
        result = super().request(method, path, body)
        return self.hook(method, path, body, copy.deepcopy(result))


class ReferenceHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = local_http_environment()
        cls.transport = cls.service.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.service.__exit__(None, None, None)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='esa-reference-tests-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.recording = RecordingTransport(self.transport)

    def clone_task(self, name='esa-navi-execute'):
        target = self.root / name
        shutil.copytree(ROOT / 'tasks' / name, target)
        return target

    def change_initial(self, task, **updates):
        path = task / 'environment/esa/initial_state.json'
        payload = json.loads(path.read_text())
        payload.update(updates)
        path.write_text(json.dumps(payload, ensure_ascii=False))

    def run_function(self, task, function, timeout=30):
        _, initial, _, messages, script = load_task(task)
        return run_program(self.recording, initial, messages, script, function, timeout=timeout)

    def assert_deleted_sessions(self):
        deleted = [path.rsplit('/', 1)[-1] for method, path, _ in self.recording.requests
                   if method == 'DELETE']
        self.assertTrue(deleted)
        for sid in deleted:
            with self.assertRaises(TransportError):
                self.transport.request('GET', f'/api/v1/rl/sessions/{sid}/snapshot')

    def test_seven_tasks_reference_fourteen_negatives_two_alternatives(self):
        negatives = alternatives = 0
        sessions = set()
        for name in TASK_NAMES:
            with self.subTest(task=name):
                report = validate_task(ROOT / 'tasks' / name, self.recording)
                self.assertTrue(report['passed'], report['failures'])
                self.assertEqual(report['status'], 'pass')
                self.assertIsNone(report['reference']['evidence']['program_failure'])
                self.assertTrue(report['reference']['assessment']['passed'])
                self.assertTrue(report['reference']['assessment']['valid'])
                self.assertIn('implementation_sha256', report['environment'])
                self.assertTrue(report['review_required'])
                variants = [report['reference'], *report['negative_tests'].values(),
                            *report['alternatives'].values()]
                for variant in variants:
                    evidence = variant['evidence']
                    self.assertNotIn(evidence['session_id'], sessions)
                    sessions.add(evidence['session_id'])
                    self.assertIn('initial', evidence)
                    self.assertIn('final', evidence)
                    self.assertIsNone(evidence['program_failure'])
                for negative in report['negative_tests'].values():
                    self.assertTrue(negative['rejected'])
                    self.assertFalse(negative['assessment']['passed'])
                    self.assertTrue(negative['assessment']['valid'])
                for alternative in report['alternatives'].values():
                    self.assertTrue(alternative['passed'])
                negatives += len(report['negative_tests'])
                alternatives += len(report['alternatives'])
        self.assertEqual(negatives, 14)
        self.assertEqual(alternatives, 2)
        self.assertEqual(len(sessions), 23)
        self.assert_deleted_sessions()

    def test_actual_user_script_creates_second_round_after_question(self):
        report = validate_task(ROOT / 'tasks/esa-veh-clarify', self.recording)
        evidence = report['reference']['evidence']
        dialogue = evidence['dialogue']
        self.assertEqual([item['role'] for item in dialogue], ['user', 'assistant', 'user', 'assistant'])
        self.assertEqual([item['turn_index'] for item in dialogue], [0, 0, 1, 1])
        self.assertRegex(dialogue[1]['content'], '温度|多少度')
        self.assertEqual(dialogue[2]['content'], '调到22度。')
        round_events = [event for event in evidence['events'] if event['tool'] == 'session/round']
        self.assertEqual(len(round_events), 2)
        self.assertEqual(evidence['calls'][0]['turn_index'], 1)
        self.assertLess(round_events[1]['sequence'], evidence['calls'][0]['event_sequence'])

    def test_visible_inputs_are_readonly_and_do_not_expose_answers(self):
        observed = {}
        def inspect_context(ctx):
            observed['messages'] = ctx.user_messages
            observed['tools'] = ctx.tools
            self.assertIsInstance(ctx.user_messages, tuple)
            self.assertIsInstance(ctx.tools, dict)
            altered = ctx.tools
            altered.clear()
            self.assertIn('get_home_or_company_address', ctx.tools)
            ctx.say('结束查询。')
        self.run_function(ROOT / 'tasks/esa-navi-execute', inspect_context)
        self.assertEqual(observed['messages'], ('导航去公司。',))
        session_bodies = [body for method, path, body in self.recording.requests
                          if method == 'POST' and path == '/eval/api/v1/sessions']
        self.assertEqual(len(session_bodies), 1)
        for forbidden in ('metadata', 'user_messages', 'user_script', 'expected_state', 'weights', 'rubric', 'reply_checks'):
            self.assertNotIn(forbidden, session_bodies[0])
        serialized_requests = json.dumps(self.recording.requests, ensure_ascii=False)
        self.assertNotIn('reference.py', serialized_requests)
        self.assertNotIn('negative_tests.py', serialized_requests)
        self.assertNotIn('rubric.json', serialized_requests)

    def test_missing_actual_return_field_fails_reference_and_cleans_up(self):
        task = self.clone_task()
        self.change_initial(task, faults=[{'tool': 'get_home_or_company_address', 'kind': 'missing_field', 'field': 'poi.id', 'times': 1}])
        report = validate_task(task, self.recording)
        self.assertFalse(report['passed'])
        self.assertIn('KeyError', report['reference']['evidence']['program_failure'])
        self.assert_deleted_sessions()

    def test_unknown_tool_cannot_bypass_visible_schema(self):
        task = self.clone_task()
        def unknown(ctx):
            ctx.call('nonexistent_tool', {})
        evidence = self.run_function(task, unknown)
        self.assertIn('not visible', evidence['program_failure'])
        self.assertEqual(evidence['calls'], [])
        self.assertFalse(any(event['tool'] == 'nonexistent_tool' for event in evidence['events']))
        self.assert_deleted_sessions()

    def test_expected_schema_error_is_recorded_but_unexpected_error_fails(self):
        task = ROOT / 'tasks/esa-navi-execute'
        def expected_error(ctx):
            error = ctx.call('get_home_or_company_address', {'poi_type': 'invalid'}, expect='error')
            self.assertEqual(error['error']['code'], 'schema_error')
            ctx.say('参数被拒绝。')
        evidence = self.run_function(task, expected_error)
        self.assertIsNone(evidence['program_failure'])
        self.assertEqual(evidence['calls'][0]['status'], 'error')
        def unexpected_error(ctx):
            ctx.call('get_home_or_company_address', {'poi_type': 'invalid'})
        evidence = self.run_function(task, unexpected_error)
        self.assertIn('unexpected tool outcome', evidence['program_failure'])
        self.assertEqual(evidence['calls'][0]['status'], 'error')
        self.assert_deleted_sessions()

    def test_expected_reject_and_committed_loss_have_distinct_evidence(self):
        rejected = validate_task(ROOT / 'tasks/esa-med-retry', self.recording)['reference']['evidence']
        self.assertIsNone(rejected['program_failure'])
        self.assertEqual([call['status'] for call in rejected['calls']], ['error', 'success', 'success'])
        lost = validate_task(ROOT / 'tasks/esa-med-response-loss', self.recording)['reference']['evidence']
        self.assertIsNone(lost['program_failure'])
        self.assertEqual([call['status'] for call in lost['calls']], ['response_lost', 'success', 'success'])
        self.assertNotEqual(lost['calls'][0]['call_id'], lost['calls'][1]['call_id'])
        self.assertEqual(lost['calls'][0]['arguments'], lost['calls'][1]['arguments'])
        self.assertTrue(any(event.get('response_lost') is True for event in lost['events']))
        self.assertEqual(len(lost['final']['state']['media']['playlist']), 1)

    def test_unexpected_committed_loss_fails_and_cleans_up(self):
        task = self.clone_task()
        self.change_initial(task, faults=[{'tool': 'get_home_or_company_address', 'kind': 'response_lost', 'times': 1}])
        report = validate_task(task, self.recording)
        self.assertFalse(report['passed'])
        evidence = report['reference']['evidence']
        self.assertIn('unexpected committed response loss', evidence['program_failure'])
        self.assertEqual(evidence['calls'][0]['status'], 'response_lost')
        self.assertTrue(any(event.get('response_lost') is True for event in evidence['events']))
        self.assert_deleted_sessions()

    def test_negative_exception_is_not_a_valid_rejection(self):
        task = self.clone_task()
        (task / 'tests/negative_tests.py').write_text(
            "def broken(ctx):\n    raise RuntimeError('negative must finish')\n"
            "CASES = {'broken': broken}\n"
        )
        report = validate_task(task, self.recording)
        self.assertTrue(report['reference']['assessment']['passed'])
        self.assertFalse(report['passed'])
        self.assertFalse(report['negative_tests']['broken']['rejected'])
        self.assertIn('RuntimeError', report['negative_tests']['broken']['evidence']['program_failure'])
        self.assert_deleted_sessions()

    def test_negative_using_correct_solution_is_not_rejected(self):
        task = self.clone_task()
        (task / 'tests/negative_tests.py').write_text("CASES = {'actually_correct': solve}\n")
        report = validate_task(task, self.recording)
        self.assertFalse(report['passed'])
        self.assertFalse(report['negative_tests']['actually_correct']['rejected'])
        self.assertTrue(report['negative_tests']['actually_correct']['assessment']['passed'])

    def test_invalid_dataset_fails_before_any_session_is_created(self):
        task = self.clone_task()
        self.change_initial(task, dataset_version='unknown-dataset-version')
        with self.assertRaises(TransportError):
            validate_task(task, self.recording)
        self.assertFalse(any(method == 'DELETE' for method, _, _ in self.recording.requests))

    def test_bad_user_script_and_case_id_are_rejected_before_transport(self):
        task = self.clone_task()
        self.change_initial(task, user_script=[{'user_reply': '22度'}])
        with self.assertRaises(ReferenceFailure):
            validate_task(task, self.recording)
        self.assertEqual(self.recording.requests, [])
        self.change_initial(task, user_script=[], case_id='mismatched-case')
        with self.assertRaises(ReferenceFailure):
            validate_task(task, self.recording)
        self.assertEqual(self.recording.requests, [])

    def test_unconsumed_user_messages_are_program_failure(self):
        task = self.clone_task()
        self.change_initial(task, user_messages=['导航去公司。', '查询现在的导航信息。'])
        solve, _, _ = load_programs(task)
        evidence = self.run_function(task, solve)
        self.assertIn('unconsumed', evidence['program_failure'])
        self.assert_deleted_sessions()

    def test_reference_return_text_is_recorded(self):
        def return_reply(ctx):
            return '这是返回形式的回复。'
        evidence = self.run_function(ROOT / 'tasks/esa-veh-boundary', return_reply)
        self.assertIsNone(evidence['program_failure'])
        self.assertEqual(evidence['reply'], '这是返回形式的回复。')

    def test_scoring_copy_drift_is_rejected_before_transport(self):
        task = self.clone_task()
        for filename in ('score.py', 'state_score.py'):
            with self.subTest(filename=filename):
                path = task / 'tests' / filename
                original = path.read_bytes()
                path.write_bytes(original + b'\n# Stale task scoring copy.\n')
                with self.assertRaises(ReferenceFailure):
                    validate_task(task, self.recording)
                self.assertEqual(self.recording.requests, [])
                path.write_bytes(original)

    def test_oracle_runtime_copies_and_entry_script_drift_are_rejected(self):
        task = self.clone_task()
        for filename in ('solution/reference_runtime.py', 'solution/runner.py', 'solution/solve.sh'):
            with self.subTest(filename=filename):
                path = task / filename
                original = path.read_bytes()
                path.write_bytes(original + b'\n# Changed Oracle entry implementation.\n')
                with self.assertRaises(ReferenceFailure):
                    validate_task(task, self.recording)
                self.assertEqual(self.recording.requests, [])
                path.write_bytes(original)

    def test_negative_program_source_is_in_input_fingerprint(self):
        task = self.clone_task()
        original = validate_task(task, self.recording)
        path = task / 'tests/negative_tests.py'
        path.write_text(path.read_text() + '\n# Negative source content changed.\n')
        changed = validate_task(task, self.recording)
        self.assertTrue(changed['passed'])
        self.assertNotEqual(original['input_sha256'], changed['input_sha256'])
        self.assertNotEqual(original['evaluation_sha256'], changed['evaluation_sha256'])

    def test_report_contains_full_evidence_and_input_fingerprint(self):
        task = self.clone_task('esa-inf-knowledge')
        output = self.root / 'report'
        report = validate_task(task, self.recording, output)
        self.assertTrue(report['passed'])
        persisted = json.loads((output / 'golden-run.json').read_text())
        self.assertEqual(persisted, report)
        self.assertRegex(report['input_sha256'], r'^[0-9a-f]{64}$')
        self.assertRegex(report['evaluation_sha256'], r'^[0-9a-f]{64}$')
        self.assertIn('structuredContent', persisted['reference']['evidence']['calls'][0]['output'])
        self.assertEqual(persisted['reference']['evidence']['final']['state']['vehicle']['battery'], 61)

    def test_initial_and_reference_source_change_fingerprints(self):
        task = self.clone_task('esa-inf-knowledge')
        original = validate_task(task, self.recording)
        self.change_initial(task, initial_state={'vehicle': {'battery': 61, 'range_km': 278, 'volume': 41}})
        changed_input = validate_task(task, self.recording)
        self.assertTrue(changed_input['passed'])
        self.assertNotEqual(original['input_sha256'], changed_input['input_sha256'])
        self.assertNotEqual(original['evaluation_sha256'], changed_input['evaluation_sha256'])
        path = task / 'solution/reference.py'
        path.write_text(path.read_text() + '\n# Source content changed for fingerprint verification.\n')
        changed_source = validate_task(task, self.recording)
        self.assertTrue(changed_source['passed'])
        self.assertNotEqual(changed_input['input_sha256'], changed_source['input_sha256'])
        self.assertNotEqual(changed_input['evaluation_sha256'], changed_source['evaluation_sha256'])


    def test_system_exit_and_generator_exit_are_reported_and_cleaned_up(self):
        task = ROOT / 'tasks/esa-veh-boundary'
        for exception in (SystemExit, GeneratorExit):
            with self.subTest(exception=exception.__name__):
                def terminate(ctx):
                    raise exception()
                evidence = self.run_function(task, terminate)
                self.assertIn(exception.__name__, evidence['program_failure'])
                self.assertIn('final', evidence)
        self.assert_deleted_sessions()

    def test_top_level_termination_and_negative_solve_override_are_rejected(self):
        task = self.clone_task()
        original = (task / 'solution/reference.py').read_text()
        for exception in ('SystemExit', 'GeneratorExit'):
            with self.subTest(exception=exception):
                (task / 'solution/reference.py').write_text(f'raise {exception}()\n')
                with self.assertRaises(ReferenceFailure):
                    load_programs(task)
        (task / 'solution/reference.py').write_text(original)
        (task / 'tests/negative_tests.py').write_text(
            "def solve(ctx):\n    ctx.say('假的参考解。')\n"
            "CASES = {'overridden': solve}\n"
        )
        with self.assertRaises(ReferenceFailure):
            load_programs(task)
        self.assertEqual(self.recording.requests, [])

    def test_negative_namespace_does_not_change_reference_helpers(self):
        task = self.clone_task()
        (task / 'tests/negative_tests.py').write_text(
            "def navigate(ctx, target):\n    raise RuntimeError('negative helper only')\n"
            "def harmless_wrong_reply(ctx):\n    ctx.say('不能导航。')\n"
            "CASES = {'wrong_reply': harmless_wrong_reply}\n"
        )
        report = validate_task(task, self.recording)
        self.assertTrue(report['passed'], report['failures'])
        self.assertTrue(report['reference']['assessment']['passed'])
        self.assertTrue(report['alternatives']['favorite_address_query']['passed'])
        self.assertTrue(report['negative_tests']['wrong_reply']['rejected'])

    def test_execution_deadline_stops_loop_and_restores_thread_trace(self):
        previous = sys.gettrace()
        def endless(ctx):
            while True:
                value = 1
        evidence = self.run_function(ROOT / 'tasks/esa-veh-boundary', endless, timeout=0.02)
        self.assertIn('deadline', evidence['program_failure'])
        self.assertIs(sys.gettrace(), previous)
        self.assert_deleted_sessions()

    def test_execution_deadlines_are_thread_local(self):
        from esa.reference import execute_program
        def worker(marker):
            def trace(frame, event, arg):
                return trace
            previous = sys.gettrace()
            sys.settrace(trace)
            try:
                self.assertEqual(execute_program(lambda ctx: marker, None, 1), marker)
                self.assertIs(sys.gettrace(), trace)
                def endless(ctx):
                    while True:
                        value = 1
                with self.assertRaises(ReferenceFailure):
                    execute_program(endless, None, 0.02)
                self.assertIs(sys.gettrace(), trace)
                return marker
            finally:
                sys.settrace(previous)
        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(set(executor.map(worker, ('first', 'second'))), {'first', 'second'})

    def test_same_call_id_is_rejected_before_second_tool_request(self):
        def duplicate(ctx):
            arguments = {'query_items': ['REMAINING_BATTERY']}
            ctx.call('get_vehicle_status', arguments, call_id='duplicated-call-id')
            ctx.call('get_vehicle_status', arguments, call_id='duplicated-call-id')
        evidence = self.run_function(ROOT / 'tasks/esa-inf-knowledge', duplicate)
        self.assertIsNotNone(evidence['program_failure'])
        self.assertEqual(len(evidence['calls']), 1)
        executions = [event for event in evidence['events'] if event['tool'] == 'get_vehicle_status']
        self.assertEqual(len(executions), 1)
        requests = [body for method, path, body in self.recording.requests
                    if method == 'POST' and '/mcp/' in path and body.get('method') == 'tools/call']
        self.assertEqual(len(requests), 1)
        self.assert_deleted_sessions()

    def test_snapshot_and_cleanup_errors_are_saved_as_failed_evidence(self):
        for failure_kind in ('snapshot', 'cleanup'):
            with self.subTest(failure=failure_kind):
                counts = {}
                def fault(method, path, body, result):
                    if method == 'GET' and path.endswith('/snapshot'):
                        counts[path] = counts.get(path, 0) + 1
                        if failure_kind == 'snapshot' and counts[path] > 1:
                            raise TransportError('injected final snapshot transport failure')
                    if failure_kind == 'cleanup' and method == 'DELETE':
                        raise TransportError('injected cleanup acknowledgement failure')
                    return result
                self.recording = EvidenceFaultTransport(self.transport, fault)
                def reply_only(ctx):
                    ctx.say('当前只能查询，没有执行控制能力，无法调整空调。')
                evidence = self.run_function(ROOT / 'tasks/esa-veh-boundary', reply_only)
                self.assertIsNotNone(evidence['program_failure'])
                error = 'evidence_error' if failure_kind == 'snapshot' else 'cleanup_error'
                self.assertIn(error, evidence)
                self.assertIn('calls', evidence)
                self.assertIn('dialogue', evidence)
                self.assert_deleted_sessions()

    def test_required_manifest_identity_fields_cannot_be_missing(self):
        for field in ('implementation_sha256', 'seed_sha256', 'source_sha256', 'dataset_version'):
            with self.subTest(field=field):
                def strip_identity(method, path, body, result):
                    if method == 'GET' and path.endswith('/snapshot'):
                        result['manifest'].pop(field, None)
                    return result
                self.recording = EvidenceFaultTransport(self.transport, strip_identity)
                report = validate_task(ROOT / 'tasks/esa-veh-boundary', self.recording)
                self.assertFalse(report['passed'])
                self.assertTrue(report['failures'])
                self.assert_deleted_sessions()

    def test_initial_final_manifest_identity_change_is_rejected(self):
        counts = {}
        def change_final(method, path, body, result):
            if method == 'GET' and path.endswith('/snapshot'):
                counts[path] = counts.get(path, 0) + 1
                if counts[path] > 1:
                    result['manifest']['seed_sha256'] = '0' * 64
            return result
        self.recording = EvidenceFaultTransport(self.transport, change_final)
        report = validate_task(ROOT / 'tasks/esa-veh-boundary', self.recording)
        self.assertFalse(report['passed'])
        self.assertTrue(report['failures'])
        self.assert_deleted_sessions()

    def test_variants_cannot_use_different_environment_identity(self):
        first_session = []
        def change_variant(method, path, body, result):
            if method == 'POST' and path == '/eval/api/v1/sessions' and not first_session:
                first_session.append(result['session_id'])
            if method == 'GET' and path.endswith('/snapshot'):
                sid = path.split('/')[-2]
                if sid != first_session[0]:
                    result['manifest']['implementation_sha256'] = 'f' * 64
            return result
        self.recording = EvidenceFaultTransport(self.transport, change_variant)
        report = validate_task(ROOT / 'tasks/esa-navi-execute', self.recording)
        self.assertTrue(report['reference']['assessment']['passed'])
        self.assertFalse(report['passed'])
        self.assertTrue(report['failures'])
        self.assert_deleted_sessions()

    def test_invalid_manifest_identity_values_are_rejected(self):
        invalid = (
            ('implementation_sha256', 'short-hash'),
            ('seed_sha256', 'G' * 64),
            ('source_sha256', None),
            ('dataset_version', 1),
            ('dataset_version', '   '),
        )
        for field, value in invalid:
            with self.subTest(field=field, value=value):
                def corrupt_identity(method, path, body, result):
                    if method == 'GET' and path.endswith('/snapshot'):
                        result['manifest'][field] = value
                    return result
                self.recording = EvidenceFaultTransport(self.transport, corrupt_identity)
                report = validate_task(ROOT / 'tasks/esa-veh-boundary', self.recording)
                self.assertFalse(report['passed'])
                self.assertTrue(report['failures'])
                self.assert_deleted_sessions()

    def test_scoring_copies_require_equal_bytes_not_normalized_newlines(self):
        task = self.clone_task()
        path = task / 'tests/score.py'
        original = path.read_bytes()
        self.assertNotIn(b'\r\n', original)
        path.write_bytes(original.replace(b'\n', b'\r\n'))
        self.assertNotEqual(path.read_bytes(), (ROOT / 'esa/score.py').read_bytes())
        with self.assertRaises(ReferenceFailure):
            validate_task(task, self.recording)
        self.assertEqual(self.recording.requests, [])

    def test_source_mutation_during_execution_is_detected(self):
        task = self.clone_task()
        mutated = []
        def mutate_source(method, path, body, result):
            if method == 'POST' and path == '/eval/api/v1/sessions' and not mutated:
                mutated.append(True)
                (task / 'solution/reference.py').write_text(
                    "def solve(ctx):\n    ctx.say('modified program would not navigate')\n"
                )
            return result
        self.recording = EvidenceFaultTransport(self.transport, mutate_source)
        report = validate_task(task, self.recording)
        self.assertTrue(mutated)
        self.assertTrue(report['reference']['assessment']['passed'])
        self.assertEqual(report['reference']['evidence']['final']['state']['navigation']['destination']['id'], '200001')
        self.assertFalse(report['passed'])
        self.assertTrue(report['failures'])
        self.assert_deleted_sessions()


class ReferenceAuditAndPackagingTests(unittest.TestCase):
    def test_file_and_dynamic_execution_access_are_rejected(self):
        invalid = (
            "import os\n", "import pathlib\n", "from environment.data import Dataset\n",
            "open('seed-v1.json')\n", "open('tests/rubric.json')\n",
            "eval('1')\n", "exec('pass')\n", "__import__('os')\n",
            "getattr(ctx, '_transport')\n", "ctx._transport\n",
        )
        for source in invalid:
            with self.subTest(source=source), self.assertRaises(ReferenceFailure):
                audit_source(source, 'reference.py')
        audit_source("import math\nimport re\nvalue = math.sqrt(4)\n", 'reference.py')

    def test_shared_scoring_copies_and_docker_only_initial(self):
        for name in TASK_NAMES:
            task = ROOT / 'tasks' / name
            with self.subTest(task=name):
                for task_filename, canonical in (
                    ('tests/score.py', ROOT / 'esa/score.py'),
                    ('tests/state_score.py', ROOT / 'esa/state_score.py'),
                    ('solution/reference_runtime.py', ROOT / 'esa/reference.py'),
                    ('solution/runner.py', ROOT / 'esa/oracle.py'),
                ):
                    path = task / task_filename
                    self.assertTrue(path.is_file(), f'missing synchronized implementation: {path}')
                    self.assertEqual(path.read_bytes(), canonical.read_bytes())
                self.assertEqual((task / 'solution/solve.sh').read_bytes(), b'#!/bin/bash\nset -euo pipefail\npython3 /solution/runner.py\n')
                self.assertFalse((task / 'reference.py').exists())
                self.assertFalse((task / 'negative_tests.py').exists())
                self.assertTrue((task / 'solution/reference.py').is_file())
                self.assertTrue((task / 'tests/negative_tests.py').is_file())
                copies = [line.strip() for line in (task / 'environment/Dockerfile').read_text().splitlines()
                          if line.strip().upper().startswith('COPY ')]
                self.assertEqual(copies, ['COPY esa/initial_state.json /opt/esa/initial_state.json'])
                self.assertEqual(sorted(p.name for p in (task / 'environment/esa').iterdir()), ['initial_state.json'])
                shell = (task / 'tests/test.sh').read_text()
                self.assertIn('ESA_RUBRIC_PATH=/tests/rubric.json', shell)
                self.assertNotIn('JUDGE_API', (task / 'task.toml').read_text())

    def test_native_oracle_acceptance_covers_benchmark_tasks(self):
        import yaml
        benchmark = yaml.safe_load((ROOT / 'configs/bench/esa.yaml').read_text())
        acceptance = yaml.safe_load((ROOT / 'configs/jobs/esa-oracle-acceptance.yaml').read_text())
        benchmark_paths = [task['path'] for task in benchmark['tasks']]
        acceptance_paths = [task['path'] for task in acceptance['tasks']]
        self.assertEqual(set(acceptance_paths), set(benchmark_paths))
        self.assertEqual(len(acceptance_paths), len(set(acceptance_paths)))
        self.assertEqual(len(benchmark_paths), len(set(benchmark_paths)))
        self.assertEqual([agent['name'] for agent in acceptance['agents']], ['oracle'])
        self.assertNotIn('import_path', acceptance['agents'][0])
        self.assertEqual(acceptance['environment']['type'], 'docker')
        self.assertNotIn('import_path', acceptance['environment'])

    def test_native_oracle_agent_and_task_paths_discover_standard_solution(self):
        from harbor.agents.oracle import OracleAgent
        from harbor.models.task.paths import TaskPaths
        from harbor.models.task.task import Task
        from harbor.models.trial.paths import TrialPaths
        with tempfile.TemporaryDirectory(prefix='esa-native-oracle-discovery-') as tmp:
            for name in TASK_NAMES:
                with self.subTest(task=name):
                    task_dir = ROOT / 'tasks' / name
                    task = Task(task_dir)
                    paths = TaskPaths(task_dir)
                    solution = task_dir / 'solution'
                    entry = solution / 'solve.sh'
                    self.assertEqual(paths.solution_dir, solution)
                    self.assertEqual(paths.solve_path, entry)
                    self.assertEqual(paths.discovered_solve_path_for(task.config.environment.os), entry)
                    paths.validate_input_tree(solution)
                    trial = TrialPaths(trial_dir=Path(tmp) / name)
                    oracle = OracleAgent(logs_dir=trial.agent_dir, task_dir=task_dir, trial_paths=trial)
                    self.assertEqual(oracle._resolve_solution_paths(), (solution, entry))
                    self.assertEqual({path.name for path in solution.iterdir()},
                                     {'solve.sh', 'reference.py', 'reference_runtime.py', 'runner.py'})
                    self.assertFalse((solution / 'rubric.json').exists())
                    self.assertFalse((solution / 'negative_tests.py').exists())

    def test_cli_failure_rewrites_old_success_report(self):
        with tempfile.TemporaryDirectory(prefix='esa-reference-cli-failure-') as tmp:
            root = Path(tmp)
            task = root / 'esa-navi-execute'
            shutil.copytree(ROOT / 'tasks/esa-navi-execute', task)
            path = task / 'environment/esa/initial_state.json'
            initial = json.loads(path.read_text())
            initial['dataset_version'] = 'unknown-dataset-version'
            path.write_text(json.dumps(initial, ensure_ascii=False))
            output = root / 'output'
            report_path = output / task.name / 'golden-run.json'
            report_path.parent.mkdir(parents=True)
            report_path.write_text(json.dumps({'status': 'pass', 'passed': True, 'old_success': True}))
            environment = {key: value for key, value in os.environ.items()
                           if not any(part in key for part in ('API_KEY', 'JUDGE_', 'RESPONSES_API', 'STEPFUN_'))}
            result = subprocess.run(
                [sys.executable, '-B', str(ROOT / 'scripts/validate_task.py'), '--task', str(task), '--local-http', '--json', '--output-dir', str(output)],
                cwd=ROOT, env=environment, capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            rows = json.loads(result.stdout)
            self.assertFalse(rows[0]['passed'])
            persisted = json.loads(report_path.read_text())
            self.assertIs(persisted['passed'], False)
            self.assertNotEqual(persisted['status'], 'pass')
            self.assertNotIn('old_success', persisted)

    def test_cli_all_local_http_outputs_seven_successful_reports(self):
        with tempfile.TemporaryDirectory(prefix='esa-reference-cli-') as tmp:
            environment = dict(os.environ)
            for name in list(environment):
                if any(part in name for part in ('API_KEY', 'JUDGE_', 'RESPONSES_API', 'STEPFUN_')):
                    environment.pop(name)
            result = subprocess.run(
                [sys.executable, '-B', str(ROOT / 'scripts/validate_task.py'), '--all', '--local-http', '--json', '--output-dir', tmp],
                cwd=ROOT, env=environment, capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            rows = json.loads(result.stdout)
            self.assertEqual({row['task'] for row in rows}, set(TASK_NAMES))
            self.assertTrue(all(row['passed'] for row in rows))
            self.assertEqual(sum(row['negatives'] for row in rows), 14)
            self.assertEqual(sum(row['alternatives'] for row in rows), 2)
            for name in TASK_NAMES:
                self.assertTrue((Path(tmp) / name / 'golden-run.json').is_file())


if __name__ == '__main__':
    unittest.main()
