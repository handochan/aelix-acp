"""Native transcript coordinates, bounded paging, and ACP display reconciliation."""

from __future__ import annotations

import asyncio
import base64
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
import json
import re
import uuid

from . import native_history
from .children import CHILD_META, SYNTHETIC_PREFIX, nested_only
from .subagents import META_KEY as SUBAGENTS_META
from ..wire.errors import ProtocolError, SESSION_LOCKED

META = "aelix/transcript"
CAPABILITY = "aelix/transcriptCapability"
TOOL_RESULT_META = "aelix/transcriptResult"  # SQLite only
TOOL_ORIGIN_META = "aelix/transcriptOrigin"  # SQLite only
TOOL_STREAM_META = "aelix/transcriptToolStream"  # SQLite only
METHOD = "transcript/list"
SETTLED = "_aelix/transcript/settled"
VERSION = {"version": 1}
ROLES = {"user", "assistant", "toolResult"}
DISPLAY_UPDATES = {"user_message_chunk", "agent_message_chunk", "agent_thought_chunk",
                   "tool_call", "tool_call_update", "plan"}
POSITIONS = ("entryId", "parentEntryId", "sequence")
MAX_PART = 196_608
MAX_BYTES = 64 * 1024**2
MAX_ROWS = native_history.MAX_UPDATES
CURSOR = re.compile(r"[A-Za-z0-9_-]{1,256}")


def offered(capabilities):
    value = (capabilities.get("_meta") or {}).get(META)
    return isinstance(value, dict) and type(value.get("version")) is int and value["version"] == 1


def supported(capabilities):
    return capabilities.get("version") == 1 and METHOD in capabilities.get("methods", [])


def tagged(update, coordinates):
    return {**update, "_meta": {**update.get("_meta", {}), META: coordinates}}


def identifier(value):
    return isinstance(value, str) and bool(value)


def position(row):
    entry, parent, sequence = (row[key] for key in POSITIONS)
    if not identifier(entry) or (parent is not None and not identifier(parent)) or type(sequence) is not int or sequence < 1:
        raise native_history.InvalidHistory
    return dict(zip(POSITIONS, (entry, parent, sequence)))


def live(event, role):
    data = event.get("transcript")
    if not isinstance(data, dict):
        return None
    keys = ("nativeSessionId", "bindingId", "streamId")
    if not all(identifier(data.get(key)) for key in keys):
        return None
    coordinates = {"source": "live", "role": role, **{key: data[key] for key in keys}}
    if event["type"] != "message_end":
        return coordinates
    status = data.get("status")
    if status == "stored":
        try:
            return {**coordinates, **position(data), "status": status, "replaced": data.get("replaced") is True}
        except (native_history.InvalidHistory, KeyError):
            return None
    if status in ("storageOnly", "notStored", "unknown"):
        return {**coordinates, "status": status}
    return None


@dataclass
class Row:
    coordinates: dict
    role: str
    updates: list


@dataclass
class Snapshot:
    marker: dict
    rows: list[Row]


def _page_coordinates(page):
    if not isinstance(page, dict):
        raise native_history.InvalidHistory
    native, binding, leaf, total = (page[key] for key in ("nativeSessionId", "bindingId", "leafId", "totalMessages"))
    recovered = page.get("recovered", False)
    if (not identifier(native) or not identifier(binding) or (leaf is not None and not identifier(leaf))
            or type(total) is not int or not 0 <= total <= MAX_ROWS or (total and leaf is None)
            or type(recovered) is not bool):
        raise native_history.InvalidHistory
    return native, binding, leaf, total, recovered


def _decode_part(part, expected_offset):
    offset, total, encoded = (part[key] for key in ("offset", "totalBytes", "base64"))
    if (type(offset) is not int or offset != expected_offset or type(total) is not int or not offset < total <= MAX_BYTES
            or not isinstance(encoded, str) or len(encoded) > MAX_PART * 4 // 3):
        raise native_history.InvalidHistory
    block = base64.b64decode(encoded, validate=True)
    if not block or len(block) > MAX_PART or offset + len(block) > total:
        raise native_history.InvalidHistory
    return total, block


async def read(rpc, project):
    """Finish a pinned scan before any display frame can be published."""
    try:
        async with asyncio.timeout(120):
            return await _scan(rpc, project)
    except ProtocolError as error:
        if error.code == SESSION_LOCKED:
            raise
        raise native_history.unavailable() from None
    except (OSError, TimeoutError, ValueError, TypeError, KeyError, RecursionError, native_history.InvalidHistory):
        raise native_history.unavailable() from None


async def _scan(rpc, project):
    rows, seen, cursors = [], set(), set()
    byte_count = update_count = 0
    snapshot = fragment_key = None
    fragment = bytearray()
    params = {"limit": 100}
    while True:
        page = await rpc("aelix_control", method=METHOD, params=params)
        current = _page_coordinates(page)
        if snapshot is None:
            snapshot = current
        entries, cursor = page["entries"], page["nextCursor"]
        if (current != snapshot or not isinstance(entries, list) or len(entries) > 100
                or (cursor is not None and (not isinstance(cursor, str) or not CURSOR.fullmatch(cursor)
                                            or cursor in cursors or not entries))):
            raise native_history.InvalidHistory
        for entry in entries:
            coordinates = position(entry)
            if (coordinates["sequence"] != len(rows) + 1 or len(rows) >= snapshot[3]
                    or coordinates["entryId"] in seen):
                raise native_history.InvalidHistory
            if "messagePart" in entry:
                if len(entries) != 1 or "message" in entry or "unavailable" in entry:
                    raise native_history.InvalidHistory
                total, part = _decode_part(entry["messagePart"], len(fragment))
                key = coordinates, total
                if fragment_key is not None and fragment_key != key:
                    raise native_history.InvalidHistory
                fragment_key = key
                fragment.extend(part)
                if len(fragment) < total:
                    continue
                message = json.loads(fragment.decode("utf-8"))
                fragment.clear()
                fragment_key = None
            elif fragment_key is not None:
                raise native_history.InvalidHistory
            else:
                message = entry.get("message")
            if isinstance(message, dict):
                role = message.get("role")
                if role not in ROLES or "unavailable" in entry:
                    raise native_history.InvalidHistory
                projected = iter(project(message))
            elif entry.get("unavailable") == "unencodable" and entry.get("role") in ROLES and "message" not in entry:
                role, projected = entry["role"], iter(())
            else:
                raise native_history.InvalidHistory
            row = Row(coordinates, role, [])
            for index, update in enumerate(_with_placeholder(projected, role, "unavailable" in entry)):
                wire = replay_update(update, row, snapshot[0], index)
                byte_count += len(json.dumps(wire, ensure_ascii=False, allow_nan=False).encode("utf-8"))
                update_count += 1
                if byte_count > MAX_BYTES or update_count > MAX_ROWS:
                    raise native_history.InvalidHistory
                row.updates.append(update)
                if index % 128 == 127:
                    await asyncio.sleep(0)
            rows.append(row)
            seen.add(coordinates["entryId"])
            await asyncio.sleep(0)
        if cursor is None:
            if fragment_key is not None or len(rows) != snapshot[3]:
                raise native_history.InvalidHistory
            native, _, leaf, total, recovered = snapshot
            return Snapshot({**VERSION, "nativeSessionId": native, "leafId": leaf,
                             "totalMessages": total, "recovered": recovered}, rows)
        cursors.add(cursor)
        params = {"limit": 100, "cursor": cursor}


def _with_placeholder(updates, role, unavailable):
    first = next(updates, None)
    if first is None:
        text = "[Native message unavailable.]" if unavailable else ""
        kind = "user_message_chunk" if role == "user" else "agent_message_chunk"
        yield {"sessionUpdate": kind, "content": native_history.text_block(text)}
    else:
        yield first
        yield from updates


def replay_update(update, row, native_id, index):
    return tagged(update, {"source": "replay", "nativeSessionId": native_id, **row.coordinates,
                           "role": row.role, "part": index})


def stream_key(coordinates):
    return tuple(coordinates[key] for key in ("nativeSessionId", "bindingId", "streamId"))


def _origin_key(origin):
    if isinstance(origin, dict):
        return origin["nativeSessionId"], origin["entryId"], origin.get("occurrence", 0)
    return None


def result_link(update):
    meta = update.get("_meta")
    return meta.get(TOOL_RESULT_META) if isinstance(meta, dict) else None


def recorded_result(call_id, origin, coordinates):
    return {"sessionUpdate": "tool_call_update", "toolCallId": call_id,
            "_meta": {TOOL_RESULT_META: {"nativeSessionId": coordinates["nativeSessionId"],
                                       "entryId": coordinates["entryId"], "origin": origin}}}


def display_update(update):
    meta = update.get("_meta")
    if isinstance(meta, dict) and (TOOL_ORIGIN_META in meta or TOOL_STREAM_META in meta):
        meta.pop(TOOL_ORIGIN_META, None)
        meta.pop(TOOL_STREAM_META, None)
        if not meta:
            del update["_meta"]
    return update


def _recorded_tool_stream(meta):
    # Earlier adapters kept live coordinates only when the client opted in.
    coordinates = meta.get(TOOL_STREAM_META, meta.get(META))
    if isinstance(coordinates, dict) and coordinates.get("source") == "live" and coordinates.get("role") == "toolResult":
        if all(identifier(coordinates.get(key)) for key in ("nativeSessionId", "bindingId", "streamId")):
            return stream_key(coordinates)
    return None


def _recorded_tool_fields(meta, previous_children=()):
    fields = {key: value for key, value in meta.items() if key not in (META, TOOL_ORIGIN_META, TOOL_STREAM_META)}
    if previous_children and (subagents := fields.get(SUBAGENTS_META)):
        # A native tool update has a verified stream, but the live ACP
        # rollup can also contain late events routed by a reused raw ID.
        fields[SUBAGENTS_META] = {**subagents,
            "children": [child for child in subagents["children"] if child["id"] not in previous_children]}
    return fields


def _occurrences(rows, native_id):
    calls = {}
    for row in rows:
        entry_id = row.coordinates["entryId"]
        counts = Counter()
        for index, update in enumerate(row.updates):
            if update["sessionUpdate"] == "tool_call":
                call_id = update["toolCallId"]
                calls[entry_id, index] = call_id, (native_id, entry_id, counts[call_id])
                counts[call_id] += 1
    return calls


def _synthetic_parent(meta, parent, synthetic_parents, native_calls):
    child = meta.get(CHILD_META)
    child_id = child.get("id") if isinstance(child, dict) else None
    if identifier(child_id):
        return parent == SYNTHETIC_PREFIX + child_id
    if parent in synthetic_parents:
        # A legacy record without a child ID is ambiguous if its parent can
        # also be a native call. A native tool can legally reuse the prefix.
        return None if parent in native_calls else True
    return False


async def _tool_overlay(recorded, native_id, selected, unique_origins, result_entries, *, session_id, nesting):
    """Overlay only the metadata whose recorded call is still on this path."""
    recorded_origins, streams = {}, {}
    native_calls = {call for call, _ in selected}
    ambiguous, synthetic_parents = set(), set()
    links = {}
    metadata, scoped_metadata, auxiliary = defaultdict(dict), defaultdict(dict), []
    auxiliary_bytes = 0
    for index, (store, sequence, update) in enumerate(recorded):
        if index % 128 == 127:
            await asyncio.sleep(0)
        call_id = update.get("toolCallId")
        if link := result_link(update):
            if link["nativeSessionId"] == native_id and link["entryId"] in result_entries:
                links[link["entryId"]] = (call_id, _origin_key(link["origin"]))
            continue
        meta = update.get("_meta") or {}
        if not isinstance(meta, dict):
            continue
        coordinates = meta.get(META)
        adapter_event = isinstance(coordinates, dict) and coordinates.get("source") == "adapter"
        nested_event = nested_only(update)
        stream = _recorded_tool_stream(meta)
        if not nested_event and isinstance(call_id, str) and (
                TOOL_ORIGIN_META in meta or update.get("sessionUpdate") == "tool_call"):
            origin = _origin_key(meta.get(TOOL_ORIGIN_META))
            if call_id in recorded_origins:
                ambiguous.add(call_id)
            recorded_origins[call_id] = origin
            occurrence = (call_id, origin)
            if stream is not None and stream[0] == native_id and occurrence in selected:
                private = meta.get(TOOL_STREAM_META, {})
                streams[stream] = (occurrence, set(private.get("previousChildren", ())))
        if adapter_event or nested_event:
            if nested_event and not nesting:
                continue
            parent = meta.get("parentToolCallId")
            if (nested_event and parent is None and isinstance(call_id, str)
                    and call_id.startswith(SYNTHETIC_PREFIX) and update.get("sessionUpdate") == "tool_call"):
                synthetic_parents.add(call_id)
            synthetic = False if parent is None else _synthetic_parent(meta, parent, synthetic_parents, native_calls)
            linked = (parent is None or (synthetic is True and parent in synthetic_parents)
                      or (synthetic is False and parent in unique_origins
                          and recorded_origins.get(parent) == unique_origins[parent]))
            if not nested_event or linked:
                if not adapter_event:
                    # Use the persisted position, which is independent of
                    # replay filtering and the lifetime of the worker.
                    binding = f"acp-history:{store}:{session_id}"
                    update = tagged(update, {"source": "adapter", "eventId": f"{binding}:{sequence}",
                                             "bindingId": binding, "sequence": sequence})
                auxiliary_bytes += len(json.dumps(update, ensure_ascii=False).encode("utf-8"))
                if auxiliary_bytes > MAX_BYTES or len(auxiliary) >= MAX_ROWS:
                    raise native_history.unavailable()
                auxiliary.append((update, synthetic))
            continue

        if not isinstance(call_id, str):
            continue
        if stream is not None:
            scope = streams.get(stream)
            if scope is None or scope[0][0] != call_id:
                continue
            occurrence, previous_children = scope
            fields = _recorded_tool_fields(meta, previous_children)
            scoped_metadata[occurrence].update(fields)
        elif call_id in unique_origins and recorded_origins.get(call_id) == unique_origins[call_id]:
            occurrence = (call_id, unique_origins[call_id])
            fields = _recorded_tool_fields(meta)
        else:
            continue
        metadata[occurrence].update(fields)

    # A bare raw ID cannot identify a late frame after reuse, even when
    # only one of its historical occurrences is on the selected path.
    associated = unique_origins.keys() - ambiguous
    return ({occurrence: fields if occurrence[0] in associated else scoped_metadata.get(occurrence)
             for occurrence, fields in metadata.items()}, auxiliary, associated, links)


async def reconcile(snapshot, recorded, *, session_id, nesting):
    """Project native membership exactly once, preserving associated ACP metadata."""
    native_id = snapshot.marker["nativeSessionId"]
    calls = _occurrences(snapshot.rows, native_id)
    call_counts = Counter(call_id for call_id, _ in calls.values())
    unique_origins = {call_id: origin for call_id, origin in calls.values() if call_counts[call_id] == 1}
    result_entries = {row.coordinates["entryId"] for row in snapshot.rows if row.role == "toolResult"}
    metadata, auxiliary, associated, links = await _tool_overlay(
        recorded, native_id, set(calls.values()), unique_origins, result_entries, session_id=session_id, nesting=nesting)
    result_counts = Counter(update["toolCallId"] for row in snapshot.rows for update in row.updates
                            if update["sessionUpdate"] == "tool_call_update" and row.coordinates["entryId"] not in links)
    pending = {}
    used_cards = set()
    current_calls = defaultdict(deque)
    for row in snapshot.rows:
        part = 0
        entry_id = row.coordinates["entryId"]
        if row.role == "assistant":
            current_calls.clear()
        for index, update in enumerate(row.updates):
            occurrence = None
            kind = update["sessionUpdate"]
            call_id = update.get("toolCallId")
            if kind == "tool_call":
                card_id = call_id if call_counts[call_id] == 1 else _card_id(native_id, row, index)
                occurrence = calls[entry_id, index]
                pending[occurrence] = card_id
                current_calls[call_id].append(occurrence)
                update = {**update, "toolCallId": card_id}
                used_cards.add(card_id)
            elif kind == "tool_call_update":
                original = links.get(entry_id)
                original_id = original[0] if original is not None else call_id
                card_id = None
                if original is not None and original[1] is not None:
                    card_id = pending.pop(original, None)
                    if card_id is not None:
                        occurrence = original
                elif original_id == call_id:
                    # If the assistant receipt was lost, retain the usual raw
                    # match for unchanged IDs on the current assistant.
                    queue = current_calls[call_id]
                    while queue and card_id is None:
                        candidate = queue.popleft()
                        card_id = pending.pop(candidate, None)
                        if card_id is not None:
                            occurrence = candidate
                if card_id is None:
                    raw_id = update["toolCallId"]
                    if original is None and not call_counts[raw_id] and result_counts[raw_id] == 1:
                        card_id = raw_id
                    else:
                        card_id = _card_id(native_id, row, index)
                    used_cards.add(card_id)
                    yield replay_update({"sessionUpdate": "tool_call", "toolCallId": card_id,
                                         "title": "tool", "kind": "other", "status": "in_progress"}, row, native_id, part)
                    part += 1
                update = {**update, "toolCallId": card_id}
            if fields := metadata.get(occurrence):
                update = {**update, "_meta": fields}
            yield replay_update(update, row, native_id, part)
            part += 1
    replay_cards = {}
    for update, synthetic in auxiliary:
        meta = update["_meta"]
        parent = meta.get("parentToolCallId")
        if parent is not None and not synthetic and parent not in associated:
            continue
        call_id = update.get("toolCallId")
        kind = update["sessionUpdate"]
        if nested_only(update) and isinstance(call_id, str) and kind in {"tool_call", "tool_call_update"}:
            replay_parent = replay_cards.get(parent, (parent, None))[0] if synthetic else parent
            if kind == "tool_call":
                # A child may recycle a raw ID after tool_end. Its next
                # creation and updates belong to a distinct replay card.
                replay_id = call_id
                if replay_id in used_cards:
                    event = meta[META]["eventId"]
                    replay_id = "aelix-transcript-nested-" + uuid.uuid5(uuid.NAMESPACE_OID, event).hex
                used_cards.add(replay_id)
                replay_cards[call_id] = replay_id, replay_parent
            else:
                replay_id, replay_parent = replay_cards.get(call_id, (call_id, replay_parent))
            if replay_id != call_id:
                update = {**update, "toolCallId": replay_id}
            if replay_parent != parent:
                update = {**update, "_meta": {**meta, "parentToolCallId": replay_parent}}
        yield update


def _card_id(native_id, row, part):
    name = json.dumps([native_id, row.coordinates["entryId"], part])
    return "aelix-transcript-" + uuid.uuid5(uuid.NAMESPACE_OID, name).hex
