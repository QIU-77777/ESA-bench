"""确定性终态与过程评分；只在 verifier 中读取答案。"""
from __future__ import annotations
from typing import Any

_MISSING = object()
def lookup(value: Any, path: str) -> Any:
    for part in path.split(".") if path else []:
        if isinstance(value, dict): value = value.get(part, _MISSING)
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value): value = value[int(part)]
        else: return _MISSING
    return value

def predicate(value: Any, rule: dict) -> bool:
    op = rule.get("op", "eq"); expected = rule.get("value")
    if op == "exists": return (value is not _MISSING) == rule.get("value", True)
    if value is _MISSING: return False
    if op == "eq": return value == expected
    if op == "ne": return value != expected
    if op == "contains":
        try: return expected in value
        except TypeError: return False
    if op == "in":
        try: return value in expected
        except TypeError: return False
    try:
        if op == "gt": return value > expected
        if op == "gte": return value >= expected
        if op == "lt": return value < expected
        if op == "lte": return value <= expected
    except TypeError: return False
    raise ValueError(f"未知评分运算符：{op}")

def score_state(rubric: dict, initial: dict | None, final: dict | None, events: list, truncated: bool = False) -> tuple[float, dict]:
    rules = rubric.get("expected_state") or []
    if isinstance(rules, dict): rules = [{"path": path, "value": value} for path, value in rules.items()]
    forbidden = rubric.get("forbidden_changes") or []
    process = rubric.get("process_constraints") or []
    if not rules and not forbidden and not process: return 1.0, {"applicable": False}
    if initial is None or final is None: return 0.0, {"applicable": True, "failed": ["缺少状态快照"]}
    initial_state = initial.get("state", initial.get("context", initial))
    final_state = final.get("state", final.get("context", final))
    passed, failed = [], []
    for rule in rules:
        label = rule.get("description", rule["path"])
        (passed if predicate(lookup(final_state, rule["path"]), rule) else failed).append(label)
    for path in forbidden:
        changed = lookup(initial_state, path) != lookup(final_state, path)
        # 同时检查过程中修改后恢复的情况。
        def intersects(pointer):
            changed_path = pointer.lstrip("/").replace("/", ".").replace("~1", "/").replace("~0", "~")
            return not changed_path or changed_path == path or changed_path.startswith(path + ".") or path.startswith(changed_path + ".")
        changed |= any(intersects(c.get("path", "")) for event in events for c in event.get("changes", []))
        (failed if changed else passed).append("禁止修改 " + path)
    for rule in process:
        kind = rule.get("type")
        if kind == "ordered_tools":
            expected = iter(rule["tools"]); target = next(expected, None)
            for event in events:
                if event.get("tool") == target and event.get("status") in {"success", "ok", "completed"}:
                    target = next(expected, None)
            okay = target is None
        elif kind == "max_calls": okay = sum(e.get("tool") == rule["tool"] for e in events) <= rule["count"]
        elif kind == "no_failed_calls": okay = all(e.get("status") not in {"error", "failed"} for e in events)
        else: raise ValueError(f"未知过程约束：{kind}")
        (passed if okay else failed).append(rule.get("description", str(rule)))
    if truncated and (forbidden or process): failed.append("事件记录被截断")
    # 禁止行为与过程约束是硬条件；终态谓词按通过比例评分。
    score = len(passed) / (len(passed) + len(failed)) if passed or failed else 1.0
    if forbidden or process:
        hard_labels = ["禁止修改 " + p for p in forbidden] + [r.get("description", str(r)) for r in process] + ["事件记录被截断"]
        if any(label in failed for label in hard_labels): score = 0.0
    return score, {"applicable": True, "passed": passed, "failed": failed}
