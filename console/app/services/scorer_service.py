"""Scorer service: versioned scorer configs and tryout on real traces."""

from __future__ import annotations

import json
import time

from .. import config
from ..db import connect, rows_to_dicts, utc_now
from ..scorers.base import run_scorer
from . import dataset_service, harbor_adapter

VALID_TYPES = ("llm", "code")


class ScorerError(ValueError):
    pass


def create_scorer(name: str, scorer_type: str, config: dict, description: str = "") -> int:
    if not name.strip():
        raise ScorerError("名称不能为空")
    if scorer_type not in VALID_TYPES:
        raise ScorerError(f"未知评分器类型：{scorer_type}")
    config = dict(config)
    config["type"] = scorer_type
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO scorers(name, type, description, created_at) VALUES (?,?,?,?)",
            (name.strip(), scorer_type, description.strip(), utc_now()),
        )
        scorer_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO scorer_versions(scorer_id, version_no, config, change_note, created_at)"
            " VALUES (?,1,?,?,?)",
            (scorer_id, json.dumps(config, ensure_ascii=False), "初始版本", utc_now()),
        )
        return scorer_id


def save(name: str, scorer_type: str, config: dict, description: str, change_note: str) -> tuple[int, str]:
    """Save a draft: same name creates a new version, new name creates a scorer."""
    with connect() as conn:
        existing = conn.execute("SELECT * FROM scorers WHERE name=?", (name.strip(),)).fetchone()
    if existing:
        if existing["type"] != scorer_type:
            raise ScorerError(
                f"评分器 {name} 的类型是 {existing['type']}，与草稿类型 {scorer_type} 不一致"
            )
        version_id = add_version(int(existing["id"]), config, change_note)
        return int(existing["id"]), f"new_version:{version_id}"
    scorer_id = create_scorer(name, scorer_type, config, description)
    return scorer_id, "created"


def add_version(scorer_id: int, config: dict, change_note: str) -> int:
    with connect() as conn:
        scorer = conn.execute("SELECT * FROM scorers WHERE id=?", (scorer_id,)).fetchone()
        if scorer is None:
            raise ScorerError(f"评分器不存在：{scorer_id}")
        config = dict(config)
        config["type"] = scorer["type"]
        prev = conn.execute(
            "SELECT MAX(version_no) AS v FROM scorer_versions WHERE scorer_id=?",
            (scorer_id,),
        ).fetchone()
        cur = conn.execute(
            "INSERT INTO scorer_versions(scorer_id, version_no, config, change_note, created_at)"
            " VALUES (?,?,?,?,?)",
            (
                scorer_id,
                int(prev["v"] or 0) + 1,
                json.dumps(config, ensure_ascii=False),
                change_note.strip(),
                utc_now(),
            ),
        )
        return int(cur.lastrowid)


def list_scorers() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT s.*, COUNT(v.id) AS version_count, COALESCE(MAX(v.created_at), '') AS last_changed"
            " FROM scorers s LEFT JOIN scorer_versions v ON v.scorer_id = s.id"
            " GROUP BY s.id ORDER BY s.id DESC"
        ).fetchall()
        return rows_to_dicts(rows)


def list_versions(scorer_id: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM scorer_versions WHERE scorer_id=? ORDER BY version_no DESC",
            (scorer_id,),
        ).fetchall()
        return rows_to_dicts(rows)


def list_all_versions() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT sv.*, s.name AS scorer_name, s.type AS scorer_type"
            " FROM scorer_versions sv JOIN scorers s ON s.id = sv.scorer_id"
            " ORDER BY s.name, sv.version_no DESC"
        ).fetchall()
        return rows_to_dicts(rows)


def list_tryouts(limit: int = 10) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM scorer_tryouts ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return rows_to_dicts(rows)


def get_scorer_detail(scorer_id: int) -> dict | None:
    """Scorer with its full version history and configs."""
    with connect() as conn:
        scorer = conn.execute("SELECT * FROM scorers WHERE id=?", (scorer_id,)).fetchone()
        if scorer is None:
            return None
        versions = conn.execute(
            "SELECT * FROM scorer_versions WHERE scorer_id=? ORDER BY version_no DESC",
            (scorer_id,),
        ).fetchall()
    return {
        "scorer": dict(scorer),
        "versions": [
            {**dict(v), "config": json.loads(v["config"])} for v in versions
        ],
    }


def delete_scorer(scorer_id: int) -> None:
    """Delete a scorer and its versions; refuse while experiments reference it."""
    with connect() as conn:
        scorer = conn.execute("SELECT * FROM scorers WHERE id=?", (scorer_id,)).fetchone()
        if scorer is None:
            raise ScorerError(f"评分器不存在：{scorer_id}")
        referenced = conn.execute(
            "SELECT COUNT(*) AS n FROM experiment_scorers es"
            " JOIN scorer_versions sv ON sv.id = es.scorer_version_id"
            " WHERE sv.scorer_id=?",
            (scorer_id,),
        ).fetchone()
        if referenced["n"]:
            raise ScorerError(
                f"评分器 {scorer['name']} 被 {referenced['n']} 个实验引用，先删除相关实验"
            )
        conn.execute("DELETE FROM scorer_versions WHERE scorer_id=?", (scorer_id,))
        conn.execute("DELETE FROM scorers WHERE id=?", (scorer_id,))


def delete_tryout(tryout_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM scorer_tryouts WHERE id=?", (tryout_id,))


def get_version(version_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM scorer_versions WHERE id=?", (version_id,)).fetchone()
        return dict(row) if row else None


def tryout(draft_config: dict, experiment_id: int, row_id: str) -> dict:
    """Run a scorer draft against one real trace of a finished experiment."""
    with connect() as conn:
        task = conn.execute(
            "SELECT * FROM experiment_tasks WHERE experiment_id=? AND row_id=?",
            (experiment_id, row_id),
        ).fetchone()
        experiment = conn.execute(
            "SELECT * FROM experiments WHERE id=?", (experiment_id,)
        ).fetchone()
    if task is None or experiment is None:
        raise ScorerError("找不到该实验任务")
    if not task["harbor_trial_path"]:
        raise ScorerError("该任务还没有运行记录")

    row = _dataset_row(experiment, row_id)
    trace, output = harbor_adapter.read_trial_trace(task["harbor_trial_path"])
    metadata = {
        "row_id": row_id,
        "expected": row["expected"],
        "rubric": row["rubric"],
        "tags": row["tags"],
        "output": output,
    }

    started = time.monotonic()
    result = run_scorer(
        draft_config,
        input_text=row["input"],
        expected=row["expected"],
        trace=trace,
        metadata=metadata,
    )
    duration_ms = int((time.monotonic() - started) * 1000)

    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO scorer_tryouts(scorer_draft, trace_ref, output, duration_ms, created_at)"
            " VALUES (?,?,?,?,?)",
            (
                json.dumps(draft_config, ensure_ascii=False),
                f"{experiment['job_name']}/{task['harbor_trial_name']}",
                json.dumps(result.to_dict(), ensure_ascii=False),
                duration_ms,
                utc_now(),
            ),
        )
        tryout_id = int(cur.lastrowid)
    return {"tryout_id": tryout_id, "result": result.to_dict(), "duration_ms": duration_ms,
            "input": row["input"][:2000], "expected": row["expected"][:1000], "output": output[:2000]}


def get_tryout(tryout_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM scorer_tryouts WHERE id=?", (tryout_id,)).fetchone()
        return dict(row) if row else None


def get_tryout_detail(tryout_id: int) -> dict | None:
    """Full tryout view: record + dataset row + agent output and trajectory."""
    record = get_tryout(tryout_id)
    if record is None:
        return None
    job_name, _, trial_name = record["trace_ref"].partition("/")
    detail = {
        "id": record["id"],
        "trace_ref": record["trace_ref"],
        "duration_ms": record["duration_ms"],
        "created_at": record["created_at"],
        "result": json.loads(record["output"]),
        "draft": json.loads(record["scorer_draft"]),
        "input": "",
        "expected": "",
        "agent_output": "",
        "view_url": f"{config.HARBOUR_VIEW_BASE}/jobs/{job_name}/{trial_name}",
    }
    with connect() as conn:
        experiment = conn.execute(
            "SELECT * FROM experiments WHERE job_name=?", (job_name,)
        ).fetchone()
    if experiment is None:
        return detail
    row_id = trial_name.split("__")[0]
    try:
        row = _dataset_row(experiment, row_id)
        detail["input"] = row["input"]
        detail["expected"] = row["expected"]
    except ScorerError:
        pass
    _, agent_output = harbor_adapter.read_trial_trace(f"jobs/{job_name}/{trial_name}")
    detail["agent_output"] = agent_output
    return detail


def get_trace_detail(experiment: dict, task: dict) -> dict:
    """Trace detail: dataset row + per-scorer results + review state + view link."""
    row = _dataset_row(experiment, task["row_id"])
    with connect() as conn:
        score_rows = conn.execute(
            "SELECT sc.*, sv.version_no, s.name AS scorer_name, s.type AS scorer_type"
            " FROM scores sc"
            " LEFT JOIN scorer_versions sv ON sv.id = sc.scorer_version_id"
            " LEFT JOIN scorers s ON s.id = sv.scorer_id"
            " WHERE sc.experiment_task_id=?",
            (task["id"],),
        ).fetchall()
        review = conn.execute(
            "SELECT * FROM reviews WHERE experiment_task_id=?", (task["id"],)
        ).fetchone()

    aggregate = None
    per_scorer = []
    for r in score_rows:
        data = json.loads(r["reason"])
        if r["scorer_version_id"] == 0:
            aggregate = data
        else:
            per_scorer.append(
                {
                    "name": r["scorer_name"],
                    "type": r["scorer_type"],
                    "version_no": r["version_no"],
                    "result": data,
                }
            )
    trial = task["harbor_trial_name"]
    return {
        "input": row["input"],
        "expected": row["expected"],
        "rubric": row["rubric"],
        "tags": row["tags"],
        "aggregate": aggregate,
        "per_scorer": per_scorer,
        "review": dict(review) if review else None,
        "view_url": (
            f"{config.HARBOUR_VIEW_BASE}/jobs/{experiment['job_name']}/trials/{trial}"
            if trial
            else ""
        ),
    }


def _dataset_row(experiment: dict, row_id: str) -> dict:
    rows = dataset_service.list_rows(experiment["dataset_version_id"])
    for row in rows:
        if row["row_id"] == row_id:
            return {
                "input": row["input"],
                "expected": row["expected"],
                "rubric": row["rubric"],
                "tags": json.loads(row["tags"]),
            }
    raise ScorerError(f"数据集版本里没有任务行：{row_id}")
