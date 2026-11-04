#!/usr/bin/env python3
"""Native Harbor Oracle entry: solve through MCP without reading grading inputs.

The same file is distributed as solution/runner.py. Only Python's standard
library and solution/reference_runtime.py are required inside the container.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path


def _reference_api():
    try:
        from esa.reference import HttpTransport, load_reference, read_dialogue, redact_evidence, sensitive_key, run_program
    except ModuleNotFoundError as error:
        if error.name not in {"esa", "esa.reference"}:
            raise
        from reference_runtime import HttpTransport, load_reference, read_dialogue, redact_evidence, sensitive_key, run_program
    return HttpTransport, load_reference, read_dialogue, redact_evidence, sensitive_key, run_program


def _inputs(initial_path):
    initial = json.loads(Path(initial_path).read_text("utf8"))
    if not isinstance(initial, dict):
        raise ValueError("initial must be an object")
    return initial


def _write_evidence(output_dir, evidence, case_id):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    calls = {key: evidence.get(key) for key in (
        "session_id", "calls", "events", "dialogue", "truncated", "program_failure",
        "user_script_complete", "evidence_error", "cleanup_error") if key in evidence}
    calls["case_id"] = case_id
    calls["source"] = "oracle"
    payloads = {
        "tool-calls.json": calls,
        "dialogue.json": evidence.get("dialogue", []),
        "snapshot-initial.json": evidence.get("initial"),
        "snapshot-final.json": evidence.get("final"),
        "state-events.json": {"events": evidence.get("events", []), "truncated": evidence.get("truncated", False)},
        "environment-manifest.json": (evidence.get("final") or {}).get("manifest", {}),
    }
    for filename, value in payloads.items():
        (output_dir / filename).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", "utf8")
    (output_dir / "reply.txt").write_text(evidence.get("reply", ""), "utf8")


def run(initial_path, solution_dir, output_dir, transport=None, *, mock_url=None, timeout=30):
    """Run only solve(ctx), then write the evidence consumed by /tests/score.py.

    Failures return an evidence object with program_failure. No rubric, negative
    program, evaluator, model API or reward output is accessed by this function.
    """
    evidence = {"calls": [], "events": [], "dialogue": [], "reply": "", "truncated": False,
                "user_script_complete": None, "program_failure": None}
    case_id = None
    redactor = None
    try:
        HttpTransport, load_reference, read_dialogue, redact_evidence, sensitive_key, run_program = _reference_api()
        secrets = [value for key, value in os.environ.items() if sensitive_key(key)]
        redactor = lambda value: redact_evidence(value, secrets=secrets)
        initial = _inputs(initial_path)
        messages, script = read_dialogue(initial)
        case_id = initial.get("case_id")
        solution_dir = Path(solution_dir).resolve()
        reference_path = (solution_dir / "reference.py").resolve()
        if reference_path.parent != solution_dir:
            raise ValueError("reference must stay inside solution")
        source = reference_path.read_text("utf8")
        namespace = load_reference(reference_path, source=source)
        if transport is None:
            transport = HttpTransport(mock_url or os.environ.get("ESA_MOCK_URL", "http://mock-system:8003"))
        result = run_program(transport, initial, messages, script, namespace["solve"], timeout=timeout)
        if not isinstance(result, dict):
            raise TypeError("reference runtime must return evidence")
        evidence = result
        if not evidence.get("program_failure") and (evidence.get("initial") is None or evidence.get("final") is None or "events" not in evidence):
            evidence["program_failure"] = "Oracle execution has incomplete evidence"
    except (Exception, SystemExit, GeneratorExit, KeyboardInterrupt) as error:
        # Exception messages can contain input/HTTP data. Do not print or store them.
        evidence["program_failure"] = "Oracle execution failed (" + type(error).__name__ + ")"
    if redactor is not None:
        evidence = redactor(evidence)
    _write_evidence(output_dir, evidence, case_id)
    return evidence


def main(argv=None):
    parser = argparse.ArgumentParser(description="执行Harbor原生Oracle参考程序并保存真实MCP证据")
    parser.add_argument("--initial", type=Path, default=Path("/opt/esa/initial_state.json"))
    parser.add_argument("--solution", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--output-dir", type=Path, default=Path("/logs/agent"))
    parser.add_argument("--mock-url", default=None)
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout必须为有限正数")
    try:
        evidence = run(args.initial, args.solution, args.output_dir, mock_url=args.mock_url, timeout=args.timeout)
    except Exception as error:
        print("Oracle could not save execution evidence (" + type(error).__name__ + ")")
        return 1
    failed = bool(evidence.get("program_failure"))
    print(json.dumps({"source": "oracle", "status": "failed" if failed else "completed"}, ensure_ascii=False))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
