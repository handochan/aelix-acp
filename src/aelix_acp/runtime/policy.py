"""Approval decisions based on runtime-verified tool provenance, never tool names."""
from __future__ import annotations


def permission_decision(mode: str, provenance: str | None, *, write_allowed=False) -> str:
    if mode == "plan":
        return "allow" if provenance == "read_only" else "deny"
    if mode in {"auto-accept-edits", "auto"} and provenance == "write" and write_allowed is True:
        return "allow"
    if mode == "yolo":
        return "allow"
    return "ask"
