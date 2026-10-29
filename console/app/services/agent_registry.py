"""Local coding agent discovery: installed agents and their models."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

KNOWN_AGENTS = [
    ("opencode", "OpenCode"),
    ("pi", "Pi"),
    ("dsh", "DeepSeek Harness"),
    ("claude", "Claude Code"),
]

_LIST_TIMEOUT_SEC = 20


def detect_agents() -> list[dict]:
    """Agents installed on this machine, in KNOWN_AGENTS order."""
    return [
        {"id": agent_id, "name": name}
        for agent_id, name in KNOWN_AGENTS
        if shutil.which(agent_id) is not None
    ]


def list_models(agent: str) -> list[str]:
    """Model ids the agent can use, read from its own configuration."""
    if agent == "opencode":
        return _opencode_models()
    if agent == "pi":
        return _pi_models()
    if agent == "dsh":
        return _dsh_models()
    return []


def _opencode_models() -> list[str]:
    try:
        result = subprocess.run(
            ["opencode", "models"],
            capture_output=True,
            text=True,
            timeout=_LIST_TIMEOUT_SEC,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _pi_models() -> list[str]:
    models: list[str] = []
    for path in (Path.home() / ".pi" / "agent" / "models.json",):
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for provider, config in (data.get("providers") or {}).items():
            for model in config.get("models") or []:
                model_id = str(model.get("id") or "").strip()
                if model_id:
                    models.append(f"{provider}/{model_id}")
    return models


def _dsh_models() -> list[str]:
    """dsh has no model listing command; read its default agent model."""
    import yaml

    candidates = [
        Path.home() / ".dsh" / "settings.yaml",
        Path.home() / ".dsh" / "settings.yaml.imported",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        default = data.get("agent-default-model") or {}
        provider = str(default.get("provider") or "").strip()
        model = str(default.get("model") or "").strip()
        if model:
            return [f"{provider}/{model}" if provider else model]
    return []
