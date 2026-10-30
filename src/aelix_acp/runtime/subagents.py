"""Native delegation progress as `_meta` on the parent `agent` tool call.

Native emits `aelix_subagent` events (rpc/control.py `bind_progress`) without
naming the tool call that started the child. The native `agent` tool runs
sequentially (aelix_agents/tool.py `execution_mode="sequential"`), so at most
one `agent` call is open at a time and a child belongs to the open one. A child
seen earlier keeps its parent for events that arrive after that call ended.
"""
from __future__ import annotations

import math
import re

META_KEY = "aelix/subagents"
AGENT_TOOL = "agent"
MAX_CHILDREN = 32
_TEXT_LIMIT = 256
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_EVENTS = {"subagent_start", "subagent_tool", "subagent_end"}
_FIELDS = {"id": "id", "profile": "profile", "state": "state", "current_tool": "currentTool",
           "model": "model", "provider": "provider"}


def _text(value):
    # Child-authored values are untrusted: bound them and drop control bytes.
    return _CONTROL.sub(" ", value)[:_TEXT_LIMIT] if isinstance(value, str) else None


def _count(value, kind):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return 0 if kind is int else 0.0
    return kind(value)


def snapshot(event: str, data: dict) -> dict | None:
    """One child's bounded progress, or None for a malformed event."""
    if event not in _EVENTS or not isinstance(data, dict) or not _text(data.get("id")):
        return None
    child = {name: _text(data.get(key)) for key, name in _FIELDS.items()}
    child.update(event=event, elapsedMs=_count(data.get("elapsed_ms"), int),
                 tokens=_count(data.get("tokens"), int), cost=_count(data.get("cost"), float))
    return child


class SubagentTracker:
    """Per-session mapping from native children to their parent tool calls."""

    def __init__(self):
        self.open_call = None
        self.parents = {}
        self.children = {}

    def tool_started(self, call_id, name):
        if name == AGENT_TOOL and isinstance(call_id, str):
            self.open_call = call_id
            self.children.setdefault(call_id, {})

    def tool_ended(self, call_id):
        if call_id == self.open_call:
            self.open_call = None

    def record(self, event: str, data: dict) -> tuple[str | None, dict | None]:
        """Return (parent call, `_meta` value), or (None, None) if unparented."""
        child = snapshot(event, data)
        if child is None:
            return None, None
        parent = self.open_call or self.parents.get(child["id"])
        if parent is None:
            return None, None
        children = self.children.setdefault(parent, {})
        if child["id"] not in children and len(children) >= MAX_CHILDREN:
            return parent, self.meta(parent)
        # A resumed child (`agent action=resume`) moves to its new parent call.
        self.parents[child["id"]] = parent
        children[child["id"]] = child
        return parent, self.meta(parent)

    def meta(self, call_id) -> dict | None:
        children = self.children.get(call_id)
        if not children:
            return None
        return {META_KEY: {"version": 1, "children": list(children.values())}}

    def clear(self):
        self.open_call = None
        self.parents.clear()
        self.children.clear()
