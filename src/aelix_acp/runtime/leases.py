"""OS-owned session leases; process death releases a lease automatically."""
from __future__ import annotations

import errno
import hashlib
import os
from pathlib import Path

from ..wire.errors import ProtocolError, session_locked


def lease_path(directory: str, session_id: str, purpose: str = "") -> Path:
    root = Path(directory).expanduser() / "leases"
    suffix = f".{purpose}.lock" if purpose else ".lock"
    return root / (hashlib.sha256(session_id.encode()).hexdigest() + suffix)


def acquire(directory: str, session_id: str, *, purpose: str = ""):
    filename = lease_path(directory, session_id, purpose)
    filename.parent.mkdir(mode=0o700, exist_ok=True)
    if filename.is_symlink():
        raise ProtocolError("Session lease must not be a symbolic link")
    stream = filename.open("a+b")
    try:
        os.chmod(filename, 0o600)
        if os.name == "nt" and stream.seek(0, 2) == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            contended = {errno.EAGAIN, errno.EWOULDBLOCK}
            if os.name == "nt":
                contended.update({errno.EACCES, errno.EDEADLK})
            if error.errno not in contended:
                raise
            if purpose == "activity":
                raise ProtocolError("Session is busy", -32000) from None
            raise session_locked(session_id, "acp") from None
    except BaseException:
        stream.close()
        raise
    return stream
