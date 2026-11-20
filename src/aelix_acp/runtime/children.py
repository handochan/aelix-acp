"""Delegated children over ACP (aelix-ai #612): native `aelix_child` frames, routed by parent call.

Native emits one `aelix_child` frame per change of a delegated child (aelix-ai
ADR-0307): `{type: "aelix_child", event, data: {id, seq, epoch, ...}}`, with a
closed set of seven events. Unlike `aelix_subagent`, every child's `state`
frame names the `agent` tool call that started it (`parentToolCallId`, null for
`/agents run`), so a spawned child's later frames still find their card after
that call ended and after the next prompt cleared the per-turn tracker.

`ChildRoutes` keeps that association per `(epoch, id)`: bounded, dropped on a
child's terminal frame, and dropped for a whole epoch when a frame of a newer
one arrives (a native reload or session change builds a new epoch, whose
children are new). Frames are deduplicated by `(epoch, id, seq)`. A frame of a
child whose first `state` frame was never seen is not routed.
"""
from __future__ import annotations

from collections import OrderedDict, deque
import hashlib
import json

CHILD_EVENTS = frozenset({"state", "tool_start", "tool_end", "text", "stuck", "wrap_up", "approval"})
# Client capabilities (`clientCapabilities._meta`) this adapter honours.
NESTED_TOOL_CALLS = "aelix/nestedToolCalls"
NOTICES = "aelix/notices"
CHILD_META = "aelix/child"
# The top-level card a nesting client gets for a child with no parent call.
SYNTHETIC_PREFIX = "aelix-child:"
# The adapter's own bound on the live children it routes (an ended child's route is
# dropped); unrelated to native's 64 kept ended rows (fix round 1, V N3).
MAX_CHILDREN = 64
RETIRED_EPOCHS = 8
# Native's bound on a call id as the wire writes it, and the prefix of a hashed one.
WIRE_ID_BYTES = 128
HASHED_PREFIX = "h:"
# ACP tool-call status for a child's own call and for a child's whole run.
TOOL_STATUS = {"ok": "completed", "failed": "failed", "no_result": "failed"}
END_STATUS = {"completed": "completed", "wrapped_up": "completed"}


def _key(event, data):
    if event not in CHILD_EVENTS or not isinstance(data, dict):
        return None
    child, seq, epoch = data.get("id"), data.get("seq"), data.get("epoch")
    if not isinstance(child, str) or not child or not isinstance(epoch, str) or not epoch:
        return None
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        return None
    return (epoch, child), seq


def wire_call_id(raw: str) -> str:
    """A call id as native's `aelix_child` frames and child questions carry it (fix round 1, Codex C-L6).

    The same rule as aelix-ai `aelix_agents/child_events.py` `wire_call_id`:
    `raw` when it is at most 128 bytes as JSON writes it without its quotes
    (UTF-8; a control character escapes to six bytes), encodable and outside the
    `h:` namespace, else `h:` and the first 32 hex digits of its sha256.
    """
    short = False
    if len(raw) <= WIRE_ID_BYTES:
        try:
            short = len(json.dumps(raw, ensure_ascii=False).encode("utf-8")) - 2 <= WIRE_ID_BYTES
        except UnicodeEncodeError:
            short = False
    if short and not raw.startswith(HASHED_PREFIX):
        return raw
    return HASHED_PREFIX + hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()[:32]


def nested_only(update) -> bool:
    """A recorded update only a client that declared `aelix/nestedToolCalls` is sent (fix round 1, Codex C-M4).

    A child's own call (`_meta.parentToolCallId`) or the top-level card of a
    child with no parent call. `session/load` replays neither to a client that
    did not declare it, as the live path sends it neither; the parent card's
    `_meta` updates are the same for both and replay to every client.
    """
    meta = update.get("_meta") if isinstance(update, dict) else None
    if not isinstance(meta, dict):
        return False
    call = update.get("toolCallId")
    return "parentToolCallId" in meta or (isinstance(call, str) and call.startswith(SYNTHETIC_PREFIX)
                                          and CHILD_META in meta)


def terminal(event, data) -> bool:
    """A child's last frame: the `state` frame that carries how it ended."""
    return event == "state" and isinstance(data, dict) and data.get("end") is not None


class ChildRoutes:
    """`(epoch, id) -> parentToolCallId` for live children, outliving the per-prompt tracker."""

    def __init__(self):
        self.epoch = None
        self.retired = deque(maxlen=RETIRED_EPOCHS)
        self.parents = OrderedDict()
        self.seqs = {}
        # The card fields each live child last showed (fix round 1, V-F1): a frame
        # after the next prompt's per-turn clear keeps its profile and state.
        self.known = {}
        # Each live child's own calls still open, in start order (fix round 1, Codex
        # C-M3): what a worker that ended without its terminal frames left open.
        self.calls = {}
        # Ended children's last seq, so a repeated terminal frame is still a duplicate.
        self.ended_seqs = OrderedDict()
        # A parent call's id as native names it -> the id of its card, for ids native hashes.
        self.aliases = OrderedDict()

    def observe(self, epoch) -> bool:
        """Native's current epoch is `epoch`; False for a retired one. A newer one drops the older's routes."""
        if epoch == self.epoch:
            return True
        if epoch in self.retired:
            return False
        self.retire()
        self.epoch = epoch
        return True

    def retire(self):
        """The current epoch is over (a newer one began, or its worker ended): drop every route of it."""
        if self.epoch is not None:
            self.retired.append(self.epoch)
        self.epoch = None
        for table in (self.parents, self.seqs, self.ended_seqs, self.known, self.calls):
            table.clear()

    def call_started(self, call_id):
        """A tool call's card opened: remember its id under the name native's frames will use."""
        if isinstance(call_id, str) and (named := wire_call_id(call_id)) != call_id:
            self.aliases[named] = call_id
            self.aliases.move_to_end(named)
            while len(self.aliases) > MAX_CHILDREN:
                self.aliases.popitem(last=False)

    def card_of(self, parent):
        """The card of the parent call native names `parent`."""
        return self.aliases.get(parent, parent)

    def accept(self, event, data):
        """`(key, parent)` for a frame to route, or None for one to drop."""
        parsed = _key(event, data)
        if parsed is None:
            return None
        key, seq = parsed
        if not self.observe(key[0]):
            return None
        if seq <= self.seqs.get(key, self.ended_seqs.get(key, 0)):
            return None
        if event == "state":
            parent = data.get("parentToolCallId")
            self.parents[key] = self.card_of(parent) if isinstance(parent, str) and parent else None
            self.parents.move_to_end(key)
            while len(self.parents) > MAX_CHILDREN:
                evicted, _parent = self.parents.popitem(last=False)
                for table in (self.seqs, self.known, self.calls):
                    table.pop(evicted, None)
        elif key not in self.parents:
            return None
        self.seqs[key] = seq
        return key, self.parents[key]

    def remember(self, key, fields) -> dict:
        """Every card field the child `key` has shown, `fields` folded in."""
        known = self.known.setdefault(key, {})
        known.update(fields)
        return dict(known)

    def tool(self, key, call_id, name=None) -> str | None:
        """A live child's own call `call_id` started (its tool `name`), or ended (None): the tool it runs now."""
        calls = self.calls.setdefault(key, {})
        if name is None:
            calls.pop(call_id, None)
        else:
            calls[call_id] = name
            while len(calls) > MAX_CHILDREN:
                calls.pop(next(iter(calls)))
        return next(reversed(calls.values()), None)

    def live(self):
        """`(key, parent, seq, open calls)` for each live child, oldest first."""
        return [(key, parent, self.seqs.get(key, 0), list(self.calls.get(key, ())))
                for key, parent in self.parents.items()]

    def ended(self, key):
        self.parents.pop(key, None)
        self.known.pop(key, None)
        self.calls.pop(key, None)
        self.ended_seqs[key] = self.seqs.pop(key, 0)
        self.ended_seqs.move_to_end(key)
        while len(self.ended_seqs) > MAX_CHILDREN:
            self.ended_seqs.popitem(last=False)

    def stale(self, epoch) -> bool:
        """A child approval from an epoch native has since replaced (aelix-ai ADR-0308)."""
        return epoch in self.retired
