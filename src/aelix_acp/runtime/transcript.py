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
from .children import nested_only
from ..wire.errors import ProtocolError, SESSION_LOCKED

META = "aelix/transcript"
TOOL_ORIGIN_META = "aelix/transcriptOrigin"  # SQLite only
METHOD = "transcript/list"
SETTLED = "_aelix/transcript/settled"
VERSION = {"version": 1}
ROLES = {"user", "assistant", "toolResult"}
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


def tool_origins(message, coordinates):
    if coordinates["status"] != "stored" or not isinstance(message.get("content"), list):
        return
    for block in message["content"]:
        if isinstance(block, dict) and block.get("type") == "toolCall":
            call_id = block.get("tool_call_id") or block.get("id")
            if identifier(call_id):
                yield call_id, {key: coordinates[key] for key in ("nativeSessionId", "entryId")}


def display_update(update):
    meta = update.get("_meta")
    if isinstance(meta, dict) and TOOL_ORIGIN_META in meta:
        del meta[TOOL_ORIGIN_META]
        if not meta:
            del update["_meta"]
    return update


async def _tool_overlay(recorded, native_id, origins, nesting):
    """A provider call ID can recur on a different branch or in another turn."""
    recorded_origins = {}
    ambiguous = set()
    metadata, auxiliary = defaultdict(dict), []
    auxiliary_bytes = 0
    for index, update in enumerate(recorded):
        if index % 128 == 127:
            await asyncio.sleep(0)
        call_id = update.get("toolCallId")
        meta = update.get("_meta") or {}
        if not isinstance(meta, dict):
            continue
        coordinates = meta.get(META)
        adapter_event = isinstance(coordinates, dict) and coordinates.get("source") == "adapter"
        nested_event = nested_only(update)
        if not nested_event and isinstance(call_id, str) and (
                TOOL_ORIGIN_META in meta or update.get("sessionUpdate") == "tool_call"):
            origin = meta.get(TOOL_ORIGIN_META)
            key = (origin["nativeSessionId"], origin["entryId"]) if isinstance(origin, dict) else None
            if call_id in recorded_origins:
                ambiguous.add(call_id)
            recorded_origins[call_id] = key
        if adapter_event or nested_event:
            parent = meta.get("parentToolCallId")
            linked = parent is None or recorded_origins.get(parent) == (native_id, origins.get(parent))
            if adapter_event or (nesting and linked):
                auxiliary_bytes += len(json.dumps(update, ensure_ascii=False).encode("utf-8"))
                if auxiliary_bytes > MAX_BYTES or len(auxiliary) >= MAX_ROWS:
                    raise native_history.unavailable()
                auxiliary.append(update)
            continue
        if isinstance(call_id, str) and recorded_origins.get(call_id) == (native_id, origins.get(call_id)):
            metadata[call_id].update({key: value for key, value in meta.items() if key not in (META, TOOL_ORIGIN_META)})
    return metadata, auxiliary, ambiguous


async def reconcile(snapshot, recorded, *, nesting):
    """Project native membership exactly once, preserving associated ACP metadata."""
    native_id = snapshot.marker["nativeSessionId"]
    call_counts = Counter(update["toolCallId"] for row in snapshot.rows for update in row.updates
                          if update["sessionUpdate"] == "tool_call")
    result_counts = Counter(update["toolCallId"] for row in snapshot.rows for update in row.updates
                            if update["sessionUpdate"] == "tool_call_update")
    unique = {key for key in call_counts.keys() | result_counts.keys()
              if call_counts[key] <= 1 and result_counts[key] <= 1}
    origins = {update["toolCallId"]: row.coordinates["entryId"] for row in snapshot.rows for update in row.updates
               if update["sessionUpdate"] == "tool_call" and update["toolCallId"] in unique}
    metadata, auxiliary, ambiguous = await _tool_overlay(recorded, native_id, origins, nesting)
    associated = origins.keys() - ambiguous
    pending = defaultdict(deque)
    for row in snapshot.rows:
        part = 0
        if row.role == "assistant":
            pending.clear()
        for index, update in enumerate(row.updates):
            kind = update["sessionUpdate"]
            call_id = update.get("toolCallId")
            if call_id in associated and metadata.get(call_id):
                update = {**update, "_meta": metadata[call_id]}
            if kind == "tool_call":
                card_id = call_id if call_id in unique else _card_id(native_id, row, index)
                pending[call_id].append(card_id)
                update = {**update, "toolCallId": card_id}
            elif kind == "tool_call_update":
                if pending[call_id]:
                    card_id = pending[call_id].popleft()
                else:
                    card_id = call_id if call_id in unique else _card_id(native_id, row, index)
                    yield replay_update({"sessionUpdate": "tool_call", "toolCallId": card_id,
                                         "title": "tool", "kind": "other", "status": "in_progress"}, row, native_id, part)
                    part += 1
                update = {**update, "toolCallId": card_id}
            yield replay_update(update, row, native_id, part)
            part += 1
    for update in auxiliary:
        meta = update["_meta"]
        if "parentToolCallId" not in meta or meta["parentToolCallId"] in associated:
            yield update


def _card_id(native_id, row, part):
    name = json.dumps([native_id, row.coordinates["entryId"], part])
    return "aelix-transcript-" + uuid.uuid5(uuid.NAMESPACE_OID, name).hex
