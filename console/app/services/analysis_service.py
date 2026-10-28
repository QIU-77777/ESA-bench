"""Analysis service: baselines, regression comparison, dashboards, export."""

from __future__ import annotations

import csv
import io
import json

from ..db import connect, rows_to_dicts, utc_now
from . import dataset_service, experiment_service, review_service


class AnalysisError(ValueError):
    pass


# ── Baseline ───────────────────────────────────────────────────────────


def set_baseline(experiment_id: int) -> None:
    experiment = experiment_service.get_experiment(experiment_id)
    if experiment is None:
        raise AnalysisError(f"实验不存在：{experiment_id}")
    with connect() as conn:
        conn.execute(
            "INSERT INTO baselines(dataset_id, experiment_id, set_at) VALUES (?,?,?)"
            " ON CONFLICT(dataset_id) DO UPDATE SET"
            " experiment_id=excluded.experiment_id, set_at=excluded.set_at",
            (experiment["dataset_version_id"], experiment_id, utc_now()),
        )


def get_baseline(experiment_id: int) -> dict | None:
    """Baseline experiment for the same dataset as this experiment."""
    experiment = experiment_service.get_experiment(experiment_id)
    if experiment is None:
        return None
    with connect() as conn:
        row = conn.execute(
            "SELECT e.*, b.set_at FROM baselines b JOIN experiments e ON e.id = b.experiment_id"
            " WHERE b.dataset_id=?",
            (experiment["dataset_version_id"],),
        ).fetchone()
    return dict(row) if row else None


def is_baseline(experiment_id: int) -> bool:
    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM baselines WHERE experiment_id=?", (experiment_id,)
        ).fetchone()
    return row is not None


def clear_baseline(experiment_id: int) -> None:
    """Remove the baseline mark of this experiment's dataset."""
    experiment = experiment_service.get_experiment(experiment_id)
    if experiment is None:
        raise AnalysisError(f"实验不存在：{experiment_id}")
    with connect() as conn:
        conn.execute(
            "DELETE FROM baselines WHERE dataset_id=?",
            (experiment["dataset_version_id"],),
        )


# ── Regression ─────────────────────────────────────────────────────────


def regression(experiment_id: int) -> dict:
    current = _score_map(experiment_id)
    baseline = get_baseline(experiment_id)
    if baseline is None:
        raise AnalysisError("该数据集还没有基线实验")
    base_scores = _score_map(baseline["id"])

    rows = []
    for row_id in sorted(set(current) | set(base_scores)):
        cur = current.get(row_id)
        base = base_scores.get(row_id)
        cur_score = cur["score"] if cur else None
        base_score = base["score"] if base else None
        if cur and base:
            delta = round((cur_score or 0) - (base_score or 0), 6)
            if cur.get("verdict") == "fail" and base.get("verdict") == "pass":
                status = "new_fail"
            elif cur.get("verdict") == "pass" and base.get("verdict") == "fail":
                status = "fixed"
            elif delta < 0:
                status = "regressed"
            elif delta > 0:
                status = "improved"
            else:
                status = "same"
        elif cur:
            delta, status = None, "new_task"
        else:
            delta, status = None, "removed"
        rows.append(
            {
                "row_id": row_id,
                "current": cur_score,
                "baseline": base_score,
                "delta": delta,
                "status": status,
                "verdict": cur.get("verdict") if cur else None,
            }
        )
    order = {"new_fail": 0, "regressed": 1, "improved": 2, "same": 3, "new_task": 4, "fixed": 5, "removed": 6}
    rows.sort(key=lambda r: (order.get(r["status"], 9), r["delta"] if r["delta"] is not None else 0))
    cur_values = [r["current"] for r in rows if r["current"] is not None]
    base_values = [r["baseline"] for r in rows if r["baseline"] is not None]
    return {
        "baseline": {"id": baseline["id"], "name": baseline["name"]},
        "rows": rows,
        "summary": {
            "n_new_fail": sum(1 for r in rows if r["status"] == "new_fail"),
            "n_fixed": sum(1 for r in rows if r["status"] == "fixed"),
            "n_regressed": sum(1 for r in rows if r["status"] == "regressed"),
            "mean_current": round(sum(cur_values) / len(cur_values), 6) if cur_values else None,
            "mean_baseline": round(sum(base_values) / len(base_values), 6) if base_values else None,
        },
    }


def _score_map(experiment_id: int) -> dict[str, dict]:
    result = {}
    for task in experiment_service.list_tasks(experiment_id):
        if task["score"] is not None:
            result[task["row_id"]] = {
                "score": task["score"],
                "verdict": task["verdict"],
            }
    return result


# ── Dashboards ─────────────────────────────────────────────────────────


def dashboard(experiment_id: int, group_by: str = "tags") -> dict:
    tasks = experiment_service.list_tasks(experiment_id)
    rows = _row_meta(experiment_id)

    scored = [t for t in tasks if t["score"] is not None]
    buckets: dict[str, int] = {}
    for task in scored:
        bucket = f"{int(float(task['score']) * 10) / 10:.1f}-{int(float(task['score']) * 10 + 1) / 10:.1f}"
        buckets[bucket] = buckets.get(bucket, 0) + 1

    categories: dict[str, int] = {}
    for task in scored:
        if task.get("failure_category"):
            for category in str(task["failure_category"]).split(","):
                categories[category.strip()] = categories.get(category.strip(), 0) + 1

    groups: dict[str, dict] = {}
    for task in tasks:
        meta = rows.get(task["row_id"], {})
        keys = _group_keys(meta, group_by)
        for key in keys:
            entry = groups.setdefault(key, {"group": key, "n": 0, "n_pass": 0, "scores": []})
            entry["n"] += 1
            if task.get("verdict") == "pass":
                entry["n_pass"] += 1
            if task["score"] is not None:
                entry["scores"].append(float(task["score"]))
    if group_by == "review":
        group_rows = review_service.review_group_rows(experiment_id)
    else:
        group_rows = []
        for entry in groups.values():
            scores = entry.pop("scores")
            entry["mean"] = round(sum(scores) / len(scores), 6) if scores else None
            entry["pass_rate"] = round(entry["n_pass"] / entry["n"], 6) if entry["n"] else None
            group_rows.append(entry)
        group_rows.sort(key=lambda r: (r["mean"] if r["mean"] is not None else 2, r["group"]))

    return {
        "distribution": [{"bucket": k, "count": v} for k, v in sorted(buckets.items())],
        "failure_categories": [
            {"category": k, "count": v} for k, v in sorted(categories.items(), key=lambda kv: -kv[1])
        ],
        "groups": group_rows,
        "group_by": group_by,
    }


def _row_meta(experiment_id: int) -> dict[str, dict]:
    experiment = experiment_service.get_experiment(experiment_id)
    result = {}
    for row in dataset_service.list_rows(experiment["dataset_version_id"]):
        result[row["row_id"]] = {
            "tags": json.loads(row["tags"]),
            "metadata": json.loads(row["metadata"]),
        }
    return result


def _group_keys(meta: dict, group_by: str) -> list[str]:
    if group_by == "tags":
        return [f"tag: {t}" for t in meta.get("tags", [])] or ["(无标签)"]
    value = meta.get("metadata", {}).get(group_by)
    return [f"{group_by}: {value}"] if value is not None else [f"{group_by}: (未设置)"]


def group_keys(experiment_id: int) -> list[str]:
    """Available grouping keys: tags, review verdict, plus dataset metadata keys."""
    keys = ["tags", "review"]
    seen = set()
    for meta in _row_meta(experiment_id).values():
        for key in meta.get("metadata", {}):
            if key not in seen:
                seen.add(key)
                keys.append(key)
    return keys


# ── Export ─────────────────────────────────────────────────────────────


def export_csv(experiment_id: int) -> str:
    tasks = experiment_service.list_tasks(experiment_id)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        ["row_id", "score", "verdict", "failure_category", "status",
         "duration_ms", "n_input_tokens", "n_output_tokens", "trial_name"]
    )
    for task in tasks:
        writer.writerow(
            [task["row_id"], task["score"], task["verdict"], task["failure_category"],
             task["status"], task["duration_ms"], task["n_input_tokens"],
             task["n_output_tokens"], task["harbor_trial_name"]]
        )
    return output.getvalue()
