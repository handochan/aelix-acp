"""Transcript/list validation and ACP recovery from recorded display events."""

import base64
import json
import unittest

from aelix_acp.runtime import transcript
from aelix_acp.runtime.children import CHILD_META
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


def records(updates):
    return (("events", sequence, update) for sequence, update in enumerate(updates, 1))


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
        updates = [update async for update in transcript.reconcile(snapshot, (), session_id="acp", nesting=False)]
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
    async def test_nested_replay_links_updates_to_the_correct_recreated_parent(self):
        parent = "aelix-child:child"
        child = "child:tool"
        parent_start = {"sessionUpdate": "tool_call", "toolCallId": parent, "_meta": {CHILD_META: {}}}
        child_start = {"sessionUpdate": "tool_call", "toolCallId": child, "_meta": {"parentToolCallId": parent}}
        child_end = {**child_start, "sessionUpdate": "tool_call_update", "status": "completed"}
        snapshot = transcript.Snapshot({"nativeSessionId": "native"}, [])
        replay = [update async for update in transcript.reconcile(
            snapshot, records([parent_start, child_start, child_end] * 2), session_id="acp", nesting=True)]
        first_parent, first_child, first_end, second_parent, second_child, second_end = replay
        self.assertNotEqual(first_parent["toolCallId"], second_parent["toolCallId"])
        self.assertNotEqual(first_child["toolCallId"], second_child["toolCallId"])
        self.assertEqual(first_end["toolCallId"], first_child["toolCallId"])
        self.assertEqual(second_end["toolCallId"], second_child["toolCallId"])
        for frame in (second_child, second_end):
            self.assertEqual(frame["_meta"]["parentToolCallId"], second_parent["toolCallId"])

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
        progress = [{"version": 1, "children": [{"id": f"child-{index}"}]} for index in (1, 2)]
        recorded = []
        for index, (start_seq, end_seq) in enumerate(((1, 2), (3, 4))):
            # The same stream ID on another binding belongs to another call.
            coordinates = {"source": "live", "role": "toolResult", "nativeSessionId": "native",
                           "bindingId": f"worker-{index}", "streamId": "execution"}
            key = transcript.META if index == 0 else transcript.TOOL_STREAM_META
            origin = {"nativeSessionId": "native", "entryId": str(start_seq)}
            recorded.extend([
                {**call, "_meta": {transcript.TOOL_ORIGIN_META: origin, key: coordinates}},
                {**result, "_meta": {key: coordinates, "aelix/subagents": progress[index]}},
                transcript.recorded_result("same", origin, {"nativeSessionId": "native", "entryId": str(end_seq)}),
            ])
        recorded.append({**result, "_meta": {"aelix/subagents": {"children": [{"id": "ambiguous-late-child"}]}}})
        updates = [update async for update in transcript.reconcile(snapshot, records(recorded), session_id="acp", nesting=True)]
        tool_ids = [update["toolCallId"] for update in updates]
        self.assertEqual(tool_ids[0], tool_ids[1])
        self.assertEqual(tool_ids[2], tool_ids[3])
        self.assertNotEqual(tool_ids[0], tool_ids[2])
        self.assertEqual([update["_meta"][transcript.META]["sequence"] for update in updates], [1, 2, 3, 4])
        self.assertEqual([update["_meta"]["aelix/subagents"] for update in updates],
                         [progress[0], progress[0], progress[1], progress[1]])
        self.assertTrue(all(set(update["_meta"]) == {transcript.META, "aelix/subagents"} for update in updates))

    async def test_nested_replay_distinguishes_a_native_and_synthetic_parent_with_the_same_id(self):
        parent = "aelix-child:standalone"
        native = {"sessionUpdate": "tool_call", "toolCallId": parent, "title": "agent"}
        synthetic = {"sessionUpdate": "tool_call", "toolCallId": parent,
                     "_meta": {CHILD_META: {"id": "standalone"}}}
        started = {**native, "_meta": {transcript.TOOL_ORIGIN_META: {"nativeSessionId": "native", "entryId": "entry"}}}

        def nested(child):
            return {"sessionUpdate": "tool_call", "toolCallId": f"{child}:tool",
                    "_meta": {"parentToolCallId": parent, CHILD_META: {"id": child}}}

        snapshot = transcript.Snapshot({"nativeSessionId": "native"}, [
            transcript.Row(transcript.position(row(1, "entry")), "assistant", [native])])
        ambiguous = {**nested("legacy"), "_meta": {"parentToolCallId": parent}}
        recorded = records([started, synthetic, nested("standalone"), nested("delegated"), ambiguous])
        replay = [update async for update in transcript.reconcile(snapshot, recorded, session_id="acp", nesting=True)]
        native_card, synthetic_card, standalone, delegated = replay
        self.assertEqual(native_card["toolCallId"], parent)
        self.assertNotEqual(synthetic_card["toolCallId"], parent)
        self.assertEqual(standalone["_meta"]["parentToolCallId"], synthetic_card["toolCallId"])
        self.assertEqual(delegated["_meta"]["parentToolCallId"], native_card["toolCallId"])

    async def test_rewritten_duplicate_calls_follow_receipt_occurrences(self):
        call = {"sessionUpdate": "tool_call", "toolCallId": "same", "status": "in_progress"}
        result = {"sessionUpdate": "tool_call_update", "toolCallId": "changed", "status": "completed"}
        snapshot = transcript.Snapshot({"nativeSessionId": "native"}, [
            transcript.Row(transcript.position(row(1, "origin")), "assistant", [call, call]),
            transcript.Row(transcript.position(row(2, "result-1")), "toolResult", [result]),
            transcript.Row(transcript.position(row(3, "result-2")), "toolResult", [result]),
        ])
        recorded = [transcript.recorded_result(
            "same", {"nativeSessionId": "native", "entryId": "origin", "occurrence": occurrence},
            {"nativeSessionId": "native", "entryId": f"result-{index}"})
            for index, occurrence in enumerate((1, 0), 1)]
        updates = [update async for update in transcript.reconcile(snapshot, records(recorded), session_id="acp", nesting=False)]
        first, second, first_result, second_result = [update["toolCallId"] for update in updates]
        self.assertNotEqual(first, second)
        self.assertEqual((first_result, second_result), (second, first))

    async def test_result_without_assistant_receipt_only_falls_back_for_unchanged_id(self):
        call = {"sessionUpdate": "tool_call", "toolCallId": "visible", "status": "in_progress"}
        result = {"sessionUpdate": "tool_call_update", "toolCallId": "visible", "status": "completed"}
        snapshot = transcript.Snapshot({"nativeSessionId": "native"}, [
            transcript.Row(transcript.position(row(1, "origin")), "assistant", [call]),
            transcript.Row(transcript.position(row(2, "result")), "toolResult", [result]),
        ])
        for original, linked in (("visible", True), ("other", False)):
            recorded = [transcript.recorded_result(original, None, {"nativeSessionId": "native", "entryId": "result"})]
            with self.subTest(original=original):
                updates = [update async for update in transcript.reconcile(snapshot, records(recorded), session_id="acp", nesting=False)]
                self.assertEqual(len(updates), 2 if linked else 3)
                self.assertEqual(updates[-1]["status"], "completed")
                self.assertEqual(updates[-1]["toolCallId"] == "visible", linked)

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
                replay = [update async for update in transcript.reconcile(snapshot, records(recorded), session_id="acp", nesting=True)]
                self.assertEqual(len(replay), 1)
                self.assertEqual("aelix/subagents" in replay[0]["_meta"], preserve)
