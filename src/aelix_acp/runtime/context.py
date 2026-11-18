"""Native context measurements and compaction updates on standard ACP frames."""
from __future__ import annotations

import uuid

CONTEXT_UNKNOWN = "aelix/contextUnknown"
COMPACTION = "aelix/compaction"
AUTOMATIC_REASONS = ("threshold", "overflow")
MAX_SAFE_INTEGER = (1 << 53) - 1


def token_count(value):
    return type(value) is int and 0 <= value <= MAX_SAFE_INTEGER


def measured_usage(used, size):
    if token_count(used) and token_count(size) and size > 0:
        return {"used": used, "size": size}
    return None


class SessionContext:
    def __init__(self, session):
        self.session = session
        self.last_measurement = None
        self.compaction = None

    @property
    def manual_compaction(self):
        return self.compaction is not None and self.compaction[1] == "manual"

    def restore(self, saved):
        self.last_measurement = measured_usage(saved.get("used"), saved.get("size")) if isinstance(saved, dict) else None

    async def publish_usage(self):
        stats = await self.session.rpc("get_session_stats", _timeout=5)
        context = stats.get("contextUsage")
        usage = measured_usage(context.get("tokens"), context.get("contextWindow")) if isinstance(context, dict) else None
        if usage is not None:
            self.last_measurement = usage
            await self.session.update({"sessionUpdate": "usage_update", **usage})
        else:
            await self.invalidate_usage()

    async def invalidate_usage(self):
        # ACP requires numeric used/size. Preserve the actual last measurement
        # for the wire, and mark it historical so Studio hides the indicator.
        if self.last_measurement is not None:
            await self.session.update({"sessionUpdate": "usage_update", **self.last_measurement,
                                       "_meta": {CONTEXT_UNKNOWN: True}})

    async def start_compaction(self, reason):
        identifier = "aelix-compact-" + uuid.uuid4().hex
        self.compaction = (identifier, reason)
        await self.session.update({"sessionUpdate": "tool_call", "toolCallId": identifier,
                                   "title": "/compact", "kind": "other", "status": "in_progress",
                                   "_meta": {COMPACTION: {"reason": reason, "state": "running"}}})

    async def finish_compaction(self, state, result=None):
        if self.compaction is None:
            return
        identifier, reason = self.compaction
        self.compaction = None
        details = {"reason": reason, "state": state}
        if isinstance(result, dict):
            before = result.get("tokens_before", result.get("tokensBefore"))
            if token_count(before):
                details["tokensBefore"] = before
        await self.session.update({"sessionUpdate": "tool_call_update", "toolCallId": identifier,
                                   "status": "completed" if state == "completed" else "failed",
                                   "_meta": {COMPACTION: details}})

    async def native_compaction_event(self, event):
        reason = event.get("reason")
        if reason not in AUTOMATIC_REASONS:
            return  # Manual command progress is owned by the explicit RPC.
        if event["type"] == "compaction_start":
            await self.start_compaction(reason)
        elif self.compaction is not None and self.compaction[1] == reason:
            result = event.get("result")
            if isinstance(result, dict):
                await self.invalidate_usage()
                await self.finish_compaction("completed", result)
            else:
                await self.finish_compaction("aborted" if event.get("aborted") is True else "failed")
