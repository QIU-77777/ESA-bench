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
"""


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
        conn.executescript(SCHEMA)


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict]:
    return [dict(row) for row in rows]
