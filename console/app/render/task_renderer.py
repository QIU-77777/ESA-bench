"""Render a dataset version into a Harbor tasks directory."""

from __future__ import annotations

import shutil
from pathlib import Path

from .. import config

STANDARD_DOCKERFILE = """\
FROM node:22-alpine
RUN apk add --no-cache bash curl coreutils python3 py3-pytest
WORKDIR /app
"""

# M1 ships a passing stub. Scoring moves to ConsoleVerifier in M2.
TEST_SH = """\
#!/bin/bash
# Stub verifier: scoring is provided by the console Scorer (M2).
echo 1 > /logs/verifier/reward.txt
"""

TASK_TOML = """\
schema_version = "1.4"
artifacts = []

[task]
name = "{task_name}"
version = "1.0.0"
description = "Console-rendered task from dataset {dataset_name} v{version_no}"
authors = []
keywords = ["console", "{dataset_name}"]

[metadata]

[verifier]
timeout_sec = 300.0
collect = []

[verifier.env]

[agent]
timeout_sec = 600.0

[environment]
network_mode = "public"
build_timeout_sec = 600.0
os = "linux"
mcp_servers = []

[environment.env]

[solution.env]
"""


def render_tasks(
    rows: list[dict],
    out_dir: Path,
    env_template: str,
    dataset_name: str,
    version_no: int,
) -> Path:
    """Write one Harbor task directory per row under out_dir/tasks/."""
    tasks_dir = out_dir / "tasks"
    if tasks_dir.exists():
        shutil.rmtree(tasks_dir)
    tasks_dir.mkdir(parents=True)

    for row in rows:
        task_dir = tasks_dir / row["row_id"]
        task_dir.mkdir()
        (task_dir / "instruction.md").write_text(row["input"])
        (task_dir / "task.toml").write_text(
            TASK_TOML.format(
                task_name=f"{dataset_name}/{row['row_id']}",
                dataset_name=dataset_name,
                version_no=version_no,
            )
        )
        tests_dir = task_dir / "tests"
        tests_dir.mkdir()
        test_sh = tests_dir / "test.sh"
        test_sh.write_text(TEST_SH)
        test_sh.chmod(0o755)

        env_dir = task_dir / "environment"
        if env_template == "esa":
            if not config.ESA_TASK_ENV_DIR.is_dir():
                raise FileNotFoundError(f"esa environment template missing: {config.ESA_TASK_ENV_DIR}")
            shutil.copytree(config.ESA_TASK_ENV_DIR, env_dir)
        else:
            env_dir.mkdir()
            (env_dir / "Dockerfile").write_text(STANDARD_DOCKERFILE)

    return tasks_dir
