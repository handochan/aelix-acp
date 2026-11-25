"""Errors whose wire representation is stable and safe for ACP clients."""
from __future__ import annotations

# Application error codes. ACP reserves -32768..-32000 and -32899..-32800, so
# these stay outside both ranges. A client that does not know them treats them
# as generic failures; AionCore maps them to a non-retryable protocol error.
SESSION_LOCKED = -33001
TURN_FAILED_AFTER_DISPATCH = -33002
FORK_HEAD_CHANGED = -33003
SOCKET_PATH_TOO_LONG = -33004

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


def fork_head_changed() -> ProtocolError:
    return ProtocolError("The native fork point changed", FORK_HEAD_CHANGED,
                         {"kind": "fork_head_changed", "retryable": False})


def model_selection_required(available: int) -> ProtocolError:
    """Model input while native has no current model, refused before native receives it.

    `-32004` with structured `data`: the session cannot take model input until
    the client meets a precondition (AionCore keeps `-32004` data as an
    operation refusal, while `-32602` reaches it as bare invalid parameters).
    The client sets the advertised `model` option, or first configures a
    provider when none is available (`availableModels: 0`, no `model` option).
    """
    message = ("No model is selected; choose one with the model option, then send the prompt again" if available
               else "No model is available; configure a provider for Aelix, then choose a model")
    return ProtocolError(message, -32004, {"kind": "model_selection_required", "configId": "model",
                                           "availableModels": available, "retryable": False})


def session_changing() -> ProtocolError:
    """A child intervention refused before anything was written (aelix-ai #612)."""
    return ProtocolError("the session is changing", -32000, {"kind": "session_changing"})


def delivery_unknown(action: str) -> ProtocolError:
    """A child intervention a restart or close interrupted after its line was written (aelix-ai #612).

    Native may have read it, so a steer or wrap-up may have reached the child.
    """
    return ProtocolError("the session changed while this request ran; its delivery is unknown",
                         TURN_FAILED_AFTER_DISPATCH,
                         {"kind": "turn_failed_after_dispatch", "retryable": False,
                          "cause": {"kind": "session_changed", "command": action}})


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
