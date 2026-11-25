"""One standard ACP permission for one recorded-to-candidate profile change."""
from __future__ import annotations

import asyncio
import json
import uuid

from .profile_refs import POLICY_FIELDS, valid_preview, valid_ref
from ..wire.errors import ProtocolError

APPROVAL_SECONDS = 45
RESTORE_MESSAGE = "Restore the original profile, then reopen this session."


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
    return preview


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
            add("omittedPrompt", before["omittedPromptDigest"], after["omittedPromptDigest"])
    return changes


def changed(recorded, candidate, original, before=None, after=None):
    return {"accepted": False, "reason": "profile-changed", "recorded": recorded,
            "candidate": candidate,
            "differences": differences(recorded, candidate, before, after) if candidate else [],
            "recovery": recovery(original)}


async def approve(session, before, after):
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
    if getattr(server, "closing", False) or session.rpc_closed or session.cancelled:
        return "cancelled"
    task = asyncio.create_task(server.client_request("session/request_permission", {
        "sessionId": session.id,
        "toolCall": {"toolCallId": "profile-change-" + uuid.uuid4().hex,
                     "title": "Use the changed profile?", "kind": "read", "status": "pending",
                     "rawInput": {"description": "\n".join(lines), "recorded": original,
                                  "candidate": candidate, "differences": delta}},
        "options": [
            {"optionId": "allow-once", "name": "Use this profile once", "kind": "allow_once"},
            {"optionId": "reject-once", "name": "Restore the original profile", "kind": "reject_once"},
        ],
    }, withdraw=True, timeout=APPROVAL_SECONDS, max_response_bytes=4096))
    session.permissions.add(task)
    starting = session.id in server.starting_ids and session.id not in server.pending_stop_sessions
    if starting:
        server.pending_stop_sessions[session.id] = session
    try:
        response = await task
    except asyncio.CancelledError:
        if asyncio.current_task().cancelling():
            raise
        return "cancelled"
    except (TimeoutError, ProtocolError, OSError):
        return "unanswered"
    finally:
        session.permissions.discard(task)
        if starting and server.pending_stop_sessions.get(session.id) is session:
            del server.pending_stop_sessions[session.id]
    if session.cancelled or session.rpc_closed or getattr(server, "closing", False):
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
