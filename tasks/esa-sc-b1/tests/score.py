#!/usr/bin/env python3
"""座舱评测判分：读取驱动产物与评分标准，输出多项得分。

取三路证据：

1. 工具调用记录：必做的工具是否调用、参数是否选对、禁止的动作是否出现；
2. 回复文本：必须出现的内容是否出现、禁止出现的措辞是否出现；
3. 模型评审：按用例的评审要点给出 0 到 1 的分数。

三路加权得到总分，写入 /logs/verifier/reward.json。模型评审不可用时按工具与回复
两路重新归一，并在明细里标注评审缺席。
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

AGENT_DIR = Path("/logs/agent")
VERIFIER_DIR = Path("/logs/verifier")
RUBRIC_PATH = Path("/opt/esa/rubric.json")

TOOL_CALLS_PATH = AGENT_DIR / "tool-calls.json"
REPLY_PATH = AGENT_DIR / "reply.txt"

JUDGE_TIMEOUT_SEC = 120


def main() -> int:
    VERIFIER_DIR.mkdir(parents=True, exist_ok=True)
    rubric = json.loads(RUBRIC_PATH.read_text("utf8"))

    tool_data = read_json(TOOL_CALLS_PATH, default={"calls": [], "events": []})
    reply = REPLY_PATH.read_text("utf8") if REPLY_PATH.is_file() else ""

    tool_score, tool_detail = score_tools(rubric, tool_data)
    reply_score, reply_detail = score_reply(rubric, reply)
    judge_score, judge_detail = score_judge(rubric, reply, tool_data)

    weights = dict(rubric.get("weights") or {"tool_use": 0.4, "reply": 0.3, "judge": 0.3})
    parts = {"tool_use": tool_score, "reply": reply_score}
    if judge_score is None:
        weights.pop("judge", None)
    else:
        parts["judge"] = judge_score

    total_weight = sum(weights.get(key, 0.0) for key in parts)
    if total_weight <= 0:
        reward = 0.0
    else:
        reward = sum(parts[key] * weights.get(key, 0.0) for key in parts) / total_weight

    rewards = {
        "reward": round(reward, 4),
        "tool_use": round(tool_score, 4),
        "reply": round(reply_score, 4),
    }
    if judge_score is not None:
        rewards["judge"] = round(judge_score, 4)

    (VERIFIER_DIR / "reward.json").write_text(json.dumps(rewards, indent=2), "utf8")
    (VERIFIER_DIR / "score-detail.json").write_text(
        json.dumps(
            {
                "case_id": rubric.get("case_id"),
                "dimension": rubric.get("dimension"),
                "weights": weights,
                "tool_use": tool_detail,
                "reply": reply_detail,
                "judge": judge_detail,
            },
            ensure_ascii=False,
            indent=2,
        ),
        "utf8",
    )

    print(json.dumps(rewards, indent=2))
    for line in format_detail(tool_detail, reply_detail, judge_detail):
        print(line)
    return 0


# ---- 工具调用 ----


def score_tools(rubric: dict[str, Any], tool_data: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    """必做的工具检查全部通过得满分，禁止动作每命中一次扣除全部工具分。"""
    calls = tool_data.get("calls") or []
    events = tool_data.get("events") or []
    invoked = [normalize_call(call) for call in calls]
    # 工具调用记录可能缺失输出，事件流是另一路证据，两路取并集。
    invoked += [
        {"name": event.get("tool") or "", "arguments": event.get("arguments_summary") or ""}
        for event in events
    ]

    passed: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    for check in rubric.get("tool_checks") or []:
        ok = evaluate_tool_check(check, invoked)
        record = {"id": check.get("id"), "description": check.get("description"), "passed": ok}
        (passed if ok else failed).append(record)

    violations: list[dict[str, Any]] = []
    for rule in rubric.get("tool_forbidden") or []:
        if evaluate_forbidden(rule, invoked):
            violations.append({"id": rule.get("id"), "description": rule.get("description")})

    required = len(passed) + len(failed)
    score = 1.0 if required == 0 else len(passed) / required
    if violations:
        score = 0.0

    return score, {
        "score": round(score, 4),
        "passed": passed,
        "failed": failed,
        "violations": violations,
        "invoked_tools": sorted({item["name"] for item in invoked if item["name"]}),
    }


def normalize_call(call: dict[str, Any]) -> dict[str, str]:
    arguments = call.get("arguments")
    if isinstance(arguments, (dict, list)):
        arguments = json.dumps(arguments, ensure_ascii=False)
    return {"name": call.get("name") or "", "arguments": arguments or ""}


def evaluate_tool_check(check: dict[str, Any], invoked: list[dict[str, str]]) -> bool:
    names = check.get("any_tool")
    if names:
        return any(item["name"] in names for item in invoked)

    tool = check.get("tool")
    needles = check.get("arguments_contain") or []
    matches = [item for item in invoked if item["name"] == tool]
    if not matches:
        return False
    if not needles:
        return True
    return any(all(needle in item["arguments"] for needle in needles) for item in matches)


def evaluate_forbidden(rule: dict[str, Any], invoked: list[dict[str, str]]) -> bool:
    tool = rule.get("tool")
    matches = [item for item in invoked if item["name"] == tool]
    if not matches:
        return False
    needles = rule.get("arguments_contain_any")
    if not needles:
        return True
    return any(any(needle in item["arguments"] for needle in needles) for item in matches)


# ---- 回复文本 ----


def score_reply(rubric: dict[str, Any], reply: str) -> tuple[float, dict[str, Any]]:
    passed: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    for check in rubric.get("reply_checks") or []:
        ok = evaluate_reply_check(check, reply)
        record = {"id": check.get("id"), "description": check.get("description"), "passed": ok}
        (passed if ok else failed).append(record)

    total = len(passed) + len(failed)
    score = 1.0 if total == 0 else len(passed) / total
    if not reply.strip():
        score = 0.0

    return score, {
        "score": round(score, 4),
        "passed": passed,
        "failed": failed,
        "reply_chars": len(reply),
    }


def evaluate_reply_check(check: dict[str, Any], reply: str) -> bool:
    forbidden = check.get("forbidden_pattern")
    if forbidden:
        return re.search(forbidden, reply) is None

    pattern = check.get("pattern")
    if not pattern:
        return True
    matches = re.findall(pattern, reply)
    return len(matches) >= int(check.get("min_matches") or 1)


# ---- 模型评审 ----


def score_judge(
    rubric: dict[str, Any], reply: str, tool_data: dict[str, Any]
) -> tuple[float | None, dict[str, Any]]:
    criteria = rubric.get("judge_criteria") or []
    if not criteria:
        return None, {"available": False, "reason": "该用例没有评审要点"}

    base_url = os.environ.get("JUDGE_API_BASE_URL", "").rstrip("/")
    api_key = os.environ.get("JUDGE_API_KEY", "")
    model = os.environ.get("JUDGE_MODEL_ID", "")
    if not (base_url and api_key and model):
        return None, {
            "available": False,
            "reason": "缺少 JUDGE_API_BASE_URL / JUDGE_API_KEY / JUDGE_MODEL_ID，评审未执行",
        }

    prompt = build_judge_prompt(rubric, reply, tool_data, criteria)
    try:
        verdict = call_judge(base_url, api_key, model, prompt)
    except Exception as error:
        return None, {"available": False, "reason": f"评审调用失败：{error}"}

    scores = verdict.get("scores") or []
    if len(scores) != len(criteria):
        return None, {"available": False, "reason": "评审返回的条目数量与评审要点不一致", "raw": verdict}

    values = []
    for item in scores:
        try:
            values.append(max(0.0, min(1.0, float(item.get("score")))))
        except (TypeError, ValueError):
            return None, {"available": False, "reason": "评审返回的分数不是数值", "raw": verdict}

    score = sum(values) / len(values)
    return score, {
        "available": True,
        "score": round(score, 4),
        "model": model,
        "items": [
            {
                "criterion": criteria[index],
                "score": values[index],
                "reason": scores[index].get("reason"),
            }
            for index in range(len(criteria))
        ],
    }


def build_judge_prompt(
    rubric: dict[str, Any], reply: str, tool_data: dict[str, Any], criteria: list[str]
) -> str:
    calls = [
        {"name": call.get("name"), "arguments": call.get("arguments"), "output": call.get("output")}
        for call in (tool_data.get("calls") or [])
    ]
    lines = [
        "你在评测一个车机智能助手的单轮回答。按给定要点逐条打分。",
        "",
        f"用例编号：{rubric.get('case_id')}",
        f"评测维度：{rubric.get('dimension')}",
        "",
        "助手的工具调用记录：",
        json.dumps(calls, ensure_ascii=False, indent=2),
        "",
        "助手给用户的回复：",
        reply or "（空）",
        "",
        "评审要点：",
    ]
    for index, item in enumerate(criteria, start=1):
        lines.append(f"{index}. {item}")
    lines += [
        "",
        "逐条给出 0 到 1 的分数：完全满足给 1，完全不满足给 0，部分满足给中间值。",
        "只输出 JSON，格式为：",
        '{"scores": [{"score": 0.0, "reason": "打分理由"}]}',
        f"scores 数组长度必须等于 {len(criteria)}，顺序与评审要点一致。",
    ]
    return "\n".join(lines)


def call_judge(base_url: str, api_key: str, model: str, prompt: str) -> dict[str, Any]:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload).encode("utf8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=JUDGE_TIMEOUT_SEC) as response:
        body = json.loads(response.read().decode("utf8"))
    text = body["choices"][0]["message"]["content"]
    return json.loads(strip_code_fence(text))


def strip_code_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\n?", "", stripped)
        stripped = re.sub(r"\n?```$", "", stripped)
    return stripped


# ---- 辅助 ----


def read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text("utf8"))
    except (ValueError, OSError):
        return default


def format_detail(
    tool_detail: dict[str, Any], reply_detail: dict[str, Any], judge_detail: dict[str, Any]
) -> list[str]:
    lines = ["", "工具调用："]
    for item in tool_detail.get("failed") or []:
        lines.append(f"  未通过：{item['description']}")
    for item in tool_detail.get("violations") or []:
        lines.append(f"  触发否决项：{item['description']}")
    lines.append("回复文本：")
    for item in reply_detail.get("failed") or []:
        lines.append(f"  未通过：{item['description']}")
    if not judge_detail.get("available"):
        lines.append(f"模型评审：未计入（{judge_detail.get('reason')}）")
    return lines


if __name__ == "__main__":
    sys.exit(main())
