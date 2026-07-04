import sqlite3
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS world (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  legend_index INTEGER NOT NULL DEFAULT 0,
  legend_text TEXT NOT NULL,
  clue_count INTEGER NOT NULL DEFAULT 0,
  act_seq INTEGER NOT NULL DEFAULT 0
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
  type TEXT NOT NULL,
  status TEXT NOT NULL,
  email TEXT,
  ending TEXT,
  last_seen_act INTEGER NOT NULL DEFAULT 0,
  joined_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS acts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  seq INTEGER NOT NULL,
  type TEXT NOT NULL,
  narrative TEXT NOT NULL,
  chronicle TEXT,
  involved_json TEXT NOT NULL,
  personal_json TEXT,
  importance INTEGER NOT NULL,
  print_json TEXT,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS triples (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  subject TEXT,
  relation TEXT,
  object TEXT,
  act_id INTEGER
);

CREATE TABLE IF NOT EXISTS weave_queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  priority INTEGER NOT NULL DEFAULT 100,
  created_at INTEGER NOT NULL,
  started_at INTEGER,
  completed_at INTEGER
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


def initialize(conn, initial_legend):
    _archive_v1_tables(conn)
    conn.executescript(SCHEMA)
    conn.execute(
        """
        INSERT OR IGNORE INTO world (id, legend_index, legend_text, clue_count, act_seq)
        VALUES (1, 0, ?, 0, 0)
        """,
        (initial_legend,),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO admin_settings
        (id, story_background, beat_interval_seconds, generation_paused)
        VALUES (1, '', 30, 0)
        """
    )
    conn.commit()


def _archive_v1_tables(conn):
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'world'"
    ).fetchone()
    if row is None:
        return
    columns = {
        item["name"]
        for item in conn.execute("PRAGMA table_info(world)").fetchall()
    }
    if "legend_index" in columns:
        return
    for table in [
        "world",
        "characters",
        "acts",
        "oracle_pool",
        "triples",
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
        backup = f"{table}_v1_backup"
        suffix = 1
        while conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            (backup,),
        ).fetchone():
            suffix += 1
            backup = f"{table}_v1_backup_{suffix}"
        conn.execute(f"ALTER TABLE {table} RENAME TO {backup}")
    conn.commit()
