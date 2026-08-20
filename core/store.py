"""
One SQLite file, shared by every project. This is the substrate the UI reads.

Design choice: append-only for spans and audit. Nothing is ever UPDATEd in the
audit table -- a corrected decision is a new row, not an edit. That is what makes
P6's trail defensible to a regulator, and it costs nothing to enforce here.
"""
from __future__ import annotations
import sqlite3, json, os, time, uuid, threading
from contextlib import contextmanager
from typing import Optional, Any

DB_PATH = os.environ.get("AGENT_DB", os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "agent.db"))

# RLock, not Lock. audit() opens the DB, and it is legitimately called from
# inside a db() block (e.g. P8 receive() logging a duplicate it just detected).
# With a plain Lock that self-deadlocks on the same thread. Caught by the P8
# dedupe path on first run -- see DECISIONS.md.
_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  project TEXT NOT NULL,
  claim_id TEXT,
  status TEXT NOT NULL,          -- running | ok | failed | paused
  started_at REAL NOT NULL,
  ended_at REAL,
  cost_usd REAL DEFAULT 0,
  meta TEXT DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS spans (
  span_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  parent_id TEXT,
  name TEXT NOT NULL,
  kind TEXT NOT NULL,            -- llm | tool | logic
  started_at REAL NOT NULL,
  duration_ms INTEGER,
  ok INTEGER DEFAULT 1,
  model TEXT, tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0,
  cost_usd REAL DEFAULT 0,
  attrs TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_spans_run ON spans(run_id);
CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY,     -- idempotency key (P8)
  claim_id TEXT, payload TEXT, status TEXT,
  attempts INTEGER DEFAULT 0, last_error TEXT,
  received_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS approvals (
  approval_id TEXT PRIMARY KEY,
  run_id TEXT, claim_id TEXT,
  reason TEXT, proposed TEXT, confidence REAL, amount_usd REAL,
  status TEXT,                   -- pending | approved | rejected | expired
  created_at REAL, expires_at REAL,
  decided_at REAL, decided_by TEXT, decision_note TEXT
);
CREATE TABLE IF NOT EXISTS audit (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
  claim_id TEXT, run_id TEXT, detail TEXT
);
"""


@contextmanager
def db():
    with _lock:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        con = sqlite3.connect(DB_PATH, timeout=10)
        con.row_factory = sqlite3.Row
        try:
            con.executescript(SCHEMA)
            yield con
            con.commit()
        finally:
            con.close()


def new_id(p="id") -> str:
    return f"{p}_{uuid.uuid4().hex[:12]}"


def audit(actor: str, action: str, claim_id: str = None,
          run_id: str = None, detail: Any = None):
    """Append-only. Never updated, never deleted."""
    with db() as c:
        c.execute("INSERT INTO audit(ts,actor,action,claim_id,run_id,detail)"
                  " VALUES(?,?,?,?,?,?)",
                  (time.time(), actor, action, claim_id, run_id,
                   json.dumps(detail) if detail is not None else None))
