"""Harbor integration: launch run.sh and read jobs/ progress."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .. import config


def build_run_command(
    bench_file: Path,
    harness: str,
    model_id: str,
    job_name: str,
    scorer_config: str | None = None,
) -> list[str]:
    command = [
        str(config.RUN_SH),
        "--bench-file", str(bench_file),
        "--harness", harness,
        "--model", model_id,
        "--job-name", job_name,
    ]
    if scorer_config:
        command += ["--scorer-config", scorer_config]
    return command


def start_run(command: list[str], log_path: Path) -> subprocess.Popen:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("w")
    return subprocess.Popen(
        command,
        cwd=config.REPO_ROOT,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )


def read_progress(job_name: str) -> dict:
    """Summarize a Harbor job directory for progress display."""
    job_dir = config.JOBS_DIR / job_name
    progress = {
        "job_exists": job_dir.is_dir(),
        "n_trials": 0,
        "n_done": 0,
        "n_errors": 0,
    }
    if not job_dir.is_dir():
        return progress
    for entry in sorted(job_dir.iterdir()):
        if not entry.is_dir():
            continue
        result_path = entry / "result.json"
        if result_path.is_file():
            progress["n_trials"] += 1
            try:
                result = json.loads(result_path.read_text())
            except json.JSONDecodeError:
                continue
            if result.get("exception_info"):
                progress["n_errors"] += 1
            if result.get("finished_at"):
                progress["n_done"] += 1
    result_path = job_dir / "result.json"
    if result_path.is_file():
        try:
            job_result = json.loads(result_path.read_text())
            progress["n_total"] = job_result.get("n_total_trials", progress["n_trials"])
            progress["finished"] = bool(job_result.get("finished_at"))
        except json.JSONDecodeError:
            pass
    return progress


def read_trial_summaries(job_name: str) -> list[dict]:
    """Read per-trial identity and metrics from jobs/<job>/<trial>/result.json."""
    job_dir = config.JOBS_DIR / job_name
    summaries: list[dict] = []
    if not job_dir.is_dir():
        return summaries
    for entry in sorted(job_dir.iterdir()):
        result_path = entry / "result.json"
        if not (entry.is_dir() and result_path.is_file()):
            continue
        try:
            result = json.loads(result_path.read_text())
        except json.JSONDecodeError:
            continue
        agent_result = result.get("agent_result") or {}
        summaries.append(
            {
                "row_id": entry.name.split("__")[0],
                "trial_name": entry.name,
                "trial_path": str(entry.relative_to(config.REPO_ROOT)),
                "status": "trial_failed" if result.get("exception_info") else "done",
                "duration_ms": _duration_ms(result),
                "n_input_tokens": agent_result.get("n_input_tokens"),
                "n_output_tokens": agent_result.get("n_output_tokens"),
            }
        )
    return summaries


def extract_final_output(trace) -> str:
    """Return the last assistant message text from a trajectory JSON."""
    texts: list[str] = []
    _collect_assistant(trace, texts)
    return texts[-1] if texts else ""


def _collect_assistant(node, out: list[str]) -> None:
    if isinstance(node, dict):
        # ATIF step form: {"source": "agent", "message": "..."}
        if node.get("source") in ("agent", "assistant"):
            message = node.get("message")
            if isinstance(message, str) and message.strip():
                out.append(message)
        if node.get("role") == "assistant":
            content = node.get("content")
            if isinstance(content, str) and content.strip():
                out.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, str) and part.strip():
                        out.append(part)
                    elif isinstance(part, dict) and part.get("type") == "text":
                        text = str(part.get("text") or "").strip()
                        if text:
                            out.append(text)
        for value in node.values():
            _collect_assistant(value, out)
    elif isinstance(node, list):
        for item in node:
            _collect_assistant(item, out)


def read_trial_trace(trial_path: str) -> tuple[object, str]:
    """Read trajectory JSON and the agent's final output from a trial directory."""
    trial_dir = config.REPO_ROOT / trial_path
    agent_dir = trial_dir / "agent"
    if not agent_dir.is_dir():
        agent_dir = trial_dir / "logs" / "agent"
    trace: object = {}
    trajectory = agent_dir / "trajectory.json"
    if trajectory.is_file():
        try:
            trace = json.loads(trajectory.read_text())
        except json.JSONDecodeError:
            trace = {"raw": trajectory.read_text()[:20000]}
    output = extract_final_output(trace)
    if not output:
        for name in ("opencode.txt", "pi.txt", "claude-code.txt"):
            candidate = agent_dir / name
            if candidate.is_file():
                output = candidate.read_text()[:4000]
                break
    return trace, output


def read_trial_scores(trial_path: str) -> dict | None:
    """Read /verifier/score.json produced by ConsoleVerifier."""
    trial_dir = config.REPO_ROOT / trial_path
    for candidate in (trial_dir / "verifier" / "score.json", trial_dir / "logs" / "verifier" / "score.json"):
        if candidate.is_file():
            try:
                return json.loads(candidate.read_text())
            except json.JSONDecodeError:
                return None
    return None


def _duration_ms(result: dict) -> int | None:
    started, finished = result.get("started_at"), result.get("finished_at")
    if not (started and finished):
        return None
    from datetime import datetime

    fmt = "%Y-%m-%dT%H:%M:%S.%f%z"
    try:
        delta = datetime.strptime(finished, fmt) - datetime.strptime(started, fmt)
    except ValueError:
        return None
    return int(delta.total_seconds() * 1000)
