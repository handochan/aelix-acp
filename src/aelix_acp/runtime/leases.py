"""OS-owned session leases; process death releases a lease automatically."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from ..wire.errors import ProtocolError, session_locked


def acquire(directory: str, session_id: str):
    root = Path(directory).expanduser() / "leases"
    root.mkdir(mode=0o700, exist_ok=True)
    filename = root / (hashlib.sha256(session_id.encode()).hexdigest() + ".lock")
    if filename.is_symlink():
        raise ProtocolError("Session lease must not be a symbolic link")
    stream = filename.open("a+b")
    try:
        os.chmod(filename, 0o600)
        if os.name == "nt":
            import msvcrt
            if stream.seek(0, 2) == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        stream.close()
        raise session_locked(session_id, "acp") from None
    return stream
