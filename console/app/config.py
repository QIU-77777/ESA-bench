"""Console path constants."""

import os
from pathlib import Path

CONSOLE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = CONSOLE_ROOT.parent

WORKSPACE = CONSOLE_ROOT / "workspace"
DB_PATH = WORKSPACE / "console.db"
GENERATED_DIR = WORKSPACE / "generated"
SNAPSHOTS_DIR = WORKSPACE / "snapshots"
REPORTS_DIR = WORKSPACE / "reports"

JOBS_DIR = REPO_ROOT / "jobs"
RUN_SH = REPO_ROOT / "scripts" / "run.sh"
RENDER_JOB_PY = REPO_ROOT / "scripts" / "render-job.py"
PYTHON = REPO_ROOT / ".venv" / "bin" / "python"
ESA_TASK_ENV_DIR = REPO_ROOT / "tasks" / "esa-sc-a1" / "environment"


def ensure_workspace() -> None:
    for path in (WORKSPACE, GENERATED_DIR, SNAPSHOTS_DIR, REPORTS_DIR):
        path.mkdir(parents=True, exist_ok=True)


def load_env(path: Path | None = None) -> None:
    """Load repo .env into os.environ (no override), mirroring run.sh."""
    env_path = path or (REPO_ROOT / ".env")
    if not env_path.is_file():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())
