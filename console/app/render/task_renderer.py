"""Render a dataset version into a Harbor tasks directory."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from .. import config
from esa.reference import SOLVE_SCRIPT

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
row_id = {row_id}
expected = {expected}
rubric = {rubric}
tags = {tags}

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


def _toml_literal(value):
    if isinstance(value, dict):
        return "{" + ", ".join(json.dumps(str(key), ensure_ascii=False) + " = " + _toml_literal(child) for key, child in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(_toml_literal(child) for child in value) + "]"
    if value is None:
        return '"null"'
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


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
                row_id=_toml_literal(row["row_id"]),
                expected=_toml_literal(row["expected"]),
                rubric=_toml_literal(row["rubric"]),
                tags=_toml_literal(row["tags"]),
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
            for name in ("score.py", "state_score.py"):
                shutil.copyfile(config.REPO_ROOT / "esa" / name, tests_dir / name)
            test_sh.write_text("#!/bin/bash\nset -euo pipefail\nmkdir -p /logs/verifier\nexport ESA_RUBRIC_PATH=/tests/rubric.json\npython3 /tests/score.py\n")
            initial_path = env_dir / "esa" / "initial_state.json"
            initial = json.loads(initial_path.read_text("utf8"))
            initial.update(case_id=row["row_id"], user_messages=[row["input"]])
            initial_path.write_text(json.dumps(initial, ensure_ascii=False, indent=2) + "\n", "utf8")
            solution_dir = task_dir / "solution"
            solution_dir.mkdir()
            shutil.copyfile(config.REPO_ROOT / "esa/reference.py", solution_dir / "reference_runtime.py")
            shutil.copyfile(config.REPO_ROOT / "esa/oracle.py", solution_dir / "runner.py")
            solve_script = solution_dir / "solve.sh"
            solve_script.write_text(SOLVE_SCRIPT)
            solve_script.chmod(0o755)
            (solution_dir / "reference.py").write_text("def solve(ctx):\n    raise NotImplementedError('author must provide a reference solution')\n")
            (tests_dir / "negative_tests.py").write_text("CASES = {}\n")
            # Drafts require an authored solution/reference.py and nonempty tests/negative_tests.py.
            (tests_dir / "rubric.json").write_text(json.dumps({"case_id": row["row_id"], **(row["rubric"] if isinstance(row["rubric"], dict) else {})}, ensure_ascii=False, indent=2) + "\n", "utf8")
        else:
            env_dir.mkdir()
            (env_dir / "Dockerfile").write_text(STANDARD_DOCKERFILE)

    return tasks_dir
