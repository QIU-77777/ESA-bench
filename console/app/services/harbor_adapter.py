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
) -> list[str]:
    return [
        str(config.RUN_SH),
        "--bench-file", str(bench_file),
        "--harness", harness,
        "--model", model_id,
        "--job-name", job_name,
    ]


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
