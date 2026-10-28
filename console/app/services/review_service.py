"""Human review service: queue, structured scoring, revision history."""

from __future__ import annotations

import json

from ..db import connect, rows_to_dicts, utc_now
from . import experiment_service

DEFAULT_DIMENSIONS = ["正确性", "完整性", "安全性", "格式符合度"]
VERDICTS = [("pass", "通过"), ("suspect", "存疑"), ("fail", "不通过")]

DEFAULTS = {
    "review_dimensions": DEFAULT_DIMENSIONS,
    "blind_review": False,
    "loop_agent": "opencode",
    "loop_model": "stepcode/gpt-5.6-sol",
}


class ReviewError(ValueError):
    pass


def get_settings() -> dict:
    settings = {key: value for key, value in DEFAULTS.items()}
    with connect() as conn:
        for row in conn.execute("SELECT key, value FROM settings").fetchall():
            try:
                settings[row["key"]] = json.loads(row["value"])
            except json.JSONDecodeError:
                settings[row["key"]] = row["value"]
    return settings


def set_settings(updates: dict) -> None:
    with connect() as conn:
        for key, value in updates.items():
            conn.execute(
                "INSERT INTO settings(key, value) VALUES (?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )


def dimensions() -> list[str]:
    value = get_settings().get("review_dimensions") or DEFAULT_DIMENSIONS
    return [str(name).strip() for name in value if str(name).strip()]


def flag_task(experiment_task_id: int) -> int:
    """Add a task to the review queue (idempotent)."""
    task = _get_task(experiment_task_id)
    if task is None:
        raise ReviewError(f"任务不存在：{experiment_task_id}")
    with connect() as conn:
        row = conn.execute(
            "SELECT id FROM reviews WHERE experiment_task_id=?", (experiment_task_id,)
        ).fetchone()
        if row:
            return int(row["id"])
        cur = conn.execute(
            "INSERT INTO reviews(experiment_task_id, dims, overall_verdict, note, created_at, updated_at)"
            " VALUES (?,'{}','suspect','',?,?)",
            (experiment_task_id, utc_now(), utc_now()),
        )
        return int(cur.lastrowid)


def submit_review(
    experiment_task_id: int,
    dims: dict,
    overall_verdict: str,
    note: str,
) -> int:
    """Insert or update the review of a task; every submit appends history."""
    if overall_verdict not in dict(VERDICTS):
        raise ReviewError(f"总体判定不合法：{overall_verdict}")
    scores = {}
    for name, value in (dims or {}).items():
        try:
            score = int(value)
        except (TypeError, ValueError):
            raise ReviewError(f"维度「{name}」分数必须是 1 到 5 的整数")
        if not 1 <= score <= 5:
            raise ReviewError(f"维度「{name}」分数必须是 1 到 5 的整数")
        scores[str(name)] = score
    if not scores:
        raise ReviewError("至少填写一个维度分数")
    task = _get_task(experiment_task_id)
    if task is None:
        raise ReviewError(f"任务不存在：{experiment_task_id}")
    now = utc_now()
    dims_json = json.dumps(scores, ensure_ascii=False)
    with connect() as conn:
        row = conn.execute(
            "SELECT id FROM reviews WHERE experiment_task_id=?", (experiment_task_id,)
        ).fetchone()
        if row:
            review_id = int(row["id"])
            conn.execute(
                "UPDATE reviews SET dims=?, overall_verdict=?, note=?, updated_at=? WHERE id=?",
                (dims_json, overall_verdict, note.strip(), now, review_id),
            )
        else:
            cur = conn.execute(
                "INSERT INTO reviews(experiment_task_id, dims, overall_verdict, note, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?)",
                (experiment_task_id, dims_json, overall_verdict, note.strip(), now, now),
            )
            review_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO review_history(review_id, dims, overall_verdict, note, created_at)"
            " VALUES (?,?,?,?,?)",
            (review_id, dims_json, overall_verdict, note.strip(), now),
        )
    return review_id


def get_review(experiment_task_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM reviews WHERE experiment_task_id=?", (experiment_task_id,)
        ).fetchone()
    return dict(row) if row else None


def get_history(review_id: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM review_history WHERE review_id=? ORDER BY id", (review_id,)
        ).fetchall()
    result = []
    for row in rows_to_dicts(rows):
        row["dims"] = json.loads(row["dims"] or "{}")
        result.append(row)
    return result


def list_reviews(experiment_id: int | None = None, state: str = "") -> list[dict]:
    """Review queue rows joined with experiment/task/score context."""
    query = (
        "SELECT rv.*, t.row_id, t.experiment_id, e.name AS experiment_name,"
        " e.job_name, sc.value AS score, sc.verdict, sc.failure_category,"
        " d.name AS dataset_name"
        " FROM reviews rv"
        " JOIN experiment_tasks t ON t.id = rv.experiment_task_id"
        " JOIN experiments e ON e.id = t.experiment_id"
        " LEFT JOIN scores sc ON sc.experiment_task_id = t.id AND sc.scorer_version_id = 0"
        " LEFT JOIN dataset_versions v ON v.id = e.dataset_version_id"
        " LEFT JOIN datasets d ON d.id = v.dataset_id"
    )
    conditions, params = [], []
    if experiment_id:
        conditions.append("t.experiment_id=?")
        params.append(experiment_id)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY rv.updated_at DESC, rv.id DESC"
    with connect() as conn:
        rows = rows_to_dicts(conn.execute(query, params).fetchall())
    for row in rows:
        row["dims"] = json.loads(row["dims"] or "{}")
        row["pending"] = not row["dims"]
    if state == "pending":
        rows = [r for r in rows if r["pending"]]
    elif state == "done":
        rows = [r for r in rows if not r["pending"]]
    return rows


def experiment_summary(experiment_id: int) -> dict:
    rows = list_reviews(experiment_id)
    summary = {"pass": 0, "suspect": 0, "fail": 0, "pending": 0}
    for row in rows:
        if row["pending"]:
            summary["pending"] += 1
        else:
            summary[row["overall_verdict"]] = summary.get(row["overall_verdict"], 0) + 1
    return summary


def review_group_rows(experiment_id: int) -> list[dict]:
    """Dashboard grouping by human verdict (unreviewed tasks form their own group)."""
    tasks = experiment_service.list_tasks(experiment_id)
    groups: dict[str, dict] = {}
    for task in tasks:
        key = task["review_verdict"] or "未复核"
        entry = groups.setdefault(key, {"group": key, "n": 0, "n_pass": 0, "scores": []})
        entry["n"] += 1
        if task.get("verdict") == "pass":
            entry["n_pass"] += 1
        if task["score"] is not None:
            entry["scores"].append(float(task["score"]))
    rows = []
    for entry in groups.values():
        scores = entry.pop("scores")
        entry["mean"] = round(sum(scores) / len(scores), 6) if scores else None
        entry["pass_rate"] = round(entry["n_pass"] / entry["n"], 6) if entry["n"] else None
        rows.append(entry)
    rows.sort(key=lambda r: (r["mean"] if r["mean"] is not None else 2, r["group"]))
    return rows


def _get_task(experiment_task_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM experiment_tasks WHERE id=?", (experiment_task_id,)
        ).fetchone()
    return dict(row) if row else None
