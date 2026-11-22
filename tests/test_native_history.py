"""Bounded frame projection and fallback through an actual pinned native worker."""

import base64
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import struct
from tempfile import TemporaryDirectory
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import zlib

from aelix_acp.runtime import host_bytes, native_frames, native_history, session_relay
from aelix_acp.runtime.store import append_event, cache_recovered_history, history, open_store
from aelix_acp.server import Server
from aelix_acp.wire.errors import ProtocolError
from support import AcpClient

MIB = 1024**2
REQUEST_ID = "0" * 32
EXECUTABLE = os.environ.get("AELIX_ACP_TEST_AELIX")


def response(messages):
    return json.dumps({"type": "response", "command": "get_messages", "success": True,
                       "id": REQUEST_ID, "data": {"messages": messages}}).encode() + b"\n"


class NativeHistoryFrames(unittest.IsolatedAsyncioTestCase):
    async def read_chunks(self, raw, *, split=13):
        class Reader:
            async def read(self, limit):
                return next(parts, b"")

        parts = (raw[offset:offset + split] for offset in range(0, len(raw), split))
        return [frame async for frame in native_frames.read_frames(
            Reader(), private=True, wants_history=lambda identifier: identifier == REQUEST_ID
        )]

    async def test_image_strings_are_discarded_across_every_boundary(self):
        messages = [{"role": "user", "content": [
            {"data": "a" * 2048, "source": 'data:image/png;base64,\\escaped"𐐷', "type": "image"},
            {"type": "text", "text": 'a\\a"\n한글\0'},
            {"type": "toolCall", "input": {"data": "unchanged"}}
        ]}]
        raw = response(messages)
        messages[0]["content"][0].update(data="", source="")
        with patch.object(native_history, "HISTORY_LIMIT", 512):
            for size in (1, 13, 1024, 8192):
                with self.subTest(chunk=size):
                    self.assertEqual(await self.read_chunks(raw, split=size),
                                     [(None, None, (REQUEST_ID, messages))])

    async def test_streamed_text_keeps_utf8_and_escape_pairs_intact(self):
        text = 'begin 𐐷 한글 \n\" \\ end' * 200
        messages = [{"role": "user", "content": text},
                    {"role": "assistant", "content": [{"type": "text", "text": text}]}]
        wire = response(messages)
        utf8_wire = (json.dumps(json.loads(wire), ensure_ascii=False) + "\n").encode()
        with patch.object(native_history, "TEXT_CHUNK", 31):
            for raw in (wire, utf8_wire):
                for split in (7, 1024):
                    frame, = await self.read_chunks(raw, split=split)
                    _, projected = frame[2]
                    self.assertEqual("".join(projected[0]["content"]), text)
                    self.assertEqual("".join(projected[1]["content"][0]["text"]), text)

    async def test_malformed_and_over_budget_history_are_drained_before_next_reply(self):
        next_frame = b'{"type":"response","command":"abort","success":true}\n'
        full = response([{"role": "user", "content": "sensitive-" + "x" * 256}])
        broken_image = response([{"role": "user", "content": [{"type": "image", "data": "bad"}]}])
        broken_image = broken_image.replace(b'"bad"', b'"\\q"')
        broken_array = response([{"role": "user", "content": "private"}]).replace(b']}}', b',]}}')
        native_error = response([]).replace(b'true', b'false').replace(b'"messages": []', b'"error": "private"')
        with patch.object(native_history, "HISTORY_LIMIT", 128):
            for raw in (full, broken_image, broken_array, native_error):
                self.assertEqual(await self.read_chunks(raw + next_frame),
                                 [(None, None, (REQUEST_ID, None)), (next_frame, None, None)])
        error = native_history.unavailable()
        self.assertEqual((error.code, str(error), error.data),
                         (-32603, "Native session history is unavailable", {"kind": "native_history_unavailable"}))


class HistoryReplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_unrepresentable_tool_result_returns_fixed_error_without_partial_cache(self):
        with TemporaryDirectory() as directory:
            server = Server(SimpleNamespace(state_dir=directory))
            session = SimpleNamespace(id="fixture", read_native_history=AsyncMock(return_value=[
                {"role": "user", "content": "before"},
                {"role": "toolResult", "tool_call_id": "call", "content": [{"type": "text", "text": "x" * 256}]},
            ]))
            with contextlib.closing(server.db), patch.object(native_history, "TOOL_CONTENT_BYTES", 128):
                with self.assertRaises(ProtocolError) as error:
                    await server.native_history(session)
                self.assertEqual(str(error.exception), "Native session history is unavailable")
                self.assertEqual(list(history(server.db, session.id)), [])

    async def test_cache_is_atomic_and_recorded_cursor_excludes_later_updates(self):
        update = {"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": "recorded"}}
        with TemporaryDirectory() as directory, contextlib.closing(open_store(directory)) as db:
            with self.assertRaises(ValueError):
                await cache_recovered_history(db, "fixture", [update] * 32 + [{"invalid": float("nan")}])
            self.assertEqual(db.execute("SELECT count(*) FROM recovered_history").fetchone(), (32,))
            self.assertEqual(list(history(db, "fixture")), [])
            await cache_recovered_history(db, "fixture", [update, update])
            recorded = history(db, "fixture")
            self.assertEqual(next(recorded), update)
            append_event(db, "fixture", update)
            self.assertEqual(list(recorded), [update])
            self.assertEqual(list(history(db, "fixture")), [update, update, update])


@unittest.skipUnless(EXECUTABLE, "Set AELIX_ACP_TEST_AELIX to the pinned native executable")
class NativeHistoryIntegration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = TemporaryDirectory(prefix="acp-history-", dir="/tmp" if os.name != "nt" else None)
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.session = "recovered"
        self.params = {"sessionId": self.session, "cwd": str(self.root), "mcpServers": []}
        (self.root / "home").mkdir()
        (self.root / "agent").mkdir()
        (self.root / "agent/settings.json").write_text(
            json.dumps({"defaultProvider": "fixture", "defaultModel": "one", "compaction": {"enabled": False}}),
            encoding="utf-8",
        )
        model = {"id": "one", "name": "One", "input": ["text", "image"]}
        (self.root / "agent/models.json").write_text(json.dumps({"providers": {"fixture": {
            "baseUrl": "http://127.0.0.1:1/v1", "api": "openai-completions", "apiKey": "fixture", "models": [model]
        }}}), encoding="utf-8")

    def seed(self, messages):
        filename = self.root / "fixture.jsonl"
        timestamp = "2026-01-01T00:00:00.000Z"
        with filename.open("w", encoding="utf-8") as output:
            header = {"type": "session", "version": 3, "id": "fixture", "timestamp": timestamp, "cwd": str(self.root)}
            output.write(json.dumps(header) + "\n")
            parent = None
            for index, message in enumerate(messages):
                entry_id = f"{index:08x}"
                entry = {"type": "message", "id": entry_id, "parentId": parent, "timestamp": timestamp,
                         "message": {**message, "timestamp": index + 1}}
                output.write(json.dumps(entry) + "\n")
                parent = entry_id
        with contextlib.closing(open_store(str(self.root / "state"))) as db, db:
            db.execute("INSERT OR REPLACE INTO sessions VALUES(?,?,?,?,?,?)",
                       (self.session, str(self.root), "coding", str(filename), "fixture", 1))

    async def open(self, private=False):
        metadata = {
            session_relay.META_KEY: {"version": 1, "notifications": False}, host_bytes.META_KEY: {"version": 1}
        } if private else {}
        client = await AcpClient.start(self.root, EXECUTABLE, capabilities={"_meta": metadata})
        self.addAsyncCleanup(client.close)
        await client.request("session/load", self.params, timeout=120)
        return client

    @staticmethod
    def replay(client):
        for frame in client.updates:
            if frame.get("method") == "session/update":
                update = frame["params"]["update"]
                if update["sessionUpdate"] in ("user_message_chunk", "agent_message_chunk", "tool_call", "tool_call_update"):
                    yield update

    async def test_plain_client_replays_more_than_16_mib_of_text(self):
        block = "한글 𐐷 " + "x" * (17 * MIB)
        expected = hashlib.sha256(block.encode())
        self.seed([{"role": "user", "content": [{"type": "text", "text": block}]}])
        client = await self.open()
        actual = hashlib.sha256()
        for update in self.replay(client):
            self.assertEqual(update["sessionUpdate"], "user_message_chunk")
            actual.update(update["content"]["text"].encode())
        self.assertEqual(actual.digest(), expected.digest())

    async def test_private_host_replays_over_64_mib_of_image_history_and_prefers_recorded(self):
        def chunk(kind, payload):
            return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))

        width, height = 512, 512
        png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
               + chunk(b"IDAT", zlib.compress(bytes((1 + width * 3) * height), level=0)) + chunk(b"IEND", b""))
        image = {"type": "image", "mime_type": "image/png", "data": base64.b64encode(png).decode()}
        self.assertGreater(65 * len(image["data"]), 64 * MIB)
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "before"}, *([image] * 5),
                                          {"type": "text", "text": "after"}]},
            {"role": "assistant", "content": [
                {"type": "text", "text": "calling"},
                {"type": "toolCall", "tool_call_id": "call-1", "tool_name": "read", "input": {"path": "input"}},
            ]},
            {"role": "toolResult", "tool_call_id": "call-1", "tool_name": "read", "is_error": True,
             "content": [{"type": "text", "text": "result"}, image]},
            *({"role": "user", "content": [image] * 5} for _ in range(12)),
            {"role": "assistant", "content": [{"type": "text", "text": "end"}]},
        ]
        self.seed(messages)
        client = await self.open(private=True)
        rendered = list(self.replay(client))
        placeholder = native_history.IMAGE_OMITTED
        self.assertEqual([row["content"]["text"] for row in rendered[:8]],
                         ["before", *([placeholder] * 5), "after", "calling"])
        start, end = rendered[8:10]
        self.assertEqual((start["sessionUpdate"], start["toolCallId"], start["title"]), ("tool_call", "call-1", "read"))
        self.assertEqual((end["sessionUpdate"], end["toolCallId"], end["status"]),
                         ("tool_call_update", "call-1", "failed"))
        self.assertEqual([row["content"]["text"] for row in end["content"]], ["result", placeholder])
        self.assertEqual([row["content"]["text"] for row in rendered[10:]], [placeholder] * 60 + ["end"])
        with contextlib.closing(sqlite3.connect(self.root / "state/sessions.sqlite3")) as db:
            stored = [value for (value,) in db.execute(
                "SELECT value FROM recovered_history WHERE session_id=? AND position>=0", (self.session,))]
        self.assertEqual(len(stored), len(rendered))
        self.assertTrue(all(len(value) < 1024 for value in stored))
        self.assertNotIn(image["data"][:100], client.stderr.decode(errors="replace"))

        await client.close()
        self.seed([{"role": "user", "content": [{"type": "text", "text": "different native history"}]}])
        reopened = await self.open(private=True)
        self.assertEqual(list(self.replay(reopened)), rendered)
