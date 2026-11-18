"""Track the one native file a HEAD fork may publish before ACP binding."""
from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import uuid

from .leases import acquire, acquire_file, lease_path
from ..wire.errors import ProtocolError, SESSION_LOCKED


def receipt_path(state_dir, session_id):
    return lease_path(state_dir, session_id, "fork-receipt").with_suffix(".sha256")


def reserve(db, state_dir, session_id, parent_id, parent_file):
    native_id = str(uuid.uuid4())
    timestamp = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    filename = timestamp.replace(":", "-").replace(".", "-") + f"_{native_id}.jsonl"
    target = Path(parent_file).resolve(strict=True).with_name(filename)
    with db:
        db.execute("INSERT INTO pending_forks VALUES(?,?,?,?)", (session_id, parent_id, native_id, str(target)))
    return {"nativeId": native_id, "target": str(target), "receipt": str(receipt_path(state_dir, session_id).resolve())}


def discard(db, state_dir, session_id):
    """Reclaim a draft while holding its ACP lease and a native writer lock."""
    row = db.execute("SELECT native_id,file FROM pending_forks WHERE session_id=?", (session_id,)).fetchone()
    if not row:
        return
    native_id, filename = row
    target = Path(filename)
    staged = Path(filename + ".tmp")
    receipt = receipt_path(state_dir, session_id)
    if not target.is_absolute() or not target.name.endswith(f"_{native_id}.jsonl"):
        raise ValueError("Invalid pending fork path")
    if target.parent.is_dir():
        with acquire_file(Path(filename + ".lock")):
            if target.is_symlink() or staged.is_symlink():
                raise ValueError("Pending fork must not be a symbolic link")
            if target.exists():
                with target.open("rb") as stream:
                    header = json.loads(stream.readline(65536))
                    stream.seek(0)
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                if (not isinstance(header, dict) or header.get("type") != "session" or header.get("id") != native_id
                        or not receipt.is_file() or receipt.is_symlink()
                        or receipt.read_text(encoding="ascii") != digest):
                    raise ValueError("Pending fork changed after publication")
                target.unlink()
            staged.unlink(missing_ok=True)
    receipt.unlink(missing_ok=True)
    with db:
        db.execute("DELETE FROM pending_forks WHERE session_id=?", (session_id,))


def recover(db, state_dir, parent_id=None):
    """Reclaim abandoned drafts without interfering with another live adapter."""
    rows = db.execute(
        "SELECT session_id FROM pending_forks WHERE (? IS NULL OR parent_id=?)",
        (parent_id, parent_id),
    ).fetchall()
    for (session_id,) in rows:
        try:
            with acquire(state_dir, session_id):
                discard(db, state_dir, session_id)
        except ProtocolError as error:
            if error.code != SESSION_LOCKED:
                raise
        except (ValueError, OSError):
            # Preserve drafts that are still locked, modified or inaccessible.
            continue
    if parent_id is not None and db.execute(
            "SELECT 1 FROM pending_forks WHERE parent_id=?", (parent_id,)).fetchone():
        raise cleanup_pending()


def cleanup_pending():
    return ProtocolError("A previous fork still needs cleanup", -32006,
                         {"kind": "fork_cleanup_pending", "retryable": False})
