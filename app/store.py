import sqlite3
from contextlib import contextmanager
from pathlib import Path
from app import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, source BLOB NOT NULL, book BLOB NOT NULL,
  result TEXT NOT NULL, created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
  run_id TEXT NOT NULL REFERENCES runs(id), version INTEGER NOT NULL,
  decision TEXT NOT NULL, note TEXT NOT NULL, changed TEXT NOT NULL,
  PRIMARY KEY(run_id,version)
);
CREATE TABLE IF NOT EXISTS credentials (
  run_id TEXT NOT NULL REFERENCES runs(id),
  credential_key TEXT NOT NULL,
  book_line INTEGER NOT NULL,
  amount TEXT NOT NULL,
  registered TEXT NOT NULL,
  PRIMARY KEY(run_id,credential_key)
);
CREATE TABLE IF NOT EXISTS claims (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  credential_key TEXT NOT NULL,
  run_id TEXT NOT NULL REFERENCES runs(id),
  status TEXT NOT NULL CHECK(status IN ('HELD','RETURNED')),
  request_id TEXT,
  note TEXT NOT NULL DEFAULT '',
  claimed_at TEXT NOT NULL,
  returned_at TEXT,
  return_reason TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS claim_active_once
  ON claims(credential_key) WHERE status='HELD';
CREATE TABLE IF NOT EXISTS claim_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  credential_key TEXT NOT NULL,
  run_id TEXT NOT NULL,
  action TEXT NOT NULL,
  reason_code TEXT NOT NULL DEFAULT '',
  detail TEXT NOT NULL,
  created TEXT NOT NULL
);
"""


@contextmanager
def connection():
    path = Path(settings.DB)
    path.parent.mkdir(parents=True,exist_ok=True)
    db = sqlite3.connect(path,timeout=10)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA foreign_keys = ON')
        db.executescript(SCHEMA)
        with db:
            yield db
    finally:
        db.close()
