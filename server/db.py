import sqlite3
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS world (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  cycle INTEGER NOT NULL DEFAULT 1,
  round INTEGER NOT NULL DEFAULT 0,
  hope INTEGER NOT NULL DEFAULT 40,
  status TEXT NOT NULL DEFAULT 'running'
);

CREATE TABLE IF NOT EXISTS characters (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  profile TEXT NOT NULL,
  type TEXT NOT NULL,
  status TEXT NOT NULL,
  location TEXT NOT NULL,
  email TEXT,
  ending TEXT,
  echo TEXT,
  cycle_joined INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS acts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cycle INTEGER NOT NULL,
  round INTEGER NOT NULL,
  location TEXT NOT NULL,
  type TEXT NOT NULL,
  narrative TEXT NOT NULL,
  chronicle TEXT,
  directive_json TEXT,
  involved_json TEXT NOT NULL,
  personal_json TEXT,
  importance INTEGER NOT NULL,
  hope_delta INTEGER NOT NULL,
  oracle_applied TEXT,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS oracle_pool (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  char_id TEXT NOT NULL,
  scope TEXT NOT NULL,
  text TEXT NOT NULL,
  round_submitted INTEGER NOT NULL,
  status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS triples (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  subject TEXT,
  relation TEXT,
  object TEXT,
  cycle INTEGER,
  round INTEGER
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

CREATE TABLE IF NOT EXISTS location_seed_queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  location TEXT NOT NULL,
  seed TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at INTEGER NOT NULL,
  consumed_at INTEGER
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
    conn.executescript(SCHEMA)
    conn.execute(
        """
        INSERT OR IGNORE INTO world (id, cycle, round, hope, status)
        VALUES (1, 1, 0, 40, 'running')
        """
    )
    conn.commit()
