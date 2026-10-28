"""CSV parsing and validation for console datasets."""

from __future__ import annotations

import csv
import io
import json

REQUIRED_COLUMNS = ("id", "input")
OPTIONAL_COLUMNS = ("expected", "tags", "metadata", "rubric")
ROW_FIELDS = ("row_id", "input", "expected", "tags", "metadata", "rubric")


def validate_rows(rows: list[dict]) -> list[str]:
    """Validate row dicts; return row-level error messages (empty = valid)."""
    errors: list[str] = []
    seen_ids: set[str] = set()
    for index, row in enumerate(rows, start=1):
        row_id = str(row.get("row_id") or "").strip()
        input_text = str(row.get("input") or "").strip()
        if not row_id:
            errors.append(f"第 {index} 行：id 为空")
            continue
        if row_id in seen_ids:
            errors.append(f"第 {index} 行：id 重复（{row_id}）")
            continue
        if not input_text:
            errors.append(f"第 {index} 行：input 为空")
            continue
        metadata = row.get("metadata")
        if isinstance(metadata, str):
            metadata_raw = metadata.strip()
            metadata = {}
            if metadata_raw:
                try:
                    parsed = json.loads(metadata_raw)
                    if not isinstance(parsed, dict):
                        raise ValueError("metadata must be a JSON object")
                    metadata = parsed
                except (json.JSONDecodeError, ValueError) as exc:
                    errors.append(f"第 {index} 行：metadata 不是合法 JSON 对象（{exc}）")
                    continue
        elif metadata is None:
            metadata = {}
        elif not isinstance(metadata, dict):
            errors.append(f"第 {index} 行：metadata 不是 JSON 对象")
            continue
        seen_ids.add(row_id)
    if not rows and not errors:
        errors.append("没有数据行")
    return errors


def normalize_rows(rows: list[dict]) -> list[dict]:
    """Coerce row dicts into the canonical dataset row shape."""
    result = []
    for row in rows:
        metadata = row.get("metadata")
        if isinstance(metadata, str):
            metadata = json.loads(metadata) if metadata.strip() else {}
        tags = row.get("tags")
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        result.append(
            {
                "row_id": str(row.get("row_id") or "").strip(),
                "input": str(row.get("input") or "").strip(),
                "expected": str(row.get("expected") or "").strip(),
                "tags": list(tags or []),
                "metadata": dict(metadata or {}),
                "rubric": str(row.get("rubric") or "").strip(),
            }
        )
    return result


def parse_csv(text: str) -> tuple[list[dict], list[str]]:
    """Return (rows, errors). Rows are valid only when errors is empty."""
    errors: list[str] = []
    rows: list[dict] = []

    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        return [], ["CSV 内容为空"]

    headers = [h.strip() for h in reader.fieldnames if h]
    missing = [c for c in REQUIRED_COLUMNS if c not in headers]
    if missing:
        return [], [f"缺少必需列：{', '.join(missing)}"]
    unknown = [h for h in headers if h not in REQUIRED_COLUMNS + OPTIONAL_COLUMNS]
    if unknown:
        errors.append(f"未知列将被忽略：{', '.join(unknown)}")

    seen_ids: set[str] = set()
    for line_no, raw in enumerate(reader, start=2):
        row_id = (raw.get("id") or "").strip()
        input_text = (raw.get("input") or "").strip()
        if not row_id:
            errors.append(f"第 {line_no} 行：id 为空")
            continue
        if row_id in seen_ids:
            errors.append(f"第 {line_no} 行：id 重复（{row_id}）")
            continue
        if not input_text:
            errors.append(f"第 {line_no} 行：input 为空")
            continue
        metadata_raw = (raw.get("metadata") or "").strip()
        metadata: dict = {}
        if metadata_raw:
            try:
                parsed = json.loads(metadata_raw)
                if not isinstance(parsed, dict):
                    raise ValueError("metadata must be a JSON object")
                metadata = parsed
            except (json.JSONDecodeError, ValueError) as exc:
                errors.append(f"第 {line_no} 行：metadata 不是合法 JSON 对象（{exc}）")
                continue
        tags_raw = (raw.get("tags") or "").strip()
        tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
        seen_ids.add(row_id)
        rows.append(
            {
                "row_id": row_id,
                "input": input_text,
                "expected": (raw.get("expected") or "").strip(),
                "tags": tags,
                "metadata": metadata,
                "rubric": (raw.get("rubric") or "").strip(),
            }
        )

    if not rows and not errors:
        errors.append("CSV 没有数据行")
    return rows, errors
