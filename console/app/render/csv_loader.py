"""CSV parsing and validation for console datasets."""

from __future__ import annotations

import csv
import io
import json

REQUIRED_COLUMNS = ("id", "input")
OPTIONAL_COLUMNS = ("expected", "tags", "metadata", "rubric")


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
