"""Private, migration-compatible ACP session index and session preferences."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3


def open_store(directory: str) -> sqlite3.Connection:
    root = Path(directory).expanduser()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    filename = root / "sessions.sqlite3"
    if filename.is_symlink():
        raise ValueError("Session database must not be a symbolic link")
    db = sqlite3.connect(filename, timeout=10)
    os.chmod(filename, 0o600)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, cwd TEXT, profile TEXT, file TEXT, title TEXT, updated INTEGER)")
    db.execute("CREATE TABLE IF NOT EXISTS preferences (session_id TEXT PRIMARY KEY, value TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS turns (session_id TEXT NOT NULL, request_key TEXT NOT NULL, state TEXT NOT NULL, result TEXT, fingerprint TEXT, PRIMARY KEY(session_id,request_key))")
    if "fingerprint" not in {row[1] for row in db.execute("PRAGMA table_info(turns)")}:
        db.execute("ALTER TABLE turns ADD COLUMN fingerprint TEXT")
    db.execute("CREATE TABLE IF NOT EXISTS events (sequence INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, value TEXT NOT NULL)")
    db.execute("CREATE INDEX IF NOT EXISTS events_session ON events(session_id,sequence)")
    db.commit()
    return db


def legacy_permission_mode(db: sqlite3.Connection, session_id: str):
    """A mode saved by the earlier Ask/Yolo adapter, which kept it in its own table."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='permission_modes'").fetchone():
        return None
    row = db.execute("SELECT mode FROM permission_modes WHERE id=?", (session_id,)).fetchone()
    return row[0] if row else None


def save_preferences(db: sqlite3.Connection, session_id: str, value: dict) -> None:
    db.execute("INSERT OR REPLACE INTO preferences VALUES(?,?)", (session_id, json.dumps(value, allow_nan=False)))


def load_preferences(db: sqlite3.Connection, session_id: str) -> dict:
    row = db.execute("SELECT value FROM preferences WHERE session_id=?", (session_id,)).fetchone()
    if not row:
        return {}
    value = json.loads(row[0])
    if not isinstance(value, dict):
        raise ValueError("Invalid stored session preferences")
    return value


HISTORY_UPDATES = {"user_message_chunk", "agent_message_chunk", "agent_thought_chunk", "tool_call", "tool_call_update"}


def append_event(db: sqlite3.Connection, session_id: str, update: dict) -> None:
    if update.get("sessionUpdate") in HISTORY_UPDATES:
        # A stream must not retain the database's sole writer slot until turn
        # completion and block other adapter processes for the model's latency.
        with db:
            db.execute("INSERT INTO events(session_id,value) VALUES(?,?)",
                       (session_id, json.dumps(update, ensure_ascii=False, allow_nan=False)))


def history(db: sqlite3.Connection, session_id: str):
    for row in db.execute("SELECT value FROM events WHERE session_id=? ORDER BY sequence", (session_id,)):
        yield json.loads(row[0])


def begin_turn(db: sqlite3.Connection, session_id: str, request_key: str, fingerprint: str | None = None) -> dict | None:
    from ..wire.errors import ProtocolError
    row = db.execute("SELECT state,result,fingerprint FROM turns WHERE session_id=? AND request_key=?", (session_id, request_key)).fetchone()
    if row:
        if fingerprint is not None and row[2] is not None and row[2] != fingerprint:
            raise ProtocolError("Request id was reused with different prompt content", -32008)
        if row[0] == "rejected" and row[1]:
            error = json.loads(row[1])["error"]
            raise ProtocolError(error["message"], error["code"], error.get("data"))
        if row[0] == "completed" and row[1]:
            return json.loads(row[1])
        raise ProtocolError("Previous request has an unresolved outcome; it will not be executed again", -32006,
                            {"kind": "unresolved_turn", "requestId": request_key, "state": row[0]})
    with db:
        db.execute("INSERT INTO turns(session_id,request_key,state,result,fingerprint) VALUES(?,?,?,NULL,?)", (session_id, request_key, "running", fingerprint))
    return None


def finish_turn(db: sqlite3.Connection, session_id: str, request_key: str, result: dict | None, error=None) -> None:
    state = "completed" if result is not None else "interrupted"
    value = result
    if error is not None and getattr(error, "code", None) in {-32601, -32602, -32004}:
        state = "rejected"
        value = {"error": {"code": error.code, "message": str(error), "data": error.data}}
    with db:
        db.execute("UPDATE turns SET state=?,result=? WHERE session_id=? AND request_key=?",
                   (state, json.dumps(value) if value is not None else None, session_id, request_key))
