"""Classify native assistant failures without forwarding provider diagnostics."""
from __future__ import annotations

import ast
import re

_STATUS_KINDS = {
    400: "provider_bad_request", 404: "provider_bad_request", 422: "provider_bad_request",
    401: "provider_auth", 403: "provider_auth", 429: "provider_rate_limit",
}
_TYPE_KINDS = {
    "authentication_error": "provider_auth",
    "permission_error": "provider_auth",
    "rate_limit_error": "provider_rate_limit",
    "overloaded_error": "provider_unavailable",
    "api_error": "provider_unavailable",
    "invalid_request_error": "provider_bad_request",
}

# SDK HTTP errors and connection errors as exposed by native 83714045.
_HTTP_STATUS = re.compile(r"\AError code: ([0-9]{3})(?: - |\Z)")
_CODEX_HTTP_STATUS = re.compile(r"\AOpenAI Codex request failed \(([0-9]{3})\)(?:: |\Z)")
_CONNECTION_ERROR = "Connection error."
# Native quotes provider diagnostics to 512 code points. Avoid parsing larger
# strings from custom providers as SDK bodies.
_MAX_BODY_CHARS = 512


def _status_kind(status: object) -> str | None:
    if type(status) is int:
        if 500 <= status <= 599:
            return "provider_unavailable"
        return _STATUS_KINDS.get(status)
    return None


def _type_kind(error_type: object) -> str | None:
    return _TYPE_KINDS.get(error_type) if isinstance(error_type, str) else None


def _body_type_kind(body: str) -> str | None:
    # The SDK prints the Python repr of the response or stream error object.
    # Only a complete envelope can identify a type field. Error messages or
    # arbitrary nested strings can contain apparent field names of their own.
    if len(body) > _MAX_BODY_CHARS or not (body.startswith("{") and body.endswith("}")):
        return None
    try:
        envelope = ast.literal_eval(body)
    except (SyntaxError, ValueError):
        return None
    error = envelope.get("error") if isinstance(envelope, dict) else None
    return _type_kind(error.get("type")) if isinstance(error, dict) else None


def provider_failure_kind(structured: object, text: object) -> str:
    """Prefer aelix-ai #921's message.error over the legacy display format."""
    if isinstance(structured, dict):
        error_type = structured.get("type")
        if kind := _status_kind(structured.get("status")):
            return kind
        if error_type == "connection_error":
            return "provider_network"
        return _type_kind(error_type) or "unknown"

    if not isinstance(text, str):
        return "unknown"
    if text == _CONNECTION_ERROR or text.startswith(_CONNECTION_ERROR + " — "):
        return "provider_network"
    if status := _HTTP_STATUS.match(text) or _CODEX_HTTP_STATUS.match(text):
        if kind := _status_kind(int(status[1])):
            return kind
        if len(text) - status.end() > _MAX_BODY_CHARS:
            return "unknown"
        text = text[status.end():]
    return _body_type_kind(text) or "unknown"
