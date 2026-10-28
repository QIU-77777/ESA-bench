"""Scorer protocol shared by the Harbor verifier and console tryout."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

VALID_VERDICTS = ("pass", "fail", "suspect")


@dataclass
class ScoreResult:
    score: float = 0.0
    verdict: str = "fail"
    failure_category: str | None = None
    reason: str = ""
    evidence: list[str] = field(default_factory=list)
    context: dict = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "verdict": self.verdict,
            "failure_category": self.failure_category,
            "reason": self.reason,
            "evidence": self.evidence,
            "context": self.context,
            "error": self.error,
        }


def normalize_result(raw: dict) -> ScoreResult:
    """Coerce a scorer payload into ScoreResult with schema checks.

    The score range is owned by the scorer itself (prompt wording or script
    logic); the platform only checks the value is numeric.
    """
    if not isinstance(raw, dict):
        raise ValueError("评分输出必须是 JSON 对象")
    try:
        score = float(raw.get("score", 0.0))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"score 不是数值：{raw.get('score')!r}") from exc
    verdict = str(raw.get("verdict", "fail"))
    if verdict not in VALID_VERDICTS:
        raise ValueError(f"verdict 非法：{verdict!r}，允许 {VALID_VERDICTS}")
    category = raw.get("failure_category")
    evidence = raw.get("evidence") or []
    if not isinstance(evidence, list):
        raise ValueError("evidence 必须是数组")
    return ScoreResult(
        score=score,
        verdict=verdict,
        failure_category=str(category) if category is not None else None,
        reason=str(raw.get("reason", "")),
        evidence=[str(e) for e in evidence],
        context=dict(raw.get("context") or {}),
    )


def run_scorer(
    config: dict,
    *,
    input_text: str,
    expected: str,
    trace,
    metadata: dict,
) -> ScoreResult:
    """Execute one scorer config against one trace."""
    kind = config.get("type")
    try:
        if kind == "llm":
            from . import llm_judge

            return llm_judge.run(config, input_text, expected, trace, metadata)
        if kind == "code":
            from . import code_runner

            return code_runner.run(config, input_text, expected, trace, metadata)
        return ScoreResult(error=f"未知评分器类型：{kind!r}")
    except Exception as exc:  # noqa: BLE001 - scoring failure becomes judge_failed
        return ScoreResult(error=str(exc))


def dumps_result(result: ScoreResult) -> str:
    return json.dumps(result.to_dict(), ensure_ascii=False, indent=2)
