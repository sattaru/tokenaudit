"""Local SQLite store. Everything stays on the machine unless you export it."""
from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Iterable, Optional

HOME = Path(os.environ.get("TOKENAUDIT_HOME", Path.home() / ".tokenaudit"))
DB_PATH = HOME / "usage.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    dedup_key     TEXT PRIMARY KEY,
    ts            TEXT NOT NULL,          -- ISO-8601 UTC
    source        TEXT NOT NULL,          -- sdk | claude-code | tokscale
    client        TEXT,                   -- app name, claude-code, codex, gemini-cli ...
    provider      TEXT,                   -- anthropic | openai | google
    model         TEXT,
    project       TEXT,                   -- repo / workspace name
    workspace     TEXT,                   -- full path (redacted in public reports)
    session       TEXT,
    is_subagent   INTEGER DEFAULT 0,
    input         INTEGER DEFAULT 0,      -- uncached input tokens
    output        INTEGER DEFAULT 0,
    cache_read    INTEGER DEFAULT 0,
    cache_write   INTEGER DEFAULT 0,
    reasoning     INTEGER DEFAULT 0,      -- already included in output for billing
    cost          REAL,                   -- USD, NULL when unpriced
    latency_ms    INTEGER,
    tag           TEXT,                   -- free-form label (feature, endpoint, task)
    tools         TEXT,                   -- comma list of tool calls in this turn
    tool_ctx      TEXT                    -- json {tool: est_tokens_added_to_context}
);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS ix_events_project ON events(project);
"""


@dataclass
class Event:
    dedup_key: str
    ts: str
    source: str
    client: str = ""
    provider: str = ""
    model: str = ""
    project: str = ""
    workspace: str = ""
    session: str = ""
    is_subagent: int = 0
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    reasoning: int = 0
    cost: Optional[float] = None
    latency_ms: Optional[int] = None
    tag: str = ""
    tools: str = ""
    tool_ctx: str = ""

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_write


_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None
_conn_path: str = ""


def connect(path: Path | None = None) -> sqlite3.Connection:
    global _conn, _conn_path
    path = Path(path or os.environ.get("TOKENAUDIT_DB", DB_PATH))
    if _conn is not None and _conn_path == str(path):
        return _conn
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _conn, _conn_path = conn, str(path)
    return conn


def upsert(events: Iterable[Event], conn: sqlite3.Connection | None = None) -> int:
    conn = conn or connect()
    rows = [asdict(e) for e in events]
    if not rows:
        return 0
    cols = list(rows[0].keys())
    sql = f"INSERT OR REPLACE INTO events ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
    with _lock:
        conn.executemany(sql, [tuple(r[c] for c in cols) for r in rows])
        conn.commit()
    return len(rows)


def load(conn: sqlite3.Connection | None = None, since: str | None = None,
         until: str | None = None, project: str | None = None) -> list[Event]:
    conn = conn or connect()
    q, args = "SELECT * FROM events WHERE 1=1", []
    if since:
        q += " AND ts >= ?"; args.append(since)
    if until:
        q += " AND ts < ?"; args.append(until)
    if project:
        q += " AND project = ?"; args.append(project)
    q += " ORDER BY ts"
    return [Event(**dict(r)) for r in conn.execute(q, args)]
