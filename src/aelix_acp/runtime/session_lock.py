"""Opted-in stdio session lock errors (native control-v1, ADR-0316)."""

import json

META = "aelix/sessionLockHolder"
WRITER_FLAG = "--session-writer-kind"
ERROR_FLAG = "--rpc-session-lock-errors"
LAUNCH_FLAGS = frozenset({WRITER_FLAG, ERROR_FLAG})
STARTUP_PREFIX = b"AELIX_RPC_STARTUP_ERROR/1 "
STARTUP_LIMIT = 256  # Includes the final LF.


def offered(capabilities):
    value = (capabilities.get("_meta") or {}).get(META)
    return type(value) is int and value == 1


def supported(capabilities):
    value = capabilities.get("sessionLockErrors")
    return type(value) is int and value == 1


def contention(details):
    return isinstance(details, dict) and details.get("kind") == "session_locked"


def holder(details, capabilities):
    kind = details.get("holderKind")
    return kind if kind in ("cli", "studio") and offered(capabilities) else None


class StartupLock:
    """Keep a bounded line prefix and count reserved records through stderr EOF."""

    def __init__(self, nonce):
        self.nonce = nonce
        self.stdout_seen = self.stdout_eof = self.stderr_eof = False
        self.partial = b""
        self.last_line = b""
        self.records = 0

    def saw_stdout(self):
        self.stdout_seen = True

    def read_stderr(self, chunk):
        parts = chunk.split(b"\n")
        for part in parts[:-1]:
            self.last_line = (self.partial + part)[:STARTUP_LIMIT]
            self.records += self.last_line.startswith(STARTUP_PREFIX)
            self.partial = b""
        self.partial = (self.partial + parts[-1])[:STARTUP_LIMIT]

    def refusal(self, returncode):
        if (returncode != 1 or self.stdout_seen or not (self.stdout_eof and self.stderr_eof)
                or self.partial or self.records != 1 or len(self.last_line) >= STARTUP_LIMIT
                or not self.last_line.startswith(STARTUP_PREFIX)):
            return None
        try:
            details = json.loads(self.last_line[len(STARTUP_PREFIX):].decode("ascii"))
        except (ValueError, UnicodeError):
            return None
        if contention(details) and details.get("nonce") == self.nonce:
            return details
        return None
