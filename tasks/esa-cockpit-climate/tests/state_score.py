"""确定性状态与过程检查，供参考执行和正式评分共用。"""
from __future__ import annotations
from typing import Any

_MISSING = object()
SUCCESS = {"success", "ok", "completed"}
OPERATORS = {"exists", "eq", "ne", "contains", "in", "gt", "gte", "lt", "lte"}


def lookup(value: Any, path: str) -> Any:
    for part in path.split(".") if path else []:
        if isinstance(value, dict):
            value = value.get(part, _MISSING)
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return _MISSING
    return value


def predicate(value: Any, rule: dict) -> bool:
    op = rule.get("op", "eq")
    expected = rule.get("value")
    if op not in OPERATORS:
        raise ValueError(f"未知评分运算符：{op}")
    if op == "exists":
        return (value is not _MISSING) == rule.get("value", True)
    if value is _MISSING:
        return False
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
    except TypeError:
        return False
    return False


def _fields(rule: dict, allowed: set[str], where: str) -> None:
    if not isinstance(rule, dict) or set(rule) - allowed:
        raise ValueError(f"{where} 含未知规则字段或结构非法")
    if "required" in rule and not isinstance(rule["required"], bool):
        raise ValueError(f"{where}.required 必须为布尔值")


def score_state(rubric: dict, initial: dict | None, final: dict | None,
                events: list, truncated: bool = False, calls: list | None = None) -> tuple[float, dict]:
    rules = rubric.get("expected_state")
    if rules is None:
        rules = []
    if isinstance(rules, dict):
        rules = [{"path": path, "value": value} for path, value in rules.items()]
    forbidden = rubric.get("forbidden_changes")
    process = rubric.get("process_constraints")
    forbidden = [] if forbidden is None else forbidden
    process = [] if process is None else process
    if not all(isinstance(x, list) for x in (rules, forbidden, process)):
        raise ValueError("状态规则必须为对象或数组，禁止路径与过程规则必须为数组")
    for rule in rules:
        _fields(rule, {"path", "value", "op", "description", "required"}, "expected_state")
        if not isinstance(rule.get("path"), str) or not rule["path"]:
            raise ValueError("状态规则需要非空 path")
        if rule.get("op", "eq") not in OPERATORS:
            raise ValueError(f"未知评分运算符：{rule.get('op')}")
    if any(not isinstance(path, str) or not path for path in forbidden):
        raise ValueError("禁止修改路径必须为非空字符串")
    fields = {
        "ordered_tools": {"type", "tools", "description"},
        "max_calls": {"type", "tool", "count", "description"},
        "no_failed_calls": {"type", "description"},
        "recover_response": {"type", "tool", "mode", "description"},
    }
    for rule in process:
        if not isinstance(rule, dict) or rule.get("type") not in fields:
            raise ValueError(f"未知过程约束：{rule}")
        _fields(rule, fields[rule["type"]], "process_constraints")
        if rule["type"] == "ordered_tools":
            tools = rule.get("tools")
            if not isinstance(tools, list) or not tools or any(not isinstance(t, str) or not t for t in tools):
                raise ValueError("ordered_tools 需要非空工具数组")
        if rule["type"] == "recover_response":
            if not isinstance(rule.get("tool"), str) or not rule["tool"] or rule.get("mode") != "retry":
                raise ValueError("recover_response 需要 tool 和 mode='retry'")
        if rule["type"] == "max_calls":
            if not isinstance(rule.get("tool"), str) or not rule["tool"]:
                raise ValueError("max_calls 需要 tool")
            if type(rule.get("count")) is not int or rule["count"] < 0:
                raise ValueError("max_calls.count 必须为非负整数")
    detail = {"applicable": bool(rules or forbidden or process), "passed": [], "failed": [],
              "optional": [], "hard_violations": [], "valid": True}
    if not detail["applicable"]:
        return 1.0, detail
    if initial is None or final is None:
        detail.update(valid=False, failed=["缺少状态快照"])
        return 0.0, detail
    initial_state = initial.get("state", initial.get("context", initial))
    final_state = final.get("state", final.get("context", final))
    for rule in rules:
        label = rule.get("description", rule["path"])
        okay = predicate(lookup(final_state, rule["path"]), rule)
        if not rule.get("required", True):
            detail["optional"].append({"description": label, "passed": okay})
        else:
            detail["passed" if okay else "failed"].append(label)
    for path in forbidden:
        changed = lookup(initial_state, path) != lookup(final_state, path)
        def intersects(pointer):
            changed_path = pointer.lstrip("/").replace("/", ".").replace("~1", "/").replace("~0", "~")
            return not changed_path or changed_path == path or changed_path.startswith(path + ".") or path.startswith(changed_path + ".")
        changed |= any(intersects(c.get("path", "")) for event in events for c in event.get("changes", []))
        label = "禁止修改 " + path
        detail["failed" if changed else "passed"].append(label)
        if changed:
            detail["hard_violations"].append(label)
    for rule in process:
        kind = rule["type"]
        if kind == "ordered_tools":
            expected = iter(rule["tools"])
            target = next(expected, None)
            for event in events:
                if event.get("tool", event.get("name")) == target and event.get("status") in SUCCESS:
                    target = next(expected, None)
            okay = target is None
        elif kind == "max_calls":
            okay = sum(e.get("tool", e.get("name")) == rule["tool"] for e in events) <= rule["count"]
        elif kind == "recover_response":
            relevant = [c for c in (calls or []) if c.get("name", c.get("tool")) == rule["tool"]]
            okay = any(
                lost.get("status") == "response_lost" and any(
                    later.get("status") in SUCCESS and
                    later.get("arguments_value", later.get("arguments")) == lost.get("arguments_value", lost.get("arguments"))
                    for later in relevant[i + 1:]
                ) for i, lost in enumerate(relevant)
            )
        else:
            okay = all(e.get("status") in SUCCESS for e in events) and all(c.get("status") in SUCCESS for c in (calls or []))
        label = rule.get("description", str(rule))
        detail["passed" if okay else "failed"].append(label)
        if not okay:
            detail["hard_violations"].append(label)
    if truncated and (forbidden or process):
        detail["failed"].append("事件记录被截断")
        detail["hard_violations"].append("事件记录被截断")
        detail["valid"] = False
    count = len(detail["passed"]) + len(detail["failed"])
    score = len(detail["passed"]) / count if count else 1.0
    if detail["hard_violations"]:
        score = 0.0
    return score, detail
