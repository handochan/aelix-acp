"""Errors whose wire representation is stable and safe for ACP clients."""
from __future__ import annotations

# Application error codes. ACP reserves -32768..-32000 and -32899..-32800, so
# these stay outside both ranges. A client that does not know them treats them
# as generic failures; AionCore maps them to a non-retryable protocol error.
SESSION_LOCKED = -33001
TURN_FAILED_AFTER_DISPATCH = -33002

# Native's own refusal when another process holds the session's writer lock
# (aelix-ai cli/entry.py `_session_owned_message` and the runtime's swap path).
# Until aelix-ai reports the lock holder (Studio request R7), this phrase is the
# only signal. Keep every match in `is_native_session_locked`.
_NATIVE_LOCKED_PHRASE = "already open in another terminal"
_MAX_MESSAGE = 2000


class ProtocolError(Exception):
    def __init__(self, message: str, code: int = -32602, data: dict | None = None):
        super().__init__(message)
        self.code = code
        self.data = data


class BackendError(ProtocolError):
    def __init__(self, message: str, command: str):
        super().__init__(message, -32603, {"kind": "backend_error", "command": command})


def require_object(value: object, name: str = "params") -> dict:
    if not isinstance(value, dict):
        raise ProtocolError(f"{name} must be an object")
    return value


def require_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ProtocolError(f"{name} must be a non-empty string without NUL bytes")
    return value


def is_native_session_locked(text: object) -> bool:
    """Whether native refused a session because another process writes to it."""
    return _NATIVE_LOCKED_PHRASE in str(text).casefold()


def session_locked(session_id: str, locked_by: str) -> ProtocolError:
    """A session another writer holds. `holder` stays null until R7 provides it."""
    where = "the Aelix CLI or another Aelix process" if locked_by == "native" else "another Aelix Studio window"
    return ProtocolError(f"This session is open in {where}. Close it there, then open it again.",
                         SESSION_LOCKED, {"kind": "session_locked", "sessionId": session_id,
                                          "lockedBy": locked_by, "holder": None, "retryable": False})


def after_dispatch(error: BaseException) -> ProtocolError:
    """Mark a failure that happened after native accepted work as not retryable.

    AionCore replays a failed turn once when it classifies the error as
    retryable, which it does for an unrecognized JSON-RPC internal error. Native
    may already have called the model or run tools by then, so a replay could
    repeat them. An error that already has a specific code keeps it.
    """
    code = getattr(error, "code", -32603)
    if isinstance(error, ProtocolError) and code not in (-32603, -32001):
        return error
    detail = {"kind": "turn_failed_after_dispatch", "retryable": False}
    data = getattr(error, "data", None)
    if isinstance(data, dict):
        cause = {key: data[key] for key in ("kind", "command") if isinstance(data.get(key), str)}
        if cause:
            detail["cause"] = cause
    return ProtocolError((str(error) or type(error).__name__)[:_MAX_MESSAGE], TURN_FAILED_AFTER_DISPATCH, detail)
