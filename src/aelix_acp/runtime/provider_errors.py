"""Classify native assistant failures without forwarding provider diagnostics."""
from __future__ import annotations

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
    "connection_error": "provider_network",
}


def _status_kind(status: object) -> str | None:
    if type(status) is int:
        if 500 <= status <= 599:
            return "provider_unavailable"
        return _STATUS_KINDS.get(status)
    return None


def provider_failure_kind(structured: object) -> str:
    """Use aelix-ai #921's message.error as the source of provenance.

    The pinned runtime serializes SDK errors and provider-authored messages
    in the same display field, including strings with SDK prefixes.
    """
    if not isinstance(structured, dict):
        return "unknown"
    if kind := _status_kind(structured.get("status")):
        return kind
    error_type = structured.get("type")
    return _TYPE_KINDS.get(error_type, "unknown") if isinstance(error_type, str) else "unknown"
