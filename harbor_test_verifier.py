"""Harbor verifier adapter: run a console Scorer inside the trial lifecycle.

Used via the task/CLI verifier selection:

    harbor run ... --verifier harbor_test_verifier:ConsoleVerifier \
                   --verifier-kwarg scorer_config=<path>

The scorer config JSON contains the scorer definition (llm or code). The
task metadata carries the dataset row data (row_id / expected / rubric /
tags). Scoring failures write score.json with an error and return reward 0.
"""

from __future__ import annotations

import json
import logging
import tomllib
from pathlib import Path
from typing import override

from harbor.environments.base import BaseEnvironment
from harbor.models.task.task import Task
from harbor.models.trial.paths import TrialPaths
from harbor.models.verifier.result import VerifierResult
from harbor.verifier.base import BaseVerifier

from console.app.scorers.base import run_scorer
from console.app.services.harbor_adapter import extract_final_output


def _aggregate(results: dict[str, dict]) -> dict:
    """Combine per-scorer results: mean score, strict verdict, merged categories."""
    if not results:
        return {"score": 0.0, "verdict": "fail", "failure_category": None,
                "reason": "没有评分器输出", "error": "no scorer output"}
    scores = [float(r.get("score", 0.0)) for r in results.values()]
    verdicts = [r.get("verdict", "fail") for r in results.values()]
    errors = [r.get("error") for r in results.values() if r.get("error")]
    categories = sorted({str(r["failure_category"]) for r in results.values()
                         if r.get("failure_category")})
    if errors or "fail" in verdicts:
        verdict = "fail"
    elif all(v == "pass" for v in verdicts):
        verdict = "pass"
    else:
        verdict = "suspect"
    reasons = [f"{key}: {r.get('reason', '')}" for key, r in results.items() if r.get("reason")]
    return {
        "score": round(sum(scores) / len(scores), 6),
        "verdict": verdict,
        "failure_category": ",".join(categories) or None,
        "reason": " | ".join(reasons)[:2000],
        "error": "; ".join(errors) or None,
    }


def _task_metadata(task: Task) -> dict:
    task_toml = Path(task.task_dir) / "task.toml"
    if not task_toml.is_file():
        return {}
    data = tomllib.loads(task_toml.read_text())
    metadata = data.get("metadata") or {}
    return metadata if isinstance(metadata, dict) else {}


def _load_trace(agent_dir: Path) -> tuple[object, str]:
    """Return (trajectory JSON, the agent's final output text)."""
    trajectory_path = agent_dir / "trajectory.json"
    trace: object = {}
    if trajectory_path.is_file():
        try:
            trace = json.loads(trajectory_path.read_text())
        except json.JSONDecodeError:
            trace = {"raw": trajectory_path.read_text()[:20000]}

    output = extract_final_output(trace)
    if not output:
        for name in ("opencode.txt", "pi.txt", "claude-code.txt"):
            candidate = agent_dir / name
            if candidate.is_file():
                output = candidate.read_text()[:4000]
                break
    return trace, output


class ConsoleVerifier(BaseVerifier):
    """Score a trial with the console Scorer selected by scorer_config."""

    def __init__(self, *, scorer_config: str = "", **kwargs):
        super().__init__(**kwargs)
        self.scorer_config = scorer_config

    @override
    async def verify(self) -> VerifierResult:
        verifier_dir = Path(self.trial_paths.verifier_dir)
        verifier_dir.mkdir(parents=True, exist_ok=True)
        score_path = verifier_dir / "score.json"

        metadata = _task_metadata(self.task)
        instruction = Path(self.task.task_dir) / "instruction.md"
        input_text = instruction.read_text() if instruction.is_file() else ""
        trace, output = _load_trace(Path(self.trial_paths.agent_dir))
        metadata["output"] = output

        results: dict[str, dict] = {}
        try:
            if not self.scorer_config:
                raise ValueError("scorer_config 未提供")
            config_data = json.loads(Path(self.scorer_config).read_text())
            entries = config_data.get("scorers")
            if entries is None:
                single = config_data.get("scorer") or config_data
                entries = [{"scorer_version_id": 0, "name": "scorer", "scorer": single}]
            for entry in entries:
                result = run_scorer(
                    entry.get("scorer") or entry,
                    input_text=input_text,
                    expected=str(metadata.get("expected", "")),
                    trace=trace,
                    metadata=metadata,
                )
                key = str(entry.get("scorer_version_id", 0))
                results[key] = result.to_dict()
        except Exception as exc:  # noqa: BLE001 - scoring failure is recorded, trial keeps reward 0
            results["0"] = {"score": 0.0, "verdict": "fail", "failure_category": None,
                            "reason": "", "evidence": [], "context": {}, "error": str(exc)}

        aggregate = _aggregate(results)
        score_path.write_text(json.dumps(
            {"aggregate": aggregate, "results": results}, ensure_ascii=False, indent=2
        ))
        return VerifierResult(rewards={"reward": float(aggregate["score"])})
