"""Experiment orchestration: create, render, run, snapshot, finalize."""

from __future__ import annotations

import json
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .. import config
from ..db import connect, rows_to_dicts, utc_now
from ..render import task_renderer
from . import dataset_service, harbor_adapter


class ExperimentError(ValueError):
    pass


_RUNNING: dict[int, subprocess.Popen] = {}


def create_experiment(
    name: str,
    dataset_version_id: int,
    eval_profile_id: int,
    scorer_version_ids: list[int] | None = None,
) -> int:
    if not name.strip():
        raise ExperimentError("实验名称不能为空")
    if dataset_service.get_version(dataset_version_id) is None:
        raise ExperimentError(f"数据集版本不存在：{dataset_version_id}")
    with connect() as conn:
        if conn.execute("SELECT 1 FROM experiments WHERE name=?", (name.strip(),)).fetchone():
            raise ExperimentError(f"实验名称已存在：{name}")
        cur = conn.execute(
            "INSERT INTO experiments(name, dataset_version_id, eval_profile_id, created_at)"
            " VALUES (?,?,?,?)",
            (name.strip(), dataset_version_id, eval_profile_id, utc_now()),
        )
        experiment_id = int(cur.lastrowid)
        for version_id in scorer_version_ids or []:
            conn.execute(
                "INSERT OR IGNORE INTO experiment_scorers(experiment_id, scorer_version_id) VALUES (?,?)",
                (experiment_id, version_id),
            )
        return experiment_id


def get_experiment(experiment_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM experiments WHERE id=?", (experiment_id,)).fetchone()
        return dict(row) if row else None


def list_experiments() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT e.*, d.name AS dataset_name, v.version_no, p.name AS profile_name,"
            " p.version_no AS profile_version_no, p.harness, p.model_id"
            " FROM experiments e"
            " LEFT JOIN dataset_versions v ON v.id = e.dataset_version_id"
            " LEFT JOIN datasets d ON d.id = v.dataset_id"
            " LEFT JOIN eval_profiles p ON p.id = e.eval_profile_id"
            " ORDER BY e.id DESC"
        ).fetchall()
        return rows_to_dicts(rows)


def list_tasks(experiment_id: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT t.*, sc.value AS score, sc.verdict, sc.failure_category,"
            " sc.reason AS score_reason, rv.overall_verdict AS review_verdict"
            " FROM experiment_tasks t"
            " LEFT JOIN scores sc ON sc.experiment_task_id = t.id AND sc.scorer_version_id = 0"
            " LEFT JOIN reviews rv ON rv.experiment_task_id = t.id"
            " WHERE t.experiment_id=? ORDER BY t.row_id",
            (experiment_id,),
        ).fetchall()
        return rows_to_dicts(rows)


def delete_experiment(experiment_id: int) -> None:
    """Delete an experiment and everything attached to it."""
    with connect() as conn:
        experiment = conn.execute(
            "SELECT * FROM experiments WHERE id=?", (experiment_id,)
        ).fetchone()
        if experiment is None:
            raise ExperimentError(f"实验不存在：{experiment_id}")
        task_ids = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM experiment_tasks WHERE experiment_id=?", (experiment_id,)
            ).fetchall()
        ]
        for task_id in task_ids:
            conn.execute("DELETE FROM scores WHERE experiment_task_id=?", (task_id,))
            conn.execute("DELETE FROM reviews WHERE experiment_task_id=?", (task_id,))
        run_ids = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM loop_runs WHERE experiment_id=?", (experiment_id,)
            ).fetchall()
        ]
        for run_id in run_ids:
            conn.execute("DELETE FROM loop_drafts WHERE loop_run_id=?", (run_id,))
        conn.execute("DELETE FROM loop_runs WHERE experiment_id=?", (experiment_id,))
        conn.execute("DELETE FROM experiment_tasks WHERE experiment_id=?", (experiment_id,))
        conn.execute("DELETE FROM experiment_scorers WHERE experiment_id=?", (experiment_id,))
        conn.execute("DELETE FROM baselines WHERE experiment_id=?", (experiment_id,))
        conn.execute("DELETE FROM experiments WHERE id=?", (experiment_id,))


def duplicate_experiment(experiment_id: int, name: str) -> int:
    """Copy an experiment's bindings into a fresh draft."""
    source = get_experiment(experiment_id)
    if source is None:
        raise ExperimentError(f"实验不存在：{experiment_id}")
    with connect() as conn:
        if conn.execute("SELECT 1 FROM experiments WHERE name=?", (name.strip(),)).fetchone():
            raise ExperimentError(f"实验名称已存在：{name}")
        cur = conn.execute(
            "INSERT INTO experiments(name, dataset_version_id, eval_profile_id, created_at)"
            " VALUES (?,?,?,?)",
            (name.strip(), source["dataset_version_id"], source["eval_profile_id"], utc_now()),
        )
        new_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO experiment_scorers(experiment_id, scorer_version_id)"
            " SELECT ?, scorer_version_id FROM experiment_scorers WHERE experiment_id=?",
            (new_id, experiment_id),
        )
        return new_id


def update_draft(
    experiment_id: int,
    dataset_version_id: int,
    eval_profile_id: int,
    scorer_version_ids: list[int],
) -> None:
    """Rebind a draft experiment before it runs."""
    with connect() as conn:
        experiment = conn.execute(
            "SELECT * FROM experiments WHERE id=?", (experiment_id,)
        ).fetchone()
        if experiment is None:
            raise ExperimentError(f"实验不存在：{experiment_id}")
        if experiment["status"] != "draft":
            raise ExperimentError(f"仅草稿状态可编辑，当前状态：{experiment['status']}")
        conn.execute(
            "UPDATE experiments SET dataset_version_id=?, eval_profile_id=? WHERE id=?",
            (dataset_version_id, eval_profile_id, experiment_id),
        )
        conn.execute("DELETE FROM experiment_scorers WHERE experiment_id=?", (experiment_id,))
        for version_id in scorer_version_ids:
            conn.execute(
                "INSERT OR IGNORE INTO experiment_scorers(experiment_id, scorer_version_id)"
                " VALUES (?,?)",
                (experiment_id, version_id),
            )


def start(experiment_id: int) -> None:
    experiment = get_experiment(experiment_id)
    if experiment is None:
        raise ExperimentError(f"实验不存在：{experiment_id}")
    if experiment["status"] not in ("draft", "failed"):
        raise ExperimentError(f"当前状态不可运行：{experiment['status']}")
    if experiment_id in _RUNNING:
        raise ExperimentError("实验已在运行中")
    with connect() as conn:
        conn.execute(
            "UPDATE experiments SET status='running', error='', started_at=? WHERE id=?",
            (utc_now(), experiment_id),
        )
    threading.Thread(target=_run_pipeline, args=(experiment_id,), daemon=True).start()


def abort(experiment_id: int) -> None:
    process = _RUNNING.get(experiment_id)
    if process is None:
        raise ExperimentError("实验不在运行中")
    process.terminate()
    with connect() as conn:
        conn.execute(
            "UPDATE experiments SET status='aborted', finished_at=? WHERE id=?",
            (utc_now(), experiment_id),
        )


def progress(experiment_id: int) -> dict:
    experiment = get_experiment(experiment_id)
    if experiment is None:
        raise ExperimentError(f"实验不存在：{experiment_id}")
    job_name = experiment["job_name"]
    data = harbor_adapter.read_progress(job_name) if job_name else {"job_exists": False}
    data["status"] = experiment["status"]
    return data


def _run_pipeline(experiment_id: int) -> None:
    try:
        _execute(experiment_id)
    except Exception as exc:  # noqa: BLE001 - surface any pipeline failure to the UI
        with connect() as conn:
            conn.execute(
                "UPDATE experiments SET status='failed', error=?, finished_at=? WHERE id=?",
                (str(exc), utc_now(), experiment_id),
            )


def _execute(experiment_id: int) -> None:
    experiment = get_experiment(experiment_id)
    assert experiment is not None
    dataset_version = dataset_service.get_version(experiment["dataset_version_id"])
    dataset = dataset_service.get_dataset(dataset_version["dataset_id"])
    rows = dataset_service.list_rows(dataset_version["id"])
    with connect() as conn:
        profile = conn.execute(
            "SELECT * FROM eval_profiles WHERE id=?", (experiment["eval_profile_id"],)
        ).fetchone()
    if profile is None:
        raise ExperimentError("评测对象不存在")

    # 1. Render tasks (cached by dataset version content hash).
    cache_key = dataset_version["content_hash"].replace(":", "-")
    dataset_dir = config.GENERATED_DIR / "datasets" / cache_key
    tasks_dir = dataset_dir / "tasks"
    if not tasks_dir.is_dir():
        task_renderer.render_tasks(
            rows, dataset_dir, dataset["env_template"], dataset["name"], dataset_version["version_no"]
        )

    # 2. Bench config pointing at the rendered tasks.
    bench_path = config.GENERATED_DIR / "benches" / f"exp-{experiment_id}.yaml"
    bench_path.parent.mkdir(parents=True, exist_ok=True)
    bench_path.write_text(
        yaml.safe_dump(
            {
                "id": f"exp-{experiment_id}",
                "datasets": [{"path": str(tasks_dir.relative_to(config.REPO_ROOT))}],
                "n_concurrent_trials": 4,
            },
            sort_keys=False,
        )
    )

    # 3. Scorer configs consumed by ConsoleVerifier inside each trial.
    scorer_config_path: Path | None = None
    scorer_snapshot: list[dict] = []
    with connect() as conn:
        scorer_rows = conn.execute(
            "SELECT sv.id, sv.version_no, sv.config, s.name AS scorer_name, s.type AS scorer_type"
            " FROM experiment_scorers es"
            " JOIN scorer_versions sv ON sv.id = es.scorer_version_id"
            " JOIN scorers s ON s.id = sv.scorer_id"
            " WHERE es.experiment_id=? ORDER BY sv.id",
            (experiment_id,),
        ).fetchall()
    if scorer_rows:
        scorer_config_path = config.GENERATED_DIR / "scorers" / f"exp-{experiment_id}.json"
        scorer_config_path.parent.mkdir(parents=True, exist_ok=True)
        entries = []
        for row in scorer_rows:
            parsed = json.loads(row["config"])
            entries.append(
                {"scorer_version_id": row["id"], "name": row["scorer_name"], "scorer": parsed}
            )
            scorer_snapshot.append(
                {
                    "name": row["scorer_name"],
                    "type": row["scorer_type"],
                    "version_no": row["version_no"],
                    "scorer_version_id": row["id"],
                    "config": parsed,
                }
            )
        scorer_config_path.write_text(
            json.dumps({"scorers": entries}, ensure_ascii=False, indent=2)
        )

    # 4. Immutable snapshot.
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    job_name = f"exp-{experiment_id}-{timestamp}"
    job_config_archive = config.SNAPSHOTS_DIR / f"{job_name}-job.yaml"
    _archive_job_config(bench_path, profile, job_config_archive)
    run_command = harbor_adapter.build_run_command(
        bench_path, profile["harness"], profile["model_id"], job_name,
        str(scorer_config_path) if scorer_config_path else None,
    )
    snapshot = {
        "experiment_id": experiment_id,
        "created_at": utc_now(),
        "dataset": {
            "name": dataset["name"],
            "version_no": dataset_version["version_no"],
            "content_hash": dataset_version["content_hash"],
            "row_count": dataset_version["row_count"],
            "env_template": dataset["env_template"],
        },
        "eval_profile": {
            "name": profile["name"],
            "harness": profile["harness"],
            "model_id": profile["model_id"],
            "params": json.loads(profile["params"]),
        },
        "scorer": scorer_snapshot,
        "bench_config": str(bench_path.relative_to(config.REPO_ROOT)),
        "rendered_job_config": str(job_config_archive.relative_to(config.REPO_ROOT)),
        "run_command": " ".join(run_command),
        "environment": {"git_commit": _git_commit(), "harbor_version": _harbor_version()},
    }
    snapshot_path = config.SNAPSHOTS_DIR / f"{job_name}-snapshot.json"
    snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2))
    with connect() as conn:
        conn.execute(
            "UPDATE experiments SET snapshot=?, job_name=? WHERE id=?",
            (json.dumps(snapshot, ensure_ascii=False), job_name, experiment_id),
        )
        conn.executemany(
            "INSERT OR IGNORE INTO experiment_tasks(experiment_id, row_id, status) VALUES (?,?, 'pending')",
            [(experiment_id, r["row_id"]) for r in rows],
        )

    # 5. Launch run.sh.
    process = harbor_adapter.start_run(
        run_command, config.REPORTS_DIR / f"run-{experiment_id}.log"
    )
    _RUNNING[experiment_id] = process
    return_code = process.wait()
    _RUNNING.pop(experiment_id, None)

    # 6. Finalize per-task results and scores.
    summaries = harbor_adapter.read_trial_summaries(job_name)
    with connect() as conn:
        for summary in summaries:
            score_data = harbor_adapter.read_trial_scores(summary["trial_path"])
            aggregate, per_scorer = _split_score_data(score_data)
            status = summary["status"]
            if status == "done" and aggregate and aggregate.get("error"):
                status = "judge_failed"
            conn.execute(
                "UPDATE experiment_tasks SET harbor_trial_name=?, harbor_trial_path=?, status=?,"
                " duration_ms=?, n_input_tokens=?, n_output_tokens=?"
                " WHERE experiment_id=? AND row_id=?",
                (
                    summary["trial_name"],
                    summary["trial_path"],
                    status,
                    summary["duration_ms"],
                    summary["n_input_tokens"],
                    summary["n_output_tokens"],
                    experiment_id,
                    summary["row_id"],
                ),
            )
            task_row = conn.execute(
                "SELECT id FROM experiment_tasks WHERE experiment_id=? AND row_id=?",
                (experiment_id, summary["row_id"]),
            ).fetchone()
            if not task_row:
                continue
            task_id = task_row["id"]
            if aggregate:
                _insert_score(conn, task_id, 0, aggregate)
            for version_id, data in per_scorer.items():
                _insert_score(conn, task_id, version_id, data)

    if return_code != 0:
        raise ExperimentError(f"run.sh 退出码 {return_code}，日志见 workspace/reports/run-{experiment_id}.log")
    with connect() as conn:
        conn.execute(
            "UPDATE experiments SET status='completed', finished_at=? WHERE id=?",
            (utc_now(), experiment_id),
        )


def _split_score_data(score_data: dict | None) -> tuple[dict | None, dict[int, dict]]:
    if not score_data:
        return None, {}
    if "aggregate" in score_data:
        per_scorer: dict[int, dict] = {}
        for key, data in (score_data.get("results") or {}).items():
            try:
                per_scorer[int(key)] = data
            except (TypeError, ValueError):
                continue
        return score_data["aggregate"], per_scorer
    return score_data, {}


def _insert_score(conn, task_id: int, scorer_version_id: int, data: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO scores(experiment_task_id, scorer_version_id,"
        " value, verdict, failure_category, reason, created_at) VALUES (?,?,?,?,?,?,?)",
        (
            task_id,
            scorer_version_id,
            float(data.get("score", 0.0)),
            str(data.get("verdict", "fail")),
            data.get("failure_category"),
            json.dumps(data, ensure_ascii=False),
            utc_now(),
        ),
    )


def _archive_job_config(bench_path: Path, profile, out_path: Path) -> None:
    result = subprocess.run(
        [
            str(config.PYTHON),
            str(config.RENDER_JOB_PY),
            "--bench-file", str(bench_path),
            "--harness", profile["harness"],
            "--model", profile["model_id"],
            "--output", str(out_path),
        ],
        cwd=config.REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ExperimentError(f"render-job.py 失败：{result.stderr.strip() or result.stdout.strip()}")


def _git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=config.REPO_ROOT, capture_output=True, text=True, check=True,
        ).stdout.strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def _harbor_version() -> str:
    try:
        return subprocess.run(
            [str(config.PYTHON), "-c", "import harbor; print(getattr(harbor, '__version__', '0.24.0'))"],
            cwd=config.REPO_ROOT, capture_output=True, text=True, check=True,
        ).stdout.strip() or "0.24.0"
    except Exception:  # noqa: BLE001
        return "unknown"
