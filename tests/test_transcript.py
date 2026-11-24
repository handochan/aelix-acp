"""Transcript/list validation and ACP recovery from recorded display events."""

import base64
import json
import unittest

from aelix_acp.runtime import transcript
from aelix_acp.wire.errors import ProtocolError


def page(entries, cursor=None, **coordinates):
    return {"nativeSessionId": "native", "bindingId": "binding", "leafId": "tail",
            "totalMessages": 3, "entries": entries, "nextCursor": cursor, **coordinates}


def row(sequence, entry, **payload):
    return {"sequence": sequence, "entryId": entry, "parentEntryId": None, **payload}


def text_message(text, role="user"):
    return {"role": role, "content": text}


def project(message):
    yield {"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": message["content"]}}


def part(offset, total, data):
    return {"offset": offset, "totalBytes": total, "base64": base64.b64encode(data).decode()}


class TranscriptPageTests(unittest.IsolatedAsyncioTestCase):
    async def scan(self, *pages):
        replies = iter(pages)
        cursors = []
        async def rpc(command, *, method, params):
            self.assertEqual((command, method), ("aelix_control", "transcript/list"))
            cursors.append(params.get("cursor"))
            return next(replies)
        result = await transcript.read(rpc, project)
        self.assertEqual(cursors, [None] + [p["nextCursor"] for p in pages[:-1]])
        return result

    async def test_contiguous_utf8_parts_and_unavailable_row(self):
        message = "분할된 메시지"
        payload = json.dumps(text_message(message), ensure_ascii=False).encode()
        split = payload.index(message.encode()) + 1
        snapshot = await self.scan(
            page([row(1, "head", message=text_message("first"))], "row-2"),
            page([row(2, "mid", messagePart=part(0, len(payload), payload[:split]))], "rest-2"),
            page([row(2, "mid", messagePart=part(split, len(payload), payload[split:]))], "row-3"),
            page([row(3, "tail", unavailable="unencodable", role="assistant")]),
        )
        self.assertEqual(snapshot.marker, {"version": 1, "nativeSessionId": "native", "leafId": "tail",
                                           "totalMessages": 3, "recovered": False})
        updates = [update async for update in transcript.reconcile(snapshot, (), nesting=False)]
        self.assertEqual([update["content"]["text"] for update in updates],
                         ["first", message, "[Native message unavailable.]"])
        self.assertEqual([update["_meta"][transcript.META]["entryId"] for update in updates],
                         ["head", "mid", "tail"])

    async def test_rejects_a_changed_binding_duplicate_or_interrupted_fragment(self):
        first = page([row(1, "first", message=text_message("first"))], "row-2")
        for later in (
            page([row(2, "second", message=text_message("second"))], bindingId="changed"),
            page([row(2, "first", message=text_message("duplicate"))]),
            page([row(2, "second", messagePart=part(2, 5, b"def"))]),
            page([row(2, "second", messagePart=part(0, 5, b"abc"))]),
        ):
            with self.subTest(later=later), self.assertRaises(ProtocolError) as error:
                await self.scan(first, later)
            self.assertEqual(error.exception.data, {"kind": "native_history_unavailable"})


    async def test_native_pipe_loss_uses_fixed_diagnostic(self):
        async def rpc(*args, **kwargs):
            raise BrokenPipeError("private child diagnostic")
        with self.assertRaises(ProtocolError) as error:
            await transcript.read(rpc, project)
        self.assertEqual(str(error.exception), "Native session history is unavailable")
        self.assertEqual(error.exception.data, {"kind": "native_history_unavailable"})


class TranscriptToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_raw_tool_ids_do_not_merge_distinct_results(self):
        call = {"sessionUpdate": "tool_call", "toolCallId": "same", "status": "in_progress"}
        result = {"sessionUpdate": "tool_call_update", "toolCallId": "same", "status": "completed"}
        rows = [
            transcript.Row(transcript.position(row(index, str(index))), role, [update])
            for index, (role, update) in enumerate(
                [("assistant", call), ("toolResult", result), ("assistant", call), ("toolResult", result)], 1
            )
        ]
        snapshot = transcript.Snapshot({"nativeSessionId": "native"}, rows)
        updates = [update async for update in transcript.reconcile(snapshot, (), nesting=True)]
        tool_ids = [update["toolCallId"] for update in updates]
        self.assertEqual(tool_ids[0], tool_ids[1])
        self.assertEqual(tool_ids[2], tool_ids[3])
        self.assertNotEqual(tool_ids[0], tool_ids[2])
        self.assertEqual([update["_meta"][transcript.META]["sequence"] for update in updates], [1, 2, 3, 4])


    async def test_overlay_requires_the_same_native_origin_as_the_recorded_tool(self):
        call = {"sessionUpdate": "tool_call", "toolCallId": "same", "status": "in_progress"}
        progress = {"sessionUpdate": "tool_call_update", "toolCallId": "same",
                    "_meta": {"aelix/subagents": {"children": [{"id": "child"}]}}}
        snapshot = transcript.Snapshot({"nativeSessionId": "native"}, [
            transcript.Row(transcript.position(row(1, "current")), "assistant", [call]),
        ])
        for origin, reused, preserve in (("withdrawn", False, False), ("current", False, True),
                                           ("current", True, False)):
            start = {**call, "_meta": {transcript.TOOL_ORIGIN_META: {"nativeSessionId": "native", "entryId": origin}}}
            unproven = {**call, "sessionUpdate": "tool_call_update", "_meta": {transcript.TOOL_ORIGIN_META: None}}
            recorded = [start, unproven, progress] if reused else [start, progress]
            with self.subTest(origin=origin, reused=reused):
                replay = [update async for update in transcript.reconcile(snapshot, recorded, nesting=True)]
                self.assertEqual(len(replay), 1)
                self.assertEqual("aelix/subagents" in replay[0]["_meta"], preserve)
