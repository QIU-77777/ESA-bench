#!/usr/bin/env python3
"""Render bench, harness, model, and job layers into a Harbor job YAML."""

from __future__ import annotations

import argparse
import copy
import os
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]


def load_yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text()) or {}
    if not isinstance(value, dict):
        raise ValueError(f"configuration must be a mapping: {path}")
    return value


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def required_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise ValueError(f"environment variable {name} is missing or empty")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bench", required=True)
    parser.add_argument("--harness", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--job-config")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    provider, separator, model_id = args.model.partition("/")
    if not separator or not provider or not model_id:
        raise ValueError("model must use provider/model-id format")

    bench_path = ROOT / "configs" / "bench" / f"{args.bench}.yaml"
    harness_path = ROOT / "configs" / "harnesses" / f"{args.harness}.yaml"
    model_path = ROOT / "configs" / "models" / provider / f"{model_id}.yaml"
    for path in (bench_path, harness_path, model_path):
        if not path.is_file():
            raise FileNotFoundError(f"configuration not found: {path}")

    bench = deep_merge(load_yaml(ROOT / "configs" / "bench" / "_default.yaml"), load_yaml(bench_path))
    harness = load_yaml(harness_path)
    model = load_yaml(model_path)
    job = load_yaml(Path(args.job_config)) if args.job_config else {}

    if model.get("id") != args.model:
        raise ValueError(f"model file {model_path} declares id {model.get('id')!r}, expected {args.model!r}")
    if harness.get("id") != args.harness:
        raise ValueError(f"harness file {harness_path} declares id {harness.get('id')!r}, expected {args.harness!r}")

    protocol = str(harness.get("protocol", ""))
    protocol_config = (model.get("protocols") or {}).get(protocol)
    if not isinstance(protocol_config, dict):
        raise ValueError(f"model {args.model} does not declare the {protocol} protocol")
    model_id_env = str(protocol_config.get("model_id_env", ""))
    base_url_env = str(protocol_config.get("base_url_env", ""))
    api_key_env = str(model.get("api_key_env", ""))
    if not model_id_env or not base_url_env or not api_key_env:
        raise ValueError(f"model {args.model} has incomplete {protocol} connection metadata")
    if required_env(model_id_env) != model_id:
        raise ValueError(f"{model_id_env} does not equal {model_id!r}")
    required_env(base_url_env)
    required_env(api_key_env)

    manifest = load_yaml(ROOT / str(harness["manifest"]))
    environment = {
        "type": "docker",
        "import_path": "harbor_test_env:HarborTestDockerEnvironment",
        "force_build": True,
        "delete": True,
        "mounts": [
            {
                "type": "image",
                "source": f"{manifest['image_repository']}:{manifest['version']}",
                "target": manifest["mount_path"],
                "read_only": True,
            }
        ],
    }

    config: dict[str, Any] = {
        "model": args.model,
        "harness": args.harness,
        "jobs_dir": bench.get("jobs_dir", "jobs"),
        "n_attempts": bench.get("n_attempts", 1),
        "n_concurrent_trials": bench.get("n_concurrent_trials", 4),
        "timeout_multiplier": bench.get("timeout_multiplier", 1.0),
        "environment": environment,
        "agents": [
            {
                "name": harness["name"],
                "import_path": harness["import_path"],
                "model_name": args.model,
                "kwargs": copy.deepcopy(harness.get("kwargs") or {}),
            }
        ],
    }
    if args.harness == "opencode":
        config["agents"][0]["kwargs"] = {
            "opencode_config": {
                "provider": {
                    "gateway": {
                        "npm": "@ai-sdk/openai-compatible",
                        "name": provider,
                        "options": {
                            "baseURL": "{env:OPENAI_BASE_URL}",
                            "apiKey": "{env:OPENAI_API_KEY}",
                        },
                    }
                }
            }
        }
    for key in ("tasks", "datasets"):
        if key in bench:
            config[key] = copy.deepcopy(bench[key])

    if args.job_config:
        excluded = {"id", "bench", "harness", "model"}
        config = deep_merge(config, {k: v for k, v in job.items() if k not in excluded})
        config["model"] = args.model
        config["harness"] = args.harness
        config["agents"] = [
            {
                "name": harness["name"],
                "import_path": harness["import_path"],
                "model_name": args.model,
                "kwargs": copy.deepcopy(harness.get("kwargs") or {}),
            }
        ]
        if args.harness == "opencode":
            config["agents"][0]["kwargs"] = {
                "opencode_config": {
                    "provider": {
                        "gateway": {
                            "npm": "@ai-sdk/openai-compatible",
                            "name": provider,
                            "options": {
                                "baseURL": "{env:OPENAI_BASE_URL}",
                                "apiKey": "{env:OPENAI_API_KEY}",
                            },
                        }
                    }
                }
            }
        config["environment"] = environment

    args.output.write_text(yaml.safe_dump(config, sort_keys=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, ValueError, KeyError) as exc:
        raise SystemExit(f"ERROR: {exc}")
