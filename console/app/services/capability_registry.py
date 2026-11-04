"""Capability registry: read harness and environment definitions from the repo.

Everything the profile page shows comes from files users can edit:
configs/harnesses/*.yaml, harness/*/manifest.yaml, esa/agent.json,
environment/catalog.json, configs/tools.json, environment/manifest.json
and environment/seed-v1.json. This service only reads.
"""

from __future__ import annotations

import json

import yaml

from .. import config

_HARNESS_DIR = "configs/harnesses"
_MANIFEST_DIR = "harness"
_ESA_AGENT_JSON = "esa/agent.json"
_TOOL_CATALOG = "environment/catalog.json"
_TOOL_CONFIG = "configs/tools.json"
_ENV_MANIFEST = "environment/manifest.json"
_SEED_FILE = "environment/seed-v1.json"

_SEED_LABELS = {
    "pois": "兴趣点",
    "tracks": "曲库",
    "documents": "知识文档",
    "routes": "路线",
    "weather": "天气",
    "contacts": "联系人",
    "visions": "视觉描述",
    "services": "生活服务",
}

_STANDARD_ENV = {
    "id": "standard",
    "name": "standard（空容器）",
    "description": "node:22-alpine 基础容器，预装 bash、curl、python3、pytest。无 MCP 工具，Agent 通过 shell 直接操作文件系统。",
}


def available_harnesses() -> list[str]:
    from . import profiles

    return profiles.available_harnesses()


def available_environments() -> list[str]:
    return ["standard", "esa", "esa-cockpit"]


def harness_detail(harness_id: str) -> dict | None:
    """Aggregate a harness definition from its yaml, manifest and agent file."""
    path = config.REPO_ROOT / _HARNESS_DIR / f"{harness_id}.yaml"
    if not path.is_file():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return None

    docs = data.get("param_docs") or {}
    params = [
        {"key": key, "value": value, "doc": str(docs.get(key) or "")}
        for key, value in (data.get("kwargs") or {}).items()
    ]
    detail = {
        "id": harness_id,
        "name": str(data.get("name") or harness_id),
        "protocol": str(data.get("protocol") or ""),
        "import_path": str(data.get("import_path") or ""),
        "image": _read_manifest(harness_id),
        "params": params,
        "agent": _read_esa_agent() if harness_id == "esa" else None,
        "notes": [],
    }
    if detail["agent"] is None:
        detail["notes"].append(
            "该 harness 的提示词与工具列表在镜像内的 CLI 中，仓库侧没有定义文件；"
            "改造需要修改镜像并重新构建。"
        )
    return detail


def environment_detail(env_id: str) -> dict | None:
    """Aggregate an environment definition from its catalog, config and seed."""
    if env_id == "standard":
        return {
            **_STANDARD_ENV,
            "tools": {"total": 0, "enabled": 0, "items": []},
            "data": {"entities": [], "seed_sha256": ""},
            "version": "",
        }
    if env_id == "esa-cockpit":
        return _cockpit_environment()
    if env_id != "esa":
        return None

    catalog = _read_json(config.REPO_ROOT / _TOOL_CATALOG) or {}
    tool_config = _read_json(config.REPO_ROOT / _TOOL_CONFIG) or {}
    manifest = _read_json(config.REPO_ROOT / _ENV_MANIFEST) or {}
    seed = _read_json(config.REPO_ROOT / _SEED_FILE) or {}

    enabled_map = {
        str(item.get("name")): bool(item.get("enabled"))
        for item in tool_config.get("tools") or []
    }
    items = []
    for tool in catalog.get("tools") or []:
        name = str(tool.get("name") or "")
        if not name:
            continue
        items.append(
            {
                "name": name,
                "title": str(tool.get("title") or name),
                "description": str(tool.get("description") or ""),
                "enabled": enabled_map.get(name, False),
            }
        )
    entities = [
        {"type": key, "label": label, "count": len(seed.get(key) or [])}
        for key, label in _SEED_LABELS.items()
        if seed.get(key)
    ]
    return {
        "id": "esa",
        "name": "esa（座舱环境）",
        "description": (
            "本地模拟座舱：mock-system 工具服务、agent-api runtime 与 Redis 会话状态。"
            "工具查询与写入真实会话状态，失败调用回滚。"
        ),
        "tools": {
            "total": len(items),
            "enabled": sum(1 for item in items if item["enabled"]),
            "entries": items,
        },
        "data": {
            "entities": entities,
            "seed_sha256": str(manifest.get("seed_sha256") or ""),
        },
        "version": str(manifest.get("environment_version") or ""),
    }


def _cockpit_environment() -> dict:
    """Cockpit stack card: recorder and MCP server runtime information.

    The tool list lives in the user MCP server, so the card reads the running
    recorder's manifest. Without the dev stack the card still describes the
    chain; tool entries stay empty until the recorder answers.
    """
    manifest = _read_runtime_json("http://127.0.0.1:18093/eval/api/v1/manifest")
    entries = []
    for tool in manifest.get("tools") or []:
        name = str(tool.get("name") or "")
        if not name:
            continue
        entries.append({"name": name, "title": name,
                        "description": str(tool.get("description") or ""),
                        "enabled": bool(tool.get("enabled", True))})
    return {
        "id": "esa-cockpit",
        "name": "esa-cockpit（座舱 agent 链路）",
        "description": (
            "座舱评测链路：mock-system 是 MCP 记录代理，cockpit-mcp 是用户 MCP server"
            "（工具与座舱状态），cockpit-agent 是被测座舱 agent。记录代理执行工具权限、"
            "故障注入与逐事件状态差异，证据与评分和 esa 环境一致。"
        ),
        "tools": {"total": len(entries), "enabled": sum(1 for item in entries if item["enabled"]),
                  "entries": entries},
        "data": {"entities": [], "seed_sha256": str(manifest.get("seed_sha256") or "")},
        "version": str(manifest.get("server_version") or ""),
    }


def _read_runtime_json(url: str) -> dict:
    """Best-effort read of a running service; the card stays usable without it."""
    try:
        import urllib.request

        with urllib.request.urlopen(url, timeout=2) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_manifest(harness_id: str) -> dict:
    path = config.REPO_ROOT / _MANIFEST_DIR / harness_id / "manifest.yaml"
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    return {
        "repository": str(data.get("image_repository") or ""),
        "version": str(data.get("version") or ""),
        "mount_path": str(data.get("mount_path") or ""),
        "npm_package": str(data.get("npm_package") or ""),
    }


def _read_esa_agent() -> dict | None:
    data = _read_json(config.REPO_ROOT / _ESA_AGENT_JSON)
    if not data:
        return None
    react = data.get("react") or {}
    model = react.get("model") or {}
    return {
        "display_name": str(data.get("display_name") or ""),
        "description": str(data.get("description") or ""),
        "capabilities": [str(item) for item in data.get("capabilities") or []],
        "instructions": str(react.get("instructions") or ""),
        "model_defaults": {
            key: model[key]
            for key in ("model_id", "temperature", "top_p", "reasoning_effort")
            if key in model
        },
        "context_continuity": str(react.get("context_continuity") or ""),
    }


def _read_json(path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
