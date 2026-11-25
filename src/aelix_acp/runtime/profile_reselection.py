"""One standard ACP permission for one recorded-to-candidate profile change."""
from __future__ import annotations

import asyncio
import json
import os
import uuid

from .profile_refs import DisplayTooLarge, POLICY_FIELDS, safe_preview, valid_preview, valid_ref
from ..wire.errors import ProtocolError
from ..wire.transport import encode_frame

APPROVAL_SECONDS = 45
APPROVAL_BYTES = 64 * 1024
MANUAL_APPROVAL_ENV = "AELIX_ACP_STUDIO_MANUAL_PROFILE_APPROVAL"
RESTORE_MESSAGE = "Restore the original profile, then reopen this session."


class Stopped(ProtocolError):
    def __init__(self):
        super().__init__("Profile operation cancelled.", -32004)


class Operation:
    """Lifetime of one write on one worker and binding generation."""

    def __init__(self, resources):
        self.resources = resources
        self.session = resources.session
        self.worker = self.session.proc
        self.generation = self.session.cancel_generation
        self.binding = resources.binding()
        self.dispatched = False
        self.no_effect = False
        self.retired = False
        self.permission = None

    @property
    def uncertain_effect(self):
        return self.dispatched and not self.no_effect

    def retire(self):
        self.retired = True
        if self.permission is not None:
            self.permission.cancel()

    def require_current(self):
        s = self.session
        if (self.retired or s.cancelled or s.cancel_generation != self.generation
                or s.proc is not self.worker or s.rpc_closed or s.ending
                or s.server.closing or s.server.input_closed or self.resources.binding() != self.binding
                or (self.binding is not None and self.binding == self.resources.retired_binding)):
            raise Stopped()

    def before_write(self):
        self.require_current()
        self.dispatched = True
        if self.resources.profile_operation is self:
            self.resources.invalidate_pages()



def recovery(original):
    return {"action": "restore-original-and-reopen", "original": original}


def load_error(reason, original):
    message = ("A revision-bound profile cannot be applied by this runtime."
               if reason == "guard-unavailable" else RESTORE_MESSAGE)
    return ProtocolError(message, -32004, {
        "kind": "profile_reselection_required", "reason": reason, "recovery": recovery(original),
    })


def recorded_profile(preferences):
    if "agentRef" not in preferences and "agentPreview" not in preferences:
        return None
    ref, preview = preferences.get("agentRef"), preferences.get("agentPreview")
    if not valid_ref(ref) or ref["name"] != preferences.get("agent"):
        raise load_error("invalid-record", None)
    if not valid_preview(preview, ref):
        raise load_error("invalid-record", ref)
    try:
        return safe_preview(preview)
    except DisplayTooLarge:
        raise load_error("invalid-record", ref) from None


def differences(before_ref, after_ref, before=None, after=None):
    changes = []

    def add(field, old, new):
        if old != new:
            changes.append({"field": field, "before": old, "after": new})

    if before and after:
        add("source", before["source"], after["source"])
    elif before_ref["scope"] != after_ref["scope"]:
        add("source", before_ref["scope"], after_ref["scope"])
    add("revision", before_ref["revision"], after_ref["revision"])
    if before and after:
        for field in ("tools", "approvalMode", "provider", "model", "promptMode", "promptPreview",
                      "promptTruncated", "skills", "description"):
            add(field, before[field], after[field])
        for field in POLICY_FIELDS:
            add(field, before["policy"][field], after["policy"][field])
        if before.get("omittedPromptDigest") and after.get("omittedPromptDigest"):
            if before["omittedPromptDigest"] != after["omittedPromptDigest"]:
                add("omittedPrompt", "omitted", "changed")
    return changes


def changed(recorded, candidate, original, before=None, after=None):
    return {"accepted": False, "reason": "profile-changed", "recorded": recorded,
            "candidate": candidate,
            "differences": differences(recorded, candidate, before, after) if candidate else [],
            "recovery": recovery(original)}


async def approve(operation, before, after):
    session = operation.session
    # Only an operator-controlled launch of a host that verifies human
    # confirmation and retires cards with their binding may enable this path.
    if (os.environ.get(MANUAL_APPROVAL_ENV) != "1" or operation.binding is None
            or not before.get("safeForConsent", True) or not after.get("safeForConsent", True)):
        return "unanswered"
    try:
        operation.require_current()
    except Stopped:
        return "cancelled"
    original, candidate = before["ref"], after["ref"]
    delta = differences(original, candidate, before, after)
    lines = [
        f"{original['name']}: {before['source']} ({original['revision']})",
        f"Candidate: {after['source']} ({candidate['revision']})",
    ]
    for field in delta:
        name = field["field"]
        if name == "omittedPrompt":
            lines.append("The omitted portion of the prompt changed.")
        elif name not in {"source", "revision"}:
            lines.append(f"{name}: {json.dumps(field['before'], ensure_ascii=False)} → "
                         f"{json.dumps(field['after'], ensure_ascii=False)}")
    if len(lines) == 2:
        lines.append("The file revision changed outside the displayed profile fields.")
        if before["promptTruncated"] and after["promptTruncated"]:
            lines.append("The omitted portion of the prompt may have changed.")
    lines.append("Use this version of the profile?")
    server = session.server
    params = {
        "sessionId": session.id,
        "toolCall": {"toolCallId": "profile-change-" + uuid.uuid4().hex,
                     "title": "Use the changed profile?", "kind": "read", "status": "pending",
                     "rawInput": {"description": "\n".join(lines), "recorded": original,
                                  "candidate": candidate, "differences": delta}},
        "options": [
            {"optionId": "allow-once", "name": "Use this profile once", "kind": "allow_once"},
            {"optionId": "reject-once", "name": "Restore the original profile", "kind": "reject_once"},
        ],
    }
    if len(encode_frame(params)) > APPROVAL_BYTES:
        return "unanswered"
    task = asyncio.create_task(server.client_request(
        "session/request_permission", params, withdraw=True, timeout=APPROVAL_SECONDS,
        max_response_bytes=4096, before_send=operation.require_current,
    ))
    operation.permission = task
    session.permissions.add(task)
    try:
        response = await task
    except asyncio.CancelledError:
        if asyncio.current_task().cancelling():
            raise
        return "cancelled"
    except Stopped:
        return "cancelled"
    except (TimeoutError, ProtocolError, OSError):
        return "unanswered"
    finally:
        operation.permission = None
        session.permissions.discard(task)
    try:
        operation.require_current()
    except Stopped:
        return "cancelled"
    outcome = response.get("outcome") if isinstance(response, dict) else None
    if not isinstance(outcome, dict):
        return "unanswered"
    if outcome.get("outcome") == "cancelled":
        return "cancelled"
    if outcome.get("outcome") == "selected":
        if outcome.get("optionId") == "allow-once":
            return None
        if outcome.get("optionId") == "reject-once":
            return "declined"
    return "unanswered"
