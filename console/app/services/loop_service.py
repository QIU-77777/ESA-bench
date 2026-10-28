"""Loop-Assistant: failure analysis via an independent coding agent."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

from .. import config
from ..db import connect, rows_to_dicts, utc_now
from . import dataset_service, experiment_service, harbor_adapter
from .review_service import get_settings

PROMPT_TEMPLATE = config.REPO_ROOT / "prompts" / "loop_task.md"

MAX_TASKS = 25
OUTPUT_CHARS = 1500


class LoopError(ValueError):
    pass


def get_failures(experiment_id: int) -> list[dict]:
    """Failed tasks of an experiment enriched with trace context."""
    experiment = experiment_service.get_experiment(experiment_id)
    if experiment is None:
        raise LoopError(f"实验不存在：{experiment_id}")
    version = dataset_service.get_version(experiment["dataset_version_id"])
    rows = {r["row_id"]: r for r in dataset_service.list_rows(version["id"])}
    failures = []
    for task in experiment_service.list_tasks(experiment_id):
        if task["verdict"] == "pass":
            continue
        row = rows.get(task["row_id"], {})
        reason = {}
        try:
            reason = json.loads(task["score_reason"] or "{}")
        except json.JSONDecodeError:
            reason = {}
        output = ""
        if task["harbor_trial_path"]:
            _, output = harbor_adapter.read_trial_trace(task["harbor_trial_path"])
        failures.append(
            {
                "row_id": task["row_id"],
                "score": task["score"],
                "verdict": task["verdict"],
                "failure_category": task["failure_category"],
                "reason": reason,
                "input": row.get("input", ""),
                "expected": row.get("expected", ""),
                "tags": row.get("tags", "[]"),
                "output": output,
                "trial_path": task["harbor_trial_path"],
            }
        )
    failures.sort(key=lambda f: (f["score"] if f["score"] is not None else 2))
    return failures


def known_categories(experiment_id: int) -> list[str]:
    """Accumulated failure categories: registry plus categories seen in this experiment."""
    names: list[str] = []
    with connect() as conn:
        for row in conn.execute("SELECT name FROM failure_categories ORDER BY id").fetchall():
            names.append(row["name"])
        for row in conn.execute(
            "SELECT DISTINCT sc.failure_category AS c FROM scores sc"
            " JOIN experiment_tasks t ON t.id = sc.experiment_task_id"
            " WHERE t.experiment_id=? AND sc.failure_category IS NOT NULL",
            (experiment_id,),
        ).fetchall():
            for part in str(row["c"]).split(","):
                part = part.strip()
                if part and part not in names:
                    names.append(part)
    return names


def register_categories(names: list[str]) -> None:
    with connect() as conn:
        for name in names:
            name = str(name).strip()
            if name:
                conn.execute(
                    "INSERT OR IGNORE INTO failure_categories(name, created_at) VALUES (?,?)",
                    (name, utc_now()),
                )


def list_runs(experiment_id: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM loop_runs WHERE experiment_id=? ORDER BY id DESC", (experiment_id,)
        ).fetchall()
    return rows_to_dicts(rows)


def get_run(loop_run_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM loop_runs WHERE id=?", (loop_run_id,)).fetchone()
    if row is None:
        return None
    run = dict(row)
    run["report"] = json.loads(run["report"]) if run["report"] else None
    return run


def list_drafts(loop_run_id: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM loop_drafts WHERE loop_run_id=? ORDER BY id", (loop_run_id,)
        ).fetchall()
    result = []
    for row in rows_to_dicts(rows):
        row["rows"] = json.loads(row["rows"] or "[]")
        result.append(row)
    return result


def get_draft(loop_draft_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM loop_drafts WHERE id=?", (loop_draft_id,)).fetchone()
    if row is None:
        return None
    draft = dict(row)
    draft["rows"] = json.loads(draft["rows"] or "[]")
    return draft


def update_draft_rows(loop_draft_id: int, rows: list[dict]) -> None:
    draft = get_draft(loop_draft_id)
    if draft is None:
        raise LoopError(f"草案不存在：{loop_draft_id}")
    if draft["status"] != "draft":
        raise LoopError("该草案已合并，不能修改")
    with connect() as conn:
        conn.execute(
            "UPDATE loop_drafts SET rows=? WHERE id=?",
            (json.dumps(rows, ensure_ascii=False), loop_draft_id),
        )


def confirm_draft(loop_draft_id: int, target_dataset_id: int) -> int:
    """Merge a draft into a dataset as a new internal snapshot."""
    draft = get_draft(loop_draft_id)
    if draft is None:
        raise LoopError(f"草案不存在：{loop_draft_id}")
    if draft["status"] != "draft":
        raise LoopError("该草案已合并")
    if dataset_service.get_dataset(target_dataset_id) is None:
        raise LoopError(f"目标数据集不存在：{target_dataset_id}")
    run = get_run(draft["loop_run_id"])
    note = f"Loop-Assistant 草案，来源实验 exp-{run['experiment_id']}"
    rows = [
        {
            "row_id": str(row.get("row_id") or row.get("id") or "").strip(),
            "input": str(row.get("input") or ""),
            "expected": str(row.get("expected") or ""),
            "tags": row.get("tags") or "",
            "metadata": row.get("metadata") or "{}",
            "rubric": str(row.get("rubric") or ""),
        }
        for row in draft["rows"]
    ]
    version_id = dataset_service.add_rows_version(target_dataset_id, rows, note)
    with connect() as conn:
        conn.execute(
            "UPDATE loop_drafts SET status='merged', target_dataset_id=?, confirmed_at=? WHERE id=?",
            (target_dataset_id, utc_now(), loop_draft_id),
        )
    return version_id


def start_run(experiment_id: int) -> int:
    """Trigger a Loop-Assistant analysis for the experiment's failed tasks."""
    failures = get_failures(experiment_id)
    if not failures:
        raise LoopError("该实验没有失败任务")
    experiment = experiment_service.get_experiment(experiment_id)
    settings = get_settings()
    agent = str(settings.get("loop_agent") or "opencode")
    model = str(settings.get("loop_model") or "stepcode/gpt-5.6-sol")
    if shutil.which(agent) is None:
        raise LoopError(f"找不到分析代理命令：{agent}")
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO loop_runs(experiment_id, status, agent, model, created_at)"
            " VALUES (?, 'running', ?, ?, ?)",
            (experiment_id, agent, model, utc_now()),
        )
        run_id = int(cur.lastrowid)
        conn.execute(
            "UPDATE loop_runs SET work_dir=? WHERE id=?",
            (str(_run_dir(run_id).relative_to(config.REPO_ROOT)), run_id),
        )
    threading.Thread(target=_execute, args=(run_id,), daemon=True).start()
    return run_id


def _run_dir(run_id: int) -> Path:
    path = config.GENERATED_DIR / "loop" / f"run-{run_id}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _execute(run_id: int) -> None:
    try:
        _analyze(run_id)
    except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
        with connect() as conn:
            conn.execute(
                "UPDATE loop_runs SET status='failed', error=?, finished_at=? WHERE id=?",
                (str(exc), utc_now(), run_id),
            )


def _analyze(run_id: int) -> None:
    run = get_run(run_id)
    assert run is not None
    experiment_id = run["experiment_id"]
    experiment = experiment_service.get_experiment(experiment_id)
    failures = get_failures(experiment_id)
    categories = known_categories(experiment_id)
    work_dir = config.REPO_ROOT / run["work_dir"]
    brief_path = work_dir / "brief.md"
    brief_path.write_text(_build_brief(experiment, failures, categories), encoding="utf-8")

    instruction = (
        "按照简报要求完成失败分析。必须调用写文件工具，把完整 JSON 结果写入当前目录的"
        " report.json；不要在回复里粘贴 JSON。"
    )
    report = None
    last = None
    for attempt in range(2):
        command = _agent_command(run["agent"], run["model"], brief_path, instruction)
        env = {**os.environ, "PWD": str(work_dir)}
        last = subprocess.run(
            command,
            cwd=work_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=1800,
        )
        report = _read_report(work_dir, last.stdout)
        if report is not None:
            break
        instruction = (
            "上一次运行没有生成 report.json。现在立即把 JSON 结果写入当前目录的"
            " report.json 文件，写完后停止。"
        )
    if report is None:
        raise LoopError(
            "分析代理未产出 report.json。"
            f"退出码 {last.returncode if last else '?'}；stderr：{((last.stderr if last else '') or '')[:500]}"
        )
    report_path = work_dir / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    register_categories([c.get("name", "") for c in report.get("categories", [])])

    with connect() as conn:
        conn.execute(
            "UPDATE loop_runs SET status='done', report=?, finished_at=? WHERE id=?",
            (json.dumps(report, ensure_ascii=False), utc_now(), run_id),
        )
        rows = report.get("draft_rows") or []
        if rows:
            cur = conn.execute(
                "INSERT INTO loop_drafts(loop_run_id, name, rows, status, created_at)"
                " VALUES (?,?,?, 'draft', ?)",
                (
                    run_id,
                    f"exp-{experiment_id} 失败案例草案",
                    json.dumps(rows, ensure_ascii=False),
                    utc_now(),
                ),
            )
            _ = cur


def _agent_command(agent: str, model: str, brief_path: Path, instruction: str) -> list[str]:
    if agent == "opencode":
        return [
            "opencode", "run",
            "--model", model,
            "--format", "json",
            "--auto",
            "--file", str(brief_path),
            instruction,
        ]
    if agent == "pi":
        command = ["pi", "-p", "--mode", "json", "--no-session"]
        if "/" in model:
            provider, _, model_id = model.partition("/")
            command += ["--provider", provider, "--model", model_id]
        else:
            command += ["--model", model]
        command += [f"@{brief_path}", instruction]
        return command
    raise LoopError(f"不支持的分析代理：{agent}")


def _read_report(work_dir: Path, stdout: str) -> dict | None:
    report_path = work_dir / "report.json"
    if report_path.is_file():
        try:
            return json.loads(report_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    # Fallback: the agent may have printed the report as text instead of a file.
    # opencode --format json streams events; collect every text part and try to parse.
    candidates: list[str] = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        part = payload.get("part") or {}
        if isinstance(part, dict) and part.get("type") == "text":
            candidates.append(str(part.get("text") or ""))
        for key in ("text", "message", "output"):
            value = payload.get(key)
            if isinstance(value, str):
                candidates.append(value)
    candidates.append(stdout or "")
    for text in candidates:
        text = text.strip()
        if not text.startswith("{"):
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and ("categories" in parsed or "overall" in parsed):
            return parsed
    return None


def _build_brief(experiment: dict, failures: list[dict], categories: list[str]) -> str:
    template = ""
    if PROMPT_TEMPLATE.is_file():
        template = PROMPT_TEMPLATE.read_text(encoding="utf-8")
    parts = [template, "", "---", ""]
    parts.append("## 实验信息")
    parts.append(f"- 实验：{experiment['name']}（exp-{experiment['id']}）")
    parts.append(f"- 失败任务数：{len(failures)}")
    parts.append("")
    parts.append("## 已有失败类别")
    parts.append("、".join(categories) if categories else "（暂无，允许新建类别）")
    parts.append("")
    parts.append("## 失败任务明细")
    for index, failure in enumerate(failures[:MAX_TASKS], start=1):
        parts.append(f"### {index}. {failure['row_id']}")
        parts.append(f"- 分数：{failure['score']}；判定：{failure['verdict']}")
        parts.append(f"- 评分器失败类别：{failure['failure_category'] or '—'}")
        reason = failure.get("reason") or {}
        if isinstance(reason, dict):
            reason_text = reason.get("reason") or reason.get("summary") or json.dumps(reason, ensure_ascii=False)
        else:
            reason_text = str(reason)
        parts.append(f"- 评分依据：{str(reason_text)[:600]}")
        parts.append(f"- 任务输入：{str(failure['input'])[:800]}")
        parts.append(f"- 预期结果：{str(failure['expected'])[:500]}")
        parts.append(f"- Agent 输出（截断）：{str(failure['output'])[:OUTPUT_CHARS]}")
        parts.append("")
    return "\n".join(parts)
