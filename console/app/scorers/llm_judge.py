"""LLM judge scorer: prompt-based scoring with structured JSON output."""

from __future__ import annotations

import json
import re

import httpx

from .base import ScoreResult, normalize_result
from .model_env import resolve_model

DEFAULT_PROMPT = """\
你是一名评测评分员。请根据评分标准对 Agent 的执行结果打分。

# 任务输入
{input}

# 预期结果
{expected}

# 评分标准
{rubric}

# Agent 执行轨迹（可能截断）
{trace}

# Agent 输出
{output}

只输出一个 JSON 对象，不要输出其他文字，格式：
{{"score": 0到1之间的小数, "verdict": "pass|fail|suspect", "failure_category": "失败类别或null", "reason": "判定理由", "evidence": ["依据摘录"]}}
"""

MAX_TRACE_CHARS = 8000
MAX_OUTPUT_CHARS = 4000


def _fill_prompt(template: str, input_text: str, expected: str, trace, metadata: dict, output: str) -> str:
    rubric = str(metadata.get("rubric") or metadata.get("metadata", {}).get("rubric") or "")
    trace_text = trace if isinstance(trace, str) else json.dumps(trace, ensure_ascii=False)
    replacements = {
        "{input}": input_text,
        "{expected}": expected,
        "{rubric}": rubric or "（无，按预期结果判定）",
        "{trace}": trace_text[:MAX_TRACE_CHARS],
        "{output}": output[:MAX_OUTPUT_CHARS],
        "{metadata}": json.dumps(metadata, ensure_ascii=False),
    }
    for key, value in replacements.items():
        template = template.replace(key, value)
    return template


def _extract_json(text: str) -> dict:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    else:
        brace = re.search(r"\{.*\}", text, re.DOTALL)
        if brace:
            text = brace.group(0)
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("judge 输出不是 JSON 对象")
    return parsed


def _call_openai(base_url: str, api_key: str, model: str, prompt: str) -> str:
    response = httpx.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
        },
        timeout=120,
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]


def _call_anthropic(base_url: str, api_key: str, model: str, prompt: str) -> str:
    response = httpx.post(
        f"{base_url}/messages",
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
        json={
            "model": model,
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=120,
    )
    response.raise_for_status()
    return response.json()["content"][0]["text"]


def run(config: dict, input_text: str, expected: str, trace, metadata: dict) -> ScoreResult:
    model = resolve_model(str(config.get("model_id", "")))
    template = str(config.get("prompt_template") or DEFAULT_PROMPT)
    output_text = str(metadata.get("output") or "")
    prompt = _fill_prompt(template, input_text, expected, trace, metadata, output_text)

    last_error: Exception | None = None
    for _attempt in range(3):
        try:
            if model["protocol"] == "anthropic":
                raw_text = _call_anthropic(model["base_url"], model["api_key"], model["model"], prompt)
            else:
                raw_text = _call_openai(model["base_url"], model["api_key"], model["model"], prompt)
            result = normalize_result(_extract_json(raw_text))
            result.context["judge_model"] = f"{model['protocol']}:{model['model']}"
            result.context["judge_raw"] = raw_text[:2000]
            return result
        except Exception as exc:  # noqa: BLE001 - retry then surface
            last_error = exc
    raise RuntimeError(f"LLM judge 三次尝试均失败：{last_error}")
