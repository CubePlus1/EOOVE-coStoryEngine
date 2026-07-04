import sqlite3
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS editions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  no INTEGER NOT NULL,
  phase TEXT NOT NULL,
  phase_ends_at INTEGER NOT NULL,
  started_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS agents (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  persona TEXT NOT NULL,
  stack TEXT NOT NULL,
  catchphrase TEXT NOT NULL,
  role TEXT NOT NULL,
  team_id TEXT,
  location TEXT NOT NULL,
  intent TEXT NOT NULL,
  talking INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS teams (
  id TEXT PRIMARY KEY,
  edition_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  member_ids_json TEXT NOT NULL,
  idea_id INTEGER
);

CREATE TABLE IF NOT EXISTS ideas (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  text TEXT NOT NULL,
  investor_name TEXT,
  email TEXT,
  receipt_no TEXT NOT NULL,
  status TEXT NOT NULL,
  team_id TEXT,
  current_form TEXT,
  progress INTEGER NOT NULL DEFAULT 0,
  current_bug TEXT,
  review TEXT,
  rank INTEGER,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id TEXT NOT NULL,
  text TEXT NOT NULL,
  importance INTEGER NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  edition_id INTEGER NOT NULL,
  location TEXT NOT NULL,
  agent_ids_json TEXT NOT NULL,
  lines_json TEXT NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  edition_id INTEGER NOT NULL,
  type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS project_tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  idea_id INTEGER NOT NULL,
  team_id TEXT NOT NULL,
  title TEXT NOT NULL,
  owner_agent_id TEXT NOT NULL,
  status TEXT NOT NULL,
  output TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS project_commits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  idea_id INTEGER NOT NULL,
  agent_id TEXT NOT NULL,
  message TEXT NOT NULL,
  diff_summary TEXT NOT NULL,
  artifact_id INTEGER,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  idea_id INTEGER NOT NULL,
  version INTEGER NOT NULL,
  type TEXT NOT NULL,
  title TEXT NOT NULL,
  summary TEXT NOT NULL,
  body TEXT NOT NULL,
  content_type TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_settings (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  story_background TEXT NOT NULL DEFAULT '',
  beat_interval_seconds INTEGER NOT NULL DEFAULT 25,
  generation_paused INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS print_queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS mail_queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  idea_id INTEGER,
  email TEXT NOT NULL,
  kind TEXT NOT NULL,
  in_world_reason TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS inputs_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  type TEXT,
  payload_json TEXT,
  verdict TEXT,
  created_at INTEGER
);
"""


def connect(db_path):
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def initialize(conn):
    _archive_legacy_tables(conn)
    conn.executescript(SCHEMA)
    conn.execute(
        """
        INSERT OR IGNORE INTO admin_settings
        (id, story_background, beat_interval_seconds, generation_paused)
        VALUES (1, '', 25, 0)
        """
    )
    conn.commit()


def _archive_legacy_tables(conn):
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'editions'"
    ).fetchone()
    if row is not None:
        return
    existing = [
        item["name"]
        for item in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    ]
    legacy_tables = [
        "world",
        "templates",
        "characters",
        "batches",
        "batch_members",
        "rules",
        "stories",
        "acts",
        "oracle_pool",
        "triples",
        "weave_queue",
        "print_queue",
        "mail_queue",
        "location_seed_queue",
        "inputs_log",
    ]
    for table in legacy_tables:
        if table not in existing:
            continue
        backup = f"{table}_pre_v4_backup"
        suffix = 1
        while conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            (backup,),
        ).fetchone():
            suffix += 1
            backup = f"{table}_pre_v4_backup_{suffix}"
        conn.execute(f"ALTER TABLE {table} RENAME TO {backup}")
    conn.commit()
