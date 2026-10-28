"""SQLite storage layer (standard library only)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  env_template TEXT NOT NULL DEFAULT 'standard',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dataset_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  dataset_id INTEGER NOT NULL REFERENCES datasets(id),
  version_no INTEGER NOT NULL,
  change_note TEXT NOT NULL DEFAULT '',
  content_hash TEXT NOT NULL,
  row_count INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(dataset_id, version_no)
);
CREATE TABLE IF NOT EXISTS dataset_rows (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  dataset_version_id INTEGER NOT NULL REFERENCES dataset_versions(id),
  row_id TEXT NOT NULL,
  input TEXT NOT NULL,
  expected TEXT NOT NULL DEFAULT '',
  tags TEXT NOT NULL DEFAULT '[]',
  metadata TEXT NOT NULL DEFAULT '{}',
  rubric TEXT NOT NULL DEFAULT '',
  UNIQUE(dataset_version_id, row_id)
);
CREATE TABLE IF NOT EXISTS eval_profiles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  harness TEXT NOT NULL,
  model_id TEXT NOT NULL,
  params TEXT NOT NULL DEFAULT '{}',
  version_no INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(name, version_no)
);
CREATE TABLE IF NOT EXISTS experiments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft',
  dataset_version_id INTEGER REFERENCES dataset_versions(id),
  eval_profile_id INTEGER REFERENCES eval_profiles(id),
  snapshot TEXT NOT NULL DEFAULT '{}',
  baseline_flag INTEGER NOT NULL DEFAULT 0,
  error TEXT NOT NULL DEFAULT '',
  job_name TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT
);
CREATE TABLE IF NOT EXISTS experiment_tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  experiment_id INTEGER NOT NULL REFERENCES experiments(id),
  row_id TEXT NOT NULL,
  harbor_trial_name TEXT NOT NULL DEFAULT '',
  harbor_trial_path TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',
  duration_ms INTEGER,
  n_input_tokens INTEGER,
  n_output_tokens INTEGER,
  UNIQUE(experiment_id, row_id)
);
CREATE TABLE IF NOT EXISTS scorers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  type TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scorer_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scorer_id INTEGER NOT NULL REFERENCES scorers(id),
  version_no INTEGER NOT NULL,
  config TEXT NOT NULL DEFAULT '{}',
  change_note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  UNIQUE(scorer_id, version_no)
);
CREATE TABLE IF NOT EXISTS scorer_tryouts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scorer_draft TEXT NOT NULL DEFAULT '{}',
  trace_ref TEXT NOT NULL DEFAULT '',
  output TEXT NOT NULL DEFAULT '{}',
  duration_ms INTEGER,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scores (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  experiment_task_id INTEGER NOT NULL REFERENCES experiment_tasks(id),
  scorer_version_id INTEGER NOT NULL DEFAULT 0,
  value REAL NOT NULL DEFAULT 0,
  verdict TEXT NOT NULL DEFAULT 'fail',
  failure_category TEXT,
  reason TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  UNIQUE(experiment_task_id, scorer_version_id)
);
CREATE TABLE IF NOT EXISTS experiment_scorers (
  experiment_id INTEGER NOT NULL REFERENCES experiments(id),
  scorer_version_id INTEGER NOT NULL REFERENCES scorer_versions(id),
  PRIMARY KEY(experiment_id, scorer_version_id)
);
CREATE TABLE IF NOT EXISTS reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  experiment_task_id INTEGER NOT NULL UNIQUE REFERENCES experiment_tasks(id),
  dims TEXT NOT NULL DEFAULT '{}',
  overall_verdict TEXT NOT NULL DEFAULT 'suspect',
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS baselines (
  dataset_id INTEGER NOT NULL UNIQUE,
  experiment_id INTEGER NOT NULL REFERENCES experiments(id),
  set_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  review_id INTEGER NOT NULL REFERENCES reviews(id),
  dims TEXT NOT NULL DEFAULT '{}',
  overall_verdict TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS loop_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  experiment_id INTEGER NOT NULL REFERENCES experiments(id),
  status TEXT NOT NULL DEFAULT 'running',
  agent TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL DEFAULT '',
  work_dir TEXT NOT NULL DEFAULT '',
  report TEXT NOT NULL DEFAULT '',
  error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  finished_at TEXT
);
CREATE TABLE IF NOT EXISTS loop_drafts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  loop_run_id INTEGER NOT NULL REFERENCES loop_runs(id),
  name TEXT NOT NULL DEFAULT '',
  rows TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'draft',
  target_dataset_id INTEGER REFERENCES datasets(id),
  created_at TEXT NOT NULL,
  confirmed_at TEXT
);
CREATE TABLE IF NOT EXISTS failure_categories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  created_at TEXT NOT NULL
);
"""

_MIGRATIONS = (
    "ALTER TABLE experiments ADD COLUMN scorer_version_id INTEGER REFERENCES scorer_versions(id)",
    "ALTER TABLE scorers ADD COLUMN description TEXT NOT NULL DEFAULT ''",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    config.ensure_workspace()
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with connect() as conn:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='scores'"
        ).fetchone()
        if row and "UNIQUE(experiment_task_id)" in row["sql"]:
            conn.execute("ALTER TABLE scores RENAME TO scores_legacy")
            conn.execute(
                "CREATE TABLE scores ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " experiment_task_id INTEGER NOT NULL REFERENCES experiment_tasks(id),"
                " scorer_version_id INTEGER NOT NULL DEFAULT 0,"
                " value REAL NOT NULL DEFAULT 0,"
                " verdict TEXT NOT NULL DEFAULT 'fail',"
                " failure_category TEXT,"
                " reason TEXT NOT NULL DEFAULT '{}',"
                " created_at TEXT NOT NULL,"
                " UNIQUE(experiment_task_id, scorer_version_id))"
            )
            conn.execute(
                "INSERT INTO scores(experiment_task_id, scorer_version_id, value, verdict,"
                " failure_category, reason, created_at)"
                " SELECT experiment_task_id, COALESCE(scorer_version_id, 0), value, verdict,"
                " failure_category, reason, created_at FROM scores_legacy"
            )
            conn.execute("DROP TABLE scores_legacy")
        conn.executescript(SCHEMA)
        for statement in _MIGRATIONS:
            try:
                conn.execute(statement)
            except sqlite3.OperationalError:
                pass  # column already exists
        conn.execute(
            "INSERT OR IGNORE INTO experiment_scorers(experiment_id, scorer_version_id)"
            " SELECT id, scorer_version_id FROM experiments"
            " WHERE scorer_version_id IS NOT NULL"
        )


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict]:
    return [dict(row) for row in rows]
