"""Resolve model connection (endpoint + key) from configs/models and .env."""

from __future__ import annotations

import os
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]


def resolve_model(model_id: str) -> dict:
    """Return {protocol, model, base_url, api_key} for a provider/model-id."""
    provider, _, slug = model_id.partition("/")
    if not provider or not slug:
        raise ValueError(f"model_id 必须是 provider/model-id 形式：{model_id!r}")
    path = REPO_ROOT / "configs" / "models" / provider / f"{slug}.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"模型配置不存在：{path}")
    data = yaml.safe_load(path.read_text()) or {}
    protocols = data.get("protocols") or {}

    for protocol in ("openai", "anthropic"):
        entry = protocols.get(protocol)
        if not isinstance(entry, dict):
            continue
        base_url = os.environ.get(str(entry.get("base_url_env", "")), "")
        api_key = os.environ.get(str(data.get("api_key_env", "")), "")
        if not base_url or not api_key:
            raise ValueError(
                f"模型 {model_id} 的连接变量缺失：{entry.get('base_url_env')} / {data.get('api_key_env')}"
            )
        return {
            "protocol": protocol,
            "model": slug,
            "base_url": base_url.rstrip("/"),
            "api_key": api_key,
        }
    raise ValueError(f"模型 {model_id} 没有可用的 openai / anthropic 协议配置")
