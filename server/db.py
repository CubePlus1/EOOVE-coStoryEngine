import sqlite3
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS world (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  phase TEXT NOT NULL DEFAULT 'running',
  repair_count INTEGER NOT NULL DEFAULT 0,
  finale_target INTEGER NOT NULL DEFAULT 10
);

CREATE TABLE IF NOT EXISTS admin_settings (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  story_background TEXT NOT NULL DEFAULT '',
  beat_interval_seconds INTEGER NOT NULL DEFAULT 30,
  generation_paused INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS templates (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  profile TEXT NOT NULL,
  tags_json TEXT NOT NULL,
  used INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS characters (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  profile TEXT NOT NULL,
  tags_json TEXT NOT NULL,
  origin TEXT NOT NULL DEFAULT '宇宙临时维修区',
  quirk TEXT NOT NULL DEFAULT '',
  type TEXT NOT NULL,
  status TEXT NOT NULL,
  email TEXT,
  ending TEXT,
  last_seen_story INTEGER NOT NULL DEFAULT 0,
  joined_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS batches (
  id TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  deadline_at INTEGER NOT NULL,
  story_id INTEGER
);

CREATE TABLE IF NOT EXISTS batch_members (
  batch_id TEXT NOT NULL,
  char_id TEXT NOT NULL,
  type TEXT NOT NULL,
  joined_at INTEGER NOT NULL,
  PRIMARY KEY (batch_id, char_id)
);

CREATE TABLE IF NOT EXISTS rules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  story_id INTEGER,
  text TEXT NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS stories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL DEFAULT 'repair',
  incident TEXT NOT NULL,
  segments_json TEXT NOT NULL,
  members_json TEXT NOT NULL,
  rule_id INTEGER,
  personal_json TEXT NOT NULL DEFAULT '{}',
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS print_queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS mail_queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  char_id TEXT NOT NULL,
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
        INSERT OR IGNORE INTO world (id, phase, repair_count, finale_target)
        VALUES (1, 'running', 0, 10)
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO admin_settings
        (id, story_background, beat_interval_seconds, generation_paused)
        VALUES (1, '', 30, 0)
        """
    )
    conn.commit()


def _archive_legacy_tables(conn):
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'world'"
    ).fetchone()
    if row is None:
        return
    columns = {
        item["name"]
        for item in conn.execute("PRAGMA table_info(world)").fetchall()
    }
    if {"phase", "repair_count", "finale_target"}.issubset(columns):
        return
    for table in [
        "world",
        "characters",
        "acts",
        "oracle_pool",
        "triples",
        "weave_queue",
        "print_queue",
        "mail_queue",
        "location_seed_queue",
        "inputs_log",
    ]:
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if exists is None:
            continue
        backup = f"{table}_v2_backup"
        suffix = 1
        while conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            (backup,),
        ).fetchone():
            suffix += 1
            backup = f"{table}_v2_backup_{suffix}"
        conn.execute(f"ALTER TABLE {table} RENAME TO {backup}")
    conn.commit()
