"""SQLite storage: the agent's workspace (inbox, invoices, records, outbox)
and the firewall's own records (sessions, calls, learned rules, benchmark runs).

Calls are short and the app is single-process, so one connection guarded by a
lock is simpler and faster than a pool.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS emails (
    id TEXT PRIMARY KEY,
    sender TEXT NOT NULL,
    sender_name TEXT NOT NULL,
    subject TEXT NOT NULL,
    html TEXT NOT NULL,
    received_at TEXT NOT NULL,
    folder TEXT NOT NULL DEFAULT 'inbox',
    crafted INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS contacts (
    email TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS invoices (
    id TEXT PRIMARY KEY,
    vendor TEXT NOT NULL,
    amount REAL NOT NULL,
    currency TEXT NOT NULL,
    month TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS records (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    id TEXT PRIMARY KEY,
    recipient TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    attachment TEXT,
    session_id TEXT,
    sent_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    task TEXT NOT NULL,
    agent_mode TEXT NOT NULL,
    status TEXT NOT NULL,
    reply TEXT,
    created_at REAL NOT NULL,
    finished_at REAL
);
CREATE TABLE IF NOT EXISTS calls (
    id TEXT PRIMARY KEY,
    session_id TEXT,
    seq INTEGER NOT NULL,
    tool TEXT NOT NULL,
    args TEXT NOT NULL,
    risk INTEGER NOT NULL,
    level TEXT NOT NULL,
    verdict TEXT NOT NULL,
    reasons TEXT NOT NULL,
    signals TEXT NOT NULL,
    evidence TEXT NOT NULL,
    decision TEXT,
    decided_by TEXT,
    decided_at REAL,
    result_preview TEXT,
    check_ms REAL NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS calls_session ON calls(session_id, seq);
CREATE INDEX IF NOT EXISTS calls_created ON calls(created_at);
CREATE TABLE IF NOT EXISTS learned_rules (
    id TEXT PRIMARY KEY,
    effect TEXT NOT NULL,
    tool TEXT NOT NULL,
    field TEXT NOT NULL,
    value TEXT NOT NULL,
    created_by TEXT NOT NULL,
    source_call TEXT,
    created_at REAL NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS bench_runs (
    id TEXT PRIMARY KEY,
    agent_mode TEXT NOT NULL,
    status TEXT NOT NULL,
    summary TEXT,
    results TEXT,
    created_at REAL NOT NULL,
    finished_at REAL
);
"""

JSON_FIELDS = {"args", "reasons", "signals", "evidence", "summary", "results"}


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    # -- primitives -------------------------------------------------------
    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            return cur.rowcount

    def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.executemany(sql, [tuple(r) for r in rows])
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def all(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [self._decode(dict(r)) for r in rows]

    def one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        rows = self.all(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Iterable[Any] = ()) -> Any:
        with self._lock:
            row = self._conn.execute(sql, tuple(params)).fetchone()
        return row[0] if row else None

    @staticmethod
    def _decode(row: dict[str, Any]) -> dict[str, Any]:
        for k in JSON_FIELDS & row.keys():
            if isinstance(row[k], str):
                try:
                    row[k] = json.loads(row[k])
                except ValueError:
                    pass
        return row

    # -- firewall records -------------------------------------------------
    def insert_call(self, row: dict[str, Any]) -> None:
        cols = list(row.keys())
        vals = [json.dumps(row[c]) if c in JSON_FIELDS else row[c] for c in cols]
        self.execute(
            f"INSERT INTO calls ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            vals,
        )

    def update_call(self, call_id: str, **fields: Any) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        vals = [json.dumps(v) if k in JSON_FIELDS else v for k, v in fields.items()]
        self.execute(f"UPDATE calls SET {sets} WHERE id=?", [*vals, call_id])

    def get_call(self, call_id: str) -> dict[str, Any] | None:
        return self.one("SELECT * FROM calls WHERE id=?", [call_id])

    def now(self) -> float:
        return time.time()
