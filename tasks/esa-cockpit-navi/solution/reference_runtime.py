"""Trusted Python reference programs executed over the real HTTP/MCP interface."""
from __future__ import annotations
import ast
import copy
import hashlib
import json
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
SESSION_FIELDS = {"type", "context", "initial_context", "initial_state", "data_overlays", "tool_profile", "faults", "seed", "dataset_version"}
REFERENCE_PATH = "solution/reference.py"
NEGATIVE_PATH = "tests/negative_tests.py"
SOLVE_SCRIPT = "#!/bin/bash\nset -euo pipefail\npython3 /solution/runner.py\n"

class ReferenceFailure(Exception):
    def __init__(self, message, report=None):
        super().__init__(message)
        self.report = report

class TransportError(ReferenceFailure):
    pass

def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()

def sensitive_key(key):
    name = str(key).lower()
    return name in {"api_key", "secret", "password", "authorization", "access_token"} or name.endswith(("_api_key", "_secret", "_token", "_password"))


def redact_evidence(value, secrets=()):
    if isinstance(value, dict):
        return {key: "<redacted>" if sensitive_key(key) else redact_evidence(child, secrets) for key, child in value.items()}
    if isinstance(value, list):
        return [redact_evidence(child, secrets) for child in value]
    if isinstance(value, str):
        for secret in secrets:
            if isinstance(secret, str) and len(secret) >= 4:
                value = value.replace(secret, "<redacted>")
        return re.sub(r"\b(?:sk|pk)-[A-Za-z0-9_-]{6,}", "<redacted>", value)
    return value


class HttpTransport:
    def __init__(self, base_url, timeout=30):
        self.base_url, self.timeout = base_url.rstrip("/"), timeout

    def request(self, method, path, body=None):
        if not path.startswith("/") or ".." in path.split("/"):
            raise ReferenceFailure("invalid API path")
        request = urllib.request.Request(self.base_url + path,
            data=None if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode(),
            headers={"Content-Type": "application/json"}, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise TransportError(f"HTTP {exc.code}: {method} {path}") from None
        except (OSError, ValueError, urllib.error.URLError) as exc:
            raise TransportError(f"transport failure: {method} {path} ({type(exc).__name__})") from None

def audit_source(source, filename):
    """Catch accidental hidden-input access; this is NOT a hostile Python sandbox."""
    tree = ast.parse(source, filename=filename)
    forbidden = {"open", "eval", "exec", "compile", "__import__", "globals", "locals", "getattr", "setattr", "delattr", "vars", "breakpoint", "input"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            modules = [n.name for n in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            if any(m not in {"math", "re"} for m in modules):
                raise ReferenceFailure(f"{filename}: imports are limited to math/re")
        if isinstance(node, ast.Name) and node.id in forbidden:
            raise ReferenceFailure(f"{filename}: file/dynamic execution access is not allowed")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ReferenceFailure(f"{filename}: private reference internals are not visible inputs")
    return tree

def _execute_module(source, filename, namespace):
    tree = audit_source(source, filename)
    try:
        execute_program(lambda ctx: exec(compile(tree, filename, "exec"), namespace), None, 30)
    except (SystemExit, GeneratorExit) as exc:
        raise ReferenceFailure(f"{filename}: program attempted to terminate validation ({type(exc).__name__})") from None


def load_reference(reference_path, source=None):
    namespace = {"__name__": "task_reference"}
    source = source if source is not None else pathlib.Path(reference_path).read_bytes().decode("utf8")
    _execute_module(source, REFERENCE_PATH, namespace)
    if not callable(namespace.get("solve")):
        raise ReferenceFailure("solution/reference.py needs solve(ctx)")
    alternatives = namespace.get("ALTERNATIVES", {})
    if not isinstance(alternatives, dict) or any(not isinstance(k, str) or not callable(v) for k, v in alternatives.items()):
        raise ReferenceFailure("program variants must map names to functions")
    return namespace


def load_programs(task_path, sources=None):
    source = sources[REFERENCE_PATH] if sources is not None else None
    namespace = load_reference(task_path / REFERENCE_PATH, source)
    solve, alternatives = namespace["solve"], namespace.get("ALTERNATIVES", {})
    negative_namespace = dict(namespace)
    source = sources[NEGATIVE_PATH] if sources is not None else (task_path / NEGATIVE_PATH).read_bytes().decode("utf8")
    _execute_module(source, NEGATIVE_PATH, negative_namespace)
    if negative_namespace.get("solve") is not solve:
        raise ReferenceFailure("tests/negative_tests.py cannot replace the reference solve entry")
    negatives = negative_namespace.get("CASES")
    if not isinstance(negatives, dict) or not negatives:
        raise ReferenceFailure("tests/negative_tests.py needs nonempty CASES")
    if any(not isinstance(k, str) or not callable(v) for k, v in negatives.items()):
        raise ReferenceFailure("program variants must map names to functions")
    return solve, negatives, alternatives

def load_task(task_path, contents=None):
    task_path = pathlib.Path(task_path).resolve()
    def read_text(name):
        return contents[name] if contents is not None else (task_path / name).read_bytes().decode("utf8")
    for target_name, canonical_name in (("tests/score.py", "score.py"), ("tests/state_score.py", "state_score.py"),
                                        ("solution/reference_runtime.py", "reference.py"), ("solution/runner.py", "oracle.py")):
        target = task_path / target_name
        expected = contents[f"canonical/{canonical_name}"] if contents is not None else (ROOT / "esa" / canonical_name).read_bytes().decode("utf8")
        if not target.is_file() or read_text(target_name) != expected:
            raise ReferenceFailure("verifier or Oracle copies are missing or stale; run scripts/sync-task-scorers.py")
    solve_path = task_path / "solution/solve.sh"
    if not solve_path.is_file() or read_text("solution/solve.sh") != SOLVE_SCRIPT:
        raise ReferenceFailure("task needs the canonical Harbor solution/solve.sh entry")
    initial = json.loads(read_text("environment/esa/initial_state.json"))
    rubric = json.loads(read_text("tests/rubric.json"))
    if not isinstance(initial, dict) or not isinstance(rubric, dict):
        raise ReferenceFailure("task input and rubric must be objects")
    messages, script = read_dialogue(initial)
    if initial.get("case_id") != rubric.get("case_id"):
        raise ReferenceFailure("task and rubric case_id differ")
    return task_path, initial, rubric, messages, script


def read_dialogue(initial):
    if not isinstance(initial, dict):
        raise ReferenceFailure("task input must be an object")
    messages = initial.get("user_messages") or [initial.get("instruction", "")]
    if not isinstance(messages, list) or not messages or any(not isinstance(m, str) or not m.strip() for m in messages):
        raise ReferenceFailure("task needs nonempty user_messages or instruction")
    script = initial.get("user_script", [])
    if not isinstance(script, list) or (script and len(messages) != 1):
        raise ReferenceFailure("user_script requires exactly one initial user message")
    for branch in script:
        if not isinstance(branch, dict) or set(branch) != {"reply_pattern", "user_reply"}:
            raise ReferenceFailure("user_script entries need reply_pattern and user_reply")
        if not isinstance(branch["user_reply"], str) or not branch["user_reply"].strip():
            raise ReferenceFailure("empty scripted user reply")
        re.compile(branch["reply_pattern"])
    return messages, script

class ReferenceContext:
    def __init__(self, transport, sid, messages, script):
        self._transport, self._sid = transport, sid
        self._script, self._messages = copy.deepcopy(script), list(messages[:1])
        self._pending_messages = list(messages[1:])
        self._branch = self._turn = self._rpc_index = 0
        self._calls, self._dialogue, self._tools = [], [], {}
        self._used_call_ids = set()
        self._begin(messages[0])

    @property
    def user_messages(self):
        return tuple(self._messages)

    @property
    def tools(self):
        return copy.deepcopy(self._tools)

    def _request(self, method, path, body=None):
        return self._transport.request(method, path, body)

    def _rpc(self, method, params):
        self._rpc_index += 1
        out = self._request("POST", f"/eval/mcp/{self._sid}", {"jsonrpc": "2.0", "id": self._rpc_index, "method": method, "params": params})
        if out.get("jsonrpc") != "2.0" or out.get("id") != self._rpc_index or "error" in out or "result" not in out:
            raise ReferenceFailure("invalid MCP response envelope")
        return out["result"]

    def _begin(self, message):
        self._request("POST", f"/eval/api/v1/sessions/{self._sid}/round", {})
        self._tools = {t["name"]: t for t in self._rpc("tools/list", {})["tools"]}
        data = self._request("GET", f"/api/v1/rl/sessions/{self._sid}/events")
        self._dialogue.append({"role": "user", "content": message, "turn_index": self._turn, "event_sequence": data.get("total_events", 0)})

    def call(self, name, args=None, *, expect="success", call_id=None):
        if expect not in {"success", "error", "response_lost"}:
            raise ReferenceFailure("unknown expected call outcome")
        if name not in self._tools:
            raise ReferenceFailure(f"reference tool is not visible: {name}")
        call_id, args = call_id or f"reference-{len(self._used_call_ids)}", copy.deepcopy(args or {})
        if not isinstance(call_id, str) or call_id in self._used_call_ids:
            raise ReferenceFailure("reference call_id must be unique, matching the model runtime")
        self._used_call_ids.add(call_id)
        call = {"name": name, "arguments": args, "call_id": call_id, "turn_index": self._turn}
        before = self._request("GET", f"/api/v1/rl/sessions/{self._sid}/events").get("total_events", 0)
        try:
            out = self._rpc("tools/call", {"name": name, "arguments": args, "call_id": call_id})
        except Exception:
            events = self._request("GET", f"/api/v1/rl/sessions/{self._sid}/events?after={before}").get("events", [])
            event = next((e for e in events if e.get("call_id") == call_id and e.get("response_lost")), None)
            if event is None:
                raise
            call.update(status="response_lost", output=None, event_sequence=event["sequence"])
            self._calls.append(call)
            if expect != "response_lost":
                raise ReferenceFailure(f"unexpected committed response loss: {name}") from None
            return None
        status = "error" if out.get("isError") else "success"
        if "structuredContent" not in out or not isinstance(out.get("content"), list):
            raise ReferenceFailure(f"unusable MCP tool result: {name}")
        data = self._request("GET", f"/api/v1/rl/sessions/{self._sid}/events")
        call.update(status=status, output=copy.deepcopy(out), event_sequence=data.get("total_events", 0))
        self._calls.append(call)
        if status != expect:
            raise ReferenceFailure(f"unexpected tool outcome: {name}, expected {expect}, got {status}")
        return copy.deepcopy(out["structuredContent"])

    def say(self, text):
        if not isinstance(text, str) or not text.strip():
            raise ReferenceFailure("reference reply must be nonempty text")
        data = self._request("GET", f"/api/v1/rl/sessions/{self._sid}/events")
        self._dialogue.append({"role": "assistant", "content": text, "turn_index": self._turn, "event_sequence": data.get("total_events", 0)})

    def ask(self, question):
        self.say(question)
        if self._branch >= len(self._script):
            raise ReferenceFailure("reference asked for an undeclared user answer")
        branch = self._script[self._branch]
        if not re.search(branch["reply_pattern"], question):
            raise ReferenceFailure("reference question does not match the scripted branch")
        self._branch += 1
        return self._next(branch["user_reply"])

    def next_user(self):
        if not self._pending_messages:
            raise ReferenceFailure("no pending user message")
        return self._next(self._pending_messages.pop(0))

    def _next(self, message):
        self._messages.append(message)
        self._turn += 1
        self._begin(message)
        return message

def execute_program(program, ctx, timeout):
    deadline = time.monotonic() + timeout
    previous = sys.gettrace()
    def trace(frame, event, arg):
        if time.monotonic() > deadline:
            raise ReferenceFailure("reference program exceeded its execution deadline")
        return trace
    sys.settrace(trace)
    try:
        return program(ctx)
    finally:
        sys.settrace(previous)

def run_program(transport, initial, messages, script, program, timeout=30):
    created = transport.request("POST", "/eval/api/v1/sessions", {k: v for k, v in initial.items() if k in SESSION_FIELDS})
    sid = created.get("session_id")
    if not isinstance(sid, str) or not re.fullmatch("[a-f0-9]+", sid):
        raise ReferenceFailure("invalid created session identifier")
    prefix, ctx, failure = f"/api/v1/rl/sessions/{sid}", None, None
    evidence = {"session_id": sid}
    try:
        evidence["initial"] = transport.request("GET", prefix + "/snapshot")
        ctx = ReferenceContext(transport, sid, messages, script)
        result = execute_program(program, ctx, timeout)
        if isinstance(result, str):
            ctx.say(result)
        elif result is not None:
            raise ReferenceFailure("solve(ctx) must finish synchronously and return None or a reply string")
        if ctx._pending_messages:
            raise ReferenceFailure("reference left declared user messages unconsumed")
    except (Exception, SystemExit, GeneratorExit) as exc:
        failure = str(exc) if isinstance(exc, ReferenceFailure) else f"program failed ({type(exc).__name__})"
    finally:
        try:
            evidence["final"] = transport.request("GET", prefix + "/snapshot")
            data = transport.request("GET", prefix + "/events")
            evidence["events"], evidence["truncated"] = data.get("events", []), data.get("truncated", False)
        except Exception as exc:
            evidence["evidence_error"] = type(exc).__name__
            failure = failure or "unable to collect complete state evidence"
        try:
            transport.request("DELETE", f"/eval/api/v1/sessions/{sid}")
        except Exception as exc:
            evidence["cleanup_error"] = type(exc).__name__
            failure = failure or "unable to delete reference session"
    evidence["calls"] = copy.deepcopy(ctx._calls) if ctx else []
    evidence["dialogue"] = copy.deepcopy(ctx._dialogue) if ctx else []
    evidence["reply"] = "\n".join(d["content"] for d in evidence["dialogue"] if d["role"] == "assistant")
    evidence["program_failure"] = failure
    evidence["user_script_complete"] = (ctx is not None and ctx._branch == len(script)) if script else None
    return evidence

def validate_task(task_path, transport, output_dir=None, judge_runner=None):
    from esa.score import evaluate
    started = time.monotonic()
    task_path = pathlib.Path(task_path).resolve()
    names = ("environment/esa/initial_state.json", "tests/rubric.json", REFERENCE_PATH, NEGATIVE_PATH,
             "tests/score.py", "tests/state_score.py", "solution/solve.sh", "solution/reference_runtime.py", "solution/runner.py")
    paths = {name: task_path / name for name in names}
    paths.update({f"canonical/{name}": ROOT / "esa" / name for name in ("score.py", "state_score.py", "reference.py", "oracle.py")})
    contents = {name: path.read_bytes().decode("utf8") for name, path in paths.items()}
    task_path, initial, rubric, messages, script = load_task(task_path, contents)
    solve, negatives, alternatives = load_programs(task_path, contents)
    fingerprint = digest({name: contents[name] for name in names[:4]})
    report = {"schema_version": 1, "case_id": initial["case_id"], "status": "fail", "passed": False,
              "input_sha256": fingerprint, "task_input_sha256": digest(initial), "reference": None,
              "alternatives": {}, "negative_tests": {}, "failures": [], "review_required": True}
    positive = run_program(transport, initial, messages, script, solve)
    assessment = evaluate(rubric, positive, judge_runner=judge_runner)
    report["reference"] = {"evidence": positive, "assessment": assessment}
    if positive["program_failure"] or not assessment.get("valid") or not assessment.get("passed"):
        report["failures"].append("reference did not complete and pass the full evaluator")
    manifest = (positive.get("final") or {}).get("manifest", {})
    report["environment"] = manifest
    report["evaluation_sha256"] = digest({"input": fingerprint, "environment": manifest,
        "score": contents["canonical/score.py"], "state_score": contents["canonical/state_score.py"],
        "runner": contents["canonical/reference.py"], "oracle": contents["canonical/oracle.py"],
        "oracle_entry": contents["solution/solve.sh"]})
    for name, program in alternatives.items():
        evidence = run_program(transport, initial, messages, script, program)
        assessment = evaluate(rubric, evidence, judge_runner=judge_runner)
        passed = not evidence["program_failure"] and assessment.get("valid") and assessment.get("passed")
        report["alternatives"][name] = {"passed": bool(passed), "evidence": evidence, "assessment": assessment}
        if not passed:
            report["failures"].append(f"alternative rejected: {name}")
    for name, program in negatives.items():
        evidence = run_program(transport, initial, messages, script, program)
        assessment = evaluate(rubric, evidence, judge_runner=judge_runner)
        rejected = not evidence["program_failure"] and assessment.get("valid") and not assessment.get("passed")
        report["negative_tests"][name] = {"rejected": bool(rejected), "evidence": evidence, "assessment": assessment}
        if not rejected:
            report["failures"].append(f"negative test did not finish with a valid rejection: {name}")
    variants = [report["reference"], *report["alternatives"].values(), *report["negative_tests"].values()]
    identities = []
    for variant in variants:
        for stage in ("initial", "final"):
            observed = (variant["evidence"].get(stage) or {}).get("manifest")
            required = ("implementation_sha256", "seed_sha256", "source_sha256")
            if not isinstance(observed, dict) or not isinstance(observed.get("dataset_version"), str) or not observed["dataset_version"].strip() or any(
                not isinstance(observed.get(key), str) or not re.fullmatch(r"[a-f0-9]{64}", observed[key]) for key in required
            ):
                report["failures"].append("reference evidence has missing or invalid environment fingerprints")
            else:
                identities.append(digest(observed))
    if len(set(identities)) > 1:
        report["failures"].append("reference variants used different environment identities")
    if any(not path.is_file() or path.read_bytes().decode("utf8") != contents[name] for name, path in paths.items()):
        report["failures"].append("task or evaluator content changed during reference validation")
    report["passed"] = not report["failures"]
    report["status"] = "pass" if report["passed"] else "fail"
    report["duration_ms"] = round((time.monotonic() - started) * 1000)
    if output_dir is not None:
        output_dir = pathlib.Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "golden-run.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", "utf8")
    return report
