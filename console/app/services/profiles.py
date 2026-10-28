"""Evaluation profile service: harness + model + params, versioned."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from .. import config
from ..db import connect, rows_to_dicts, utc_now


class ProfileError(ValueError):
    pass


def available_harnesses() -> list[str]:
    harness_dir = config.REPO_ROOT / "configs" / "harnesses"
    return sorted(p.stem for p in harness_dir.glob("*.yaml"))


def available_models() -> list[str]:
    models_dir = config.REPO_ROOT / "configs" / "models"
    result = []
    for provider_dir in sorted(models_dir.iterdir()):
        if provider_dir.is_dir():
            for model_file in sorted(provider_dir.glob("*.yaml")):
                data = yaml.safe_load(model_file.read_text()) or {}
                if data.get("id"):
                    result.append(str(data["id"]))
    return result


def harness_defaults() -> dict[str, dict]:
    """Default run params (kwargs) per harness, from configs/harnesses/*.yaml."""
    harness_dir = config.REPO_ROOT / "configs" / "harnesses"
    result = {}
    for path in sorted(harness_dir.glob("*.yaml")):
        data = yaml.safe_load(path.read_text()) or {}
        result[path.stem] = dict(data.get("kwargs") or {})
    return result


def create_profile(name: str, harness: str, model_id: str, params: dict) -> int:
    if not name.strip():
        raise ProfileError("名称不能为空")
    if harness not in available_harnesses():
        raise ProfileError(f"未知 harness：{harness}")
    if model_id not in available_models():
        raise ProfileError(f"未知模型：{model_id}")
    with connect() as conn:
        prev = conn.execute(
            "SELECT MAX(version_no) AS v FROM eval_profiles WHERE name=?",
            (name.strip(),),
        ).fetchone()
        cur = conn.execute(
            "INSERT INTO eval_profiles(name, harness, model_id, params, version_no, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (name.strip(), harness, model_id, json.dumps(params, ensure_ascii=False),
             int(prev["v"] or 0) + 1, utc_now()),
        )
        return int(cur.lastrowid)


def list_profiles() -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM eval_profiles ORDER BY name, version_no DESC").fetchall()
        return rows_to_dicts(rows)


def get_profile(profile_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM eval_profiles WHERE id=?", (profile_id,)).fetchone()
        return dict(row) if row else None


def delete_profile(profile_id: int) -> None:
    """Delete a profile and its versions; refuse while experiments use it."""
    with connect() as conn:
        profile = conn.execute("SELECT * FROM eval_profiles WHERE id=?", (profile_id,)).fetchone()
        if profile is None:
            raise ProfileError(f"评测对象不存在：{profile_id}")
        referenced = conn.execute(
            "SELECT COUNT(*) AS n FROM experiments WHERE eval_profile_id=?",
            (profile_id,),
        ).fetchone()
        if referenced["n"]:
            raise ProfileError(
                f"评测对象 {profile['name']} 被 {referenced['n']} 个实验引用，先删除相关实验"
            )
        conn.execute("DELETE FROM eval_profiles WHERE id=?", (profile_id,))
