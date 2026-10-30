"""Errors whose wire representation is stable and safe for ACP clients."""
from __future__ import annotations


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

