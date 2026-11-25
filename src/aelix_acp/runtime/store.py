"""Private, migration-compatible ACP session index and session preferences."""
from __future__ import annotations

import asyncio
from collections import deque
from contextlib import closing
import json
from itertools import islice
import os
from pathlib import Path
import sqlite3
from threading import Event
from time import monotonic

from ..stdio import write_diagnostic
from ..wire.errors import ProtocolError
from .transcript import META as TRANSCRIPT_META

WRITE_TIMEOUT = 10
PROMPT_FAILURE_PREFIX = "Request failed: "
PROMPT_FAILURE_TEXT = "Request failed."
ASSISTANT_ORIGIN_META = "aelix/assistantOrigin"  # SQLite only


def open_store(directory: str) -> sqlite3.Connection:
    root = Path(directory).expanduser()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    filename = root / "sessions.sqlite3"
    if filename.is_symlink():
        raise ValueError("Session database must not be a symbolic link")
    db = sqlite3.connect(filename, timeout=WRITE_TIMEOUT)
    os.chmod(filename, 0o600)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, cwd TEXT, profile TEXT, file TEXT, title TEXT, updated INTEGER)")
    db.execute("CREATE TABLE IF NOT EXISTS pending_forks (session_id TEXT PRIMARY KEY, parent_id TEXT NOT NULL, native_id TEXT NOT NULL, file TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS preferences (session_id TEXT PRIMARY KEY, value TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS turns (session_id TEXT NOT NULL, request_key TEXT NOT NULL, state TEXT NOT NULL, result TEXT, fingerprint TEXT, PRIMARY KEY(session_id,request_key))")
    if "fingerprint" not in {row[1] for row in db.execute("PRAGMA table_info(turns)")}:
        db.execute("ALTER TABLE turns ADD COLUMN fingerprint TEXT")
    db.execute("CREATE TABLE IF NOT EXISTS events (sequence INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, value TEXT NOT NULL)")
    db.execute("CREATE INDEX IF NOT EXISTS events_session ON events(session_id,sequence)")
    db.execute("""CREATE TABLE IF NOT EXISTS recovered_history (
        session_id TEXT NOT NULL, position INTEGER NOT NULL, value TEXT,
        PRIMARY KEY(session_id,position)) WITHOUT ROWID""")
    db.commit()
    return db


async def write_store(directory, write, *args, diagnostic="session_store_write_failed", before_commit=None):
    """Run a write on its own connection, keeping the caller's lease through commit."""
    cancelled = False
    if before_commit is not None:
        loop = asyncio.get_running_loop()
        owner = asyncio.current_task()
        prior_cancels = owner.cancelling()

        async def admit():
            # Serialize admission with Stop and worker exit on the event loop.
            if cancelled or owner.cancelling() > prior_cancels:
                raise ProtocolError("session_publication_cancelled", -32003)
            before_commit()

        def check_commit():
            asyncio.run_coroutine_threadsafe(admit(), loop).result()
    else:
        check_commit = None
    task = asyncio.create_task(asyncio.to_thread(_write_store, directory, write, args, check_commit))
    while True:
        try:
            result, failure = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    if isinstance(failure, (sqlite3.Error, OSError, ValueError, TypeError)):
        raise ProtocolError(diagnostic, -32603, {"kind": diagnostic}) from None
    if failure is not None:
        raise failure
    return result


def _write_store(directory, write, args, check_commit):
    try:
        filename = Path(directory).expanduser() / "sessions.sqlite3"
        if filename.is_symlink():
            raise ValueError("Session database must not be a symbolic link")
        with closing(sqlite3.connect(filename, timeout=WRITE_TIMEOUT)) as db, db:
            db.execute("PRAGMA synchronous=FULL")
            result = write(db, *args)
            if check_commit is not None:
                check_commit()
            return result, None
    except Exception as failure:
        return None, failure


def save_session(db, row, preferences, inherited_history=None):
    db.execute("INSERT OR REPLACE INTO sessions VALUES(?,?,?,?,?,?)", row)
    save_preferences(db, row[0], preferences)
    if inherited_history is not None:
        insert_history(db, row[0], inherited_history)
        db.execute("DELETE FROM pending_forks WHERE session_id=?", (row[0],))


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
            insert_history(db, session_id, (update,))


def insert_history(db: sqlite3.Connection, session_id: str, updates) -> None:
    db.executemany("INSERT INTO events(session_id,value) VALUES(?,?)",
                   ((session_id, json.dumps(update, ensure_ascii=False, allow_nan=False)) for update in updates))


def confirm_adapter_input(db, session_id, binding, first, last):
    """A slash command completed locally. Keep its previously recorded input."""
    prefix = '$._meta."' + TRANSCRIPT_META + '".'
    db.execute("UPDATE events SET value=json_set(value, ?, 'adapter') WHERE session_id=? "
               "AND json_extract(value, ?)='composer' AND json_extract(value, ?)=? "
               "AND json_extract(value, ?) BETWEEN ? AND ?",
               (prefix + "source", session_id, prefix + "source", prefix + "bindingId", binding,
                prefix + "sequence", first, last))


def _write_busy(error):
    return (getattr(error, "sqlite_errorcode", 0) & 255) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)


class LiveHistory:
    """One session's ordered recording, including the batch currently writing."""

    def __init__(self, directory, session_id, *, max_events=1024, max_bytes=32 * 1024 * 1024):
        self.directory = directory
        self.session_id = session_id
        self.max_events = max_events
        self.max_bytes = max_bytes
        self.pending = deque()
        self.pending_bytes = 0
        self.committed = 0
        self.advanced = asyncio.Event()
        self.worker = None
        self.failure = None

    def append(self, update, *, retry_if_full=False):
        """Offer a record. A prompt producer can retry after a flush."""
        if self.failure or update.get("sessionUpdate") not in HISTORY_UPDATES:
            return True
        try:
            value = json.dumps(update, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (ValueError, TypeError, RecursionError):
            self.fail("history_write_failed")
            return True
        if len(self.pending) >= self.max_events or self.pending_bytes + len(value) > self.max_bytes:
            if retry_if_full and self.pending and len(value) <= self.max_bytes:
                return False
            self.fail("history_backlog_overflow")
            return True
        self.pending.append(value)
        self.pending_bytes += len(value)
        if self.worker is None:
            self.advanced = asyncio.Event()
            self.worker = asyncio.create_task(self.drain())
        return True

    def fail(self, code):
        if self.failure != code:
            write_diagnostic(code)
        self.failure = code

    async def drain(self):
        try:
            while self.pending:
                rows, size = [], 0
                for value in islice(self.pending, 32):
                    rows.append((self.session_id, value))
                    size += len(value)
                    if size >= 64 * 1024:
                        break
                await write_store(self.directory, self.insert_batch, rows)
                for _ in rows:
                    self.pending.popleft()
                self.pending_bytes -= size
                self.committed += len(rows)
                self.advanced.set()
                self.advanced = asyncio.Event()
        except Exception:
            self.fail("history_write_failed")
            self.pending.clear()
            self.pending_bytes = 0
        finally:
            self.worker = None
            self.advanced.set()

    @staticmethod
    def insert_batch(db, rows):
        db.executemany("INSERT INTO events(session_id,value) VALUES(?,?)",
                       ((session_id, value.decode("utf-8")) for session_id, value in rows))

    async def flush(self):
        # Only wait for the prefix accepted when the barrier was reached.
        target = self.committed + len(self.pending)
        while self.worker is not None and self.committed < target:
            await self.advanced.wait()


async def cache_recovered_history(directory: str, session_id: str, updates) -> None:
    """Keep SQLite work and lock waits off the event loop for recovery."""
    stop = Event()

    async def write():
        try:
            await asyncio.to_thread(_cache_recovered_history, directory, session_id, updates, stop)
        except Exception as exc:
            # A cancelled shield can log an inner task's exception on Python
            # 3.14. Return the error to this caller's fixed diagnostic path.
            return exc
        return None

    worker = asyncio.create_task(write())
    try:
        failure = await asyncio.shield(worker)
    except asyncio.CancelledError:
        # The load owns the lease until this writer is finished, including
        # when the client goes away. Staged rows remain invisible on retry.
        stop.set()
        await worker
        raise
    if failure is not None:
        raise failure


def _recovery_transaction(db, stop, sql, rows, *, finish=None):
    deadline = monotonic() + WRITE_TIMEOUT
    while not stop.is_set():
        try:
            with db:
                cursor = db.executemany(sql, rows)
                if finish is not None:
                    if stop.is_set():
                        raise InterruptedError
                    db.execute(finish[0], finish[1])
            return cursor.rowcount
        except sqlite3.OperationalError as exc:
            if not _write_busy(exc) or monotonic() >= deadline:
                raise
            stop.wait(0.01)
    raise InterruptedError


def _cache_recovered_history(directory, session_id, updates, stop):
    filename = Path(directory).expanduser() / "sessions.sqlite3"
    if filename.is_symlink():
        raise ValueError("Session database must not be a symbolic link")
    # This connection is only used on the worker thread. Keep individual
    # transactions short so unrelated live events can acquire the writer slot.
    with closing(sqlite3.connect(filename, timeout=0.05)) as db:
        db.execute("PRAGMA synchronous=FULL")
        cleanup = """DELETE FROM recovered_history WHERE session_id=? AND position IN (
            SELECT position FROM recovered_history WHERE session_id=? LIMIT 32)"""
        while _recovery_transaction(db, stop, cleanup, [(session_id, session_id)]):
            pass
        insert = "INSERT INTO recovered_history VALUES(?,?,?)"
        batch = []
        chars = 0
        for position, update in enumerate(updates):
            value = json.dumps(update, ensure_ascii=False, allow_nan=False)
            batch.append((session_id, position, value))
            chars += len(value)
            if chars >= 64 * 1024 or len(batch) == 32:
                _recovery_transaction(db, stop, insert, batch)
                batch.clear()
                chars = 0
        marker = ("INSERT INTO recovered_history VALUES(?,-1,NULL)", (session_id,))
        _recovery_transaction(db, stop, insert, batch, finish=marker)


def history(db: sqlite3.Connection, session_id: str):
    # A replay can await client output while this connection records live
    # updates. Keep the same cutoff for the lifetime of the cursor.
    (last,) = db.execute("SELECT max(sequence) FROM events WHERE session_id=?", (session_id,)).fetchone()
    if db.execute("SELECT 1 FROM recovered_history WHERE session_id=? AND position=-1", (session_id,)).fetchone():
        for (value,) in db.execute("SELECT value FROM recovered_history WHERE session_id=? AND position>=0 "
                                   "ORDER BY position", (session_id,)):
            yield json.loads(value)
    for (value,) in db.execute("SELECT value FROM events WHERE session_id=? AND sequence<=? ORDER BY sequence",
                               (session_id, last)):
        yield json.loads(value)


def with_assistant_origin(update: dict, origin: str) -> dict:
    if update.get("sessionUpdate") != "agent_message_chunk":
        return update
    return {**update, "_meta": {**update.get("_meta", {}), ASSISTANT_ORIGIN_META: origin}}


def _ambiguous_legacy_failure(update: dict, meta: object) -> bool:
    coordinates = meta.get(TRANSCRIPT_META) if isinstance(meta, dict) else None
    if isinstance(coordinates, dict) and coordinates.get("source") not in (None, "adapter"):
        return False
    content = update.get("content")
    if not isinstance(content, dict) or content.get("type") != "text":
        return False
    text = content.get("text")
    return isinstance(text, str) and text.startswith(PROMPT_FAILURE_PREFIX)


def display_recorded_update(update: dict, *, protected: bool) -> dict:
    """Project stored display events and consume the private origin marker."""
    if update.get("sessionUpdate") != "agent_message_chunk":
        return update
    meta = update.get("_meta")
    origin = None
    if isinstance(meta, dict) and ASSISTANT_ORIGIN_META in meta:
        origin = meta.pop(ASSISTANT_ORIGIN_META)
        if not meta:
            del update["_meta"]
    if protected and (origin == "prompt_failure" or (origin is None and _ambiguous_legacy_failure(update, meta))):
        return {**update, "content": {"type": "text", "text": PROMPT_FAILURE_TEXT}}
    return update


def recorded_turn(db: sqlite3.Connection, session_id: str, request_key: str, fingerprint: str | None = None) -> dict | None:
    """Read an existing turn's outcome without claiming an unseen request."""
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
    return None


def begin_turn(db: sqlite3.Connection, session_id: str, request_key: str, fingerprint: str | None = None) -> dict | None:
    prior = recorded_turn(db, session_id, request_key, fingerprint)
    if prior is not None:
        return prior
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
