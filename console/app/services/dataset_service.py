"""Dataset domain service: create datasets, save immutable versions."""

from __future__ import annotations

import csv
import hashlib
import io
import json

from ..db import connect, rows_to_dicts, utc_now
from ..render.csv_loader import parse_csv

VALID_ENV_TEMPLATES = ("standard", "esa")


class DatasetError(ValueError):
    pass


def content_hash(rows: list[dict]) -> str:
    canonical = json.dumps(
        [
            {
                "row_id": r["row_id"],
                "input": r["input"],
                "expected": r["expected"],
                "tags": r["tags"],
                "metadata": r["metadata"],
                "rubric": r["rubric"],
            }
            for r in sorted(rows, key=lambda r: r["row_id"])
        ],
        ensure_ascii=False,
        sort_keys=True,
    )
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def create_dataset(name: str, description: str, env_template: str) -> int:
    if not name.strip():
        raise DatasetError("数据集名称不能为空")
    if env_template not in VALID_ENV_TEMPLATES:
        raise DatasetError(f"未知任务环境模板：{env_template}")
    with connect() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO datasets(name, description, env_template, created_at) VALUES (?,?,?,?)",
                (name.strip(), description.strip(), env_template, utc_now()),
            )
        except Exception as exc:
            raise DatasetError(f"数据集名称已存在：{name}") from exc
        return int(cur.lastrowid)


def add_version(dataset_id: int, csv_text: str, change_note: str) -> int:
    rows, errors = parse_csv(csv_text)
    if errors:
        raise DatasetError("CSV 校验失败：\n" + "\n".join(errors))
    with connect() as conn:
        dataset = conn.execute("SELECT * FROM datasets WHERE id=?", (dataset_id,)).fetchone()
        if dataset is None:
            raise DatasetError(f"数据集不存在：{dataset_id}")
        prev = conn.execute(
            "SELECT MAX(version_no) AS v FROM dataset_versions WHERE dataset_id=?",
            (dataset_id,),
        ).fetchone()
        version_no = int(prev["v"] or 0) + 1
        cur = conn.execute(
            "INSERT INTO dataset_versions(dataset_id, version_no, change_note, content_hash, row_count, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (dataset_id, version_no, change_note.strip(), content_hash(rows), len(rows), utc_now()),
        )
        version_id = int(cur.lastrowid)
        conn.executemany(
            "INSERT INTO dataset_rows(dataset_version_id, row_id, input, expected, tags, metadata, rubric)"
            " VALUES (?,?,?,?,?,?,?)",
            [
                (
                    version_id,
                    r["row_id"],
                    r["input"],
                    r["expected"],
                    json.dumps(r["tags"], ensure_ascii=False),
                    json.dumps(r["metadata"], ensure_ascii=False),
                    r["rubric"],
                )
                for r in rows
            ],
        )
        return version_id


def list_datasets() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT d.*, "
            "(SELECT v.row_count FROM dataset_versions v WHERE v.dataset_id = d.id"
            " ORDER BY v.version_no DESC LIMIT 1) AS row_count, "
            "COALESCE((SELECT MAX(v.created_at) FROM dataset_versions v WHERE v.dataset_id = d.id), '') AS last_changed"
            " FROM datasets d ORDER BY d.id DESC"
        ).fetchall()
        return rows_to_dicts(rows)


def get_dataset(dataset_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM datasets WHERE id=?", (dataset_id,)).fetchone()
        return dict(row) if row else None


def add_rows_version(dataset_id: int, rows: list[dict], change_note: str) -> int:
    """Save edited rows as a new immutable version."""
    from ..render.csv_loader import normalize_rows, validate_rows

    errors = validate_rows(rows)
    if errors:
        raise DatasetError("行数据校验失败：\n" + "\n".join(errors))
    normalized = normalize_rows(rows)
    with connect() as conn:
        dataset = conn.execute("SELECT * FROM datasets WHERE id=?", (dataset_id,)).fetchone()
        if dataset is None:
            raise DatasetError(f"数据集不存在：{dataset_id}")
        prev = conn.execute(
            "SELECT MAX(version_no) AS v FROM dataset_versions WHERE dataset_id=?",
            (dataset_id,),
        ).fetchone()
        version_no = int(prev["v"] or 0) + 1
        cur = conn.execute(
            "INSERT INTO dataset_versions(dataset_id, version_no, change_note, content_hash, row_count, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (dataset_id, version_no, change_note.strip(), content_hash(normalized),
             len(normalized), utc_now()),
        )
        version_id = int(cur.lastrowid)
        conn.executemany(
            "INSERT INTO dataset_rows(dataset_version_id, row_id, input, expected, tags, metadata, rubric)"
            " VALUES (?,?,?,?,?,?,?)",
            [
                (
                    version_id,
                    r["row_id"],
                    r["input"],
                    r["expected"],
                    json.dumps(r["tags"], ensure_ascii=False),
                    json.dumps(r["metadata"], ensure_ascii=False),
                    r["rubric"],
                )
                for r in normalized
            ],
        )
        return version_id


def list_versions(dataset_id: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM dataset_versions WHERE dataset_id=? ORDER BY version_no DESC",
            (dataset_id,),
        ).fetchall()
        return rows_to_dicts(rows)


def get_version(version_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM dataset_versions WHERE id=?", (version_id,)).fetchone()
        return dict(row) if row else None


def list_rows(version_id: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM dataset_rows WHERE dataset_version_id=? ORDER BY row_id",
            (version_id,),
        ).fetchall()
        return rows_to_dicts(rows)


def update_dataset(dataset_id: int, name: str, description: str, env_template: str) -> None:
    if not name.strip():
        raise DatasetError("数据集名称不能为空")
    if env_template not in VALID_ENV_TEMPLATES:
        raise DatasetError(f"未知任务环境模板：{env_template}")
    with connect() as conn:
        existing = conn.execute(
            "SELECT id FROM datasets WHERE name=? AND id<>?",
            (name.strip(), dataset_id),
        ).fetchone()
        if existing:
            raise DatasetError(f"数据集名称已存在：{name}")
        conn.execute(
            "UPDATE datasets SET name=?, description=?, env_template=? WHERE id=?",
            (name.strip(), description.strip(), env_template, dataset_id),
        )


def delete_dataset(dataset_id: int) -> None:
    """Delete a dataset with all versions; refuse while experiments use it."""
    with connect() as conn:
        dataset = conn.execute("SELECT * FROM datasets WHERE id=?", (dataset_id,)).fetchone()
        if dataset is None:
            raise DatasetError(f"数据集不存在：{dataset_id}")
        referenced = conn.execute(
            "SELECT COUNT(*) AS n FROM experiments e"
            " JOIN dataset_versions v ON v.id = e.dataset_version_id"
            " WHERE v.dataset_id=?",
            (dataset_id,),
        ).fetchone()
        if referenced["n"]:
            raise DatasetError(
                f"数据集 {dataset['name']} 被 {referenced['n']} 个实验引用，先删除相关实验"
            )
        version_ids = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM dataset_versions WHERE dataset_id=?", (dataset_id,)
            ).fetchall()
        ]
        for version_id in version_ids:
            conn.execute("DELETE FROM dataset_rows WHERE dataset_version_id=?", (version_id,))
        conn.execute("DELETE FROM dataset_versions WHERE dataset_id=?", (dataset_id,))
        conn.execute("DELETE FROM baselines WHERE dataset_id=?", (dataset_id,))
        conn.execute("DELETE FROM loop_drafts WHERE target_dataset_id=?", (dataset_id,))
        conn.execute("DELETE FROM datasets WHERE id=?", (dataset_id,))


def export_version_csv(version_id: int) -> str:
    version = get_version(version_id)
    if version is None:
        raise DatasetError(f"数据集版本不存在：{version_id}")
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "input", "expected", "tags", "metadata", "rubric"])
    for row in list_rows(version_id):
        writer.writerow(
            [
                row["row_id"],
                row["input"],
                row["expected"],
                ",".join(json.loads(row["tags"])),
                json.dumps(json.loads(row["metadata"]), ensure_ascii=False)
                if row["metadata"] != "{}"
                else "",
                row["rubric"],
            ]
        )
    return output.getvalue()


def save_as(
    dataset_id: int, name: str, description: str, env_template: str, rows: list[dict]
) -> int:
    """Save the given rows as a brand new dataset."""
    new_id = create_dataset(name, description, env_template)
    if rows:
        add_rows_version(new_id, rows, "另存数据集")
    return new_id
