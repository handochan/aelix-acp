"""Bounded frame projection and fallback through an actual pinned native worker."""

import asyncio
import base64
import contextlib
import gc
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import struct
import sys
from tempfile import TemporaryDirectory
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import weakref
import zlib

from aelix_acp.runtime import host_bytes, native_frames, native_history, session_relay, store
from aelix_acp.runtime.store import append_event, cache_recovered_history, history, open_store
from aelix_acp.server import Server
from aelix_acp.session import Session
from aelix_acp.wire.errors import BackendError, ProtocolError
from support import AcpClient
from test_native_control_contention import ObservedClient, report

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
            {"type": "text", "metadata": {"data": "unchanged"}}
        ]}]
        raw = response(messages)
        messages[0]["content"][0].update(data="", source="")
        with patch.object(native_history, "HISTORY_LIMIT", 512):
            for size in (1, 13, 1024, 8192):
                with self.subTest(chunk=size):
                    self.assertEqual(await self.read_chunks(raw, split=size),
                                     [(None, None, (REQUEST_ID, messages))])

    async def test_discarded_image_strings_validate_split_utf8(self):
        for field in ("data", "source"):
            raw = response([{"role": "user", "content": [{"type": "image", field: "MARKER"}]}])
            for valid, sample in [(True, "é𐐷".encode()), (False, b"\xff"),
                                  (False, b"\xf0\x90"), (False, b"\xe2A\x82")]:
                with self.subTest(field=field, sample=sample):
                    frame, = await self.read_chunks(raw.replace(b"MARKER", sample), split=1)
                    messages = frame[2][1]
                    if valid:
                        self.assertEqual(messages[0]["content"][0][field], "")
                    else:
                        self.assertIsNone(messages)

    async def test_arguments_and_oversized_tool_results_do_not_consume_the_replay_budget(self):
        messages = [
            {"role": "user", "content": "before"},
            {"role": "assistant", "content": [{"type": "toolCall", "input":
                {"nested": [{"payload": "a" * 8192}, 123, True, None, [False, -12.5]]}, "tool_call_id": "id"},
                {"type": "toolCall", "arguments": "a" * 8192, "id": "id-two"}]},
            {"tool_call_id": "id", "content": [{"type": "text", "text": "b" * 8192}],
             "is_error": False, "role": "toolResult"},
            {"role": "assistant", "content": "after"},
        ]
        with patch.multiple(native_history, MAX_RECORD_BYTES=512, TOOL_CONTENT_BYTES=128, HISTORY_LIMIT=512):
            frame, = await self.read_chunks(response(messages), split=17)
        first, call, result, last = frame[2][1]
        self.assertEqual(first["content"], "before")
        self.assertEqual(last["content"], "after")
        self.assertEqual(call["content"][0],
                         {"type": "toolCall", "tool_call_id": "id", "input": None, "argumentsOmitted": True})
        self.assertEqual(call["content"][1],
                         {"type": "toolCall", "id": "id-two", "arguments": "", "argumentsOmitted": True})
        self.assertIs(result["content"], native_history.OMITTED_RESULT)
        malformed = response(messages).replace(b"123", b"12 3")
        invalid, = await self.read_chunks(malformed, split=13)
        self.assertIsNone(invalid[2][1])

    async def test_reader_drops_decoded_history_while_worker_remains_open(self):
        class WeakList(list):
            pass

        references = []
        initialize = native_history.HistoryResponse.__init__

        def tracked(parser, *args):
            initialize(parser, *args)
            parser.messages = WeakList()
            references.append(weakref.ref(parser.messages))

        reader = asyncio.StreamReader()
        complete = asyncio.get_running_loop().create_future()
        worker = SimpleNamespace(proc=SimpleNamespace(stdout=reader), private_host=True,
                                 history_requests={REQUEST_ID}, pending={REQUEST_ID: complete})
        reader.feed_data(response([{"role": "user", "content": "x" * 16384}]))
        next_frame = b'{"type":"response","command":"get_state","success":true}\n'
        reader.feed_data(next_frame)
        frames = Session.rpc_frames(worker)
        with patch.object(native_history.HistoryResponse, "__init__", tracked):
            request = asyncio.create_task(anext(frames))
            decoded = await complete
            self.assertIs(references[0](), decoded)
            worker.pending.clear()
            del decoded, complete
            self.assertEqual(await request, next_frame)
            request = asyncio.create_task(anext(frames))
            await asyncio.sleep(0)
            gc.collect()
            self.assertFalse(request.done())  # The reader is still waiting for more native output.
            self.assertIsNone(references[0]())
            reader.feed_eof()
            with self.assertRaises(StopAsyncIteration):
                await request

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
    async def test_protected_replay_preserves_pre_upgrade_native_cache_text(self):
        def assistant_update(text):
            return {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}

        recovered = assistant_update("Request failed: ordinary previously recovered assistant text")
        with TemporaryDirectory() as directory:
            server = Server(SimpleNamespace(state_dir=directory))
            server.send = AsyncMock()
            session = Session(server, "fixture", directory)
            session.private_host = True
            with contextlib.closing(server.db):
                with server.db:
                    server.db.executemany("INSERT INTO recovered_history VALUES(?, ?, ?)",
                                          [(session.id, -1, None), (session.id, 0, json.dumps(recovered))])
                append_event(server.db, session.id, assistant_update("Request failed: legacy failure diagnostic"))

                await server.replay_history(session)

        updates = [call.args[0]["params"]["update"] for call in server.send.await_args_list]
        self.assertEqual(updates, [recovered, assistant_update("Request failed.")])

    async def test_unrepresentable_tool_result_does_not_discard_neighboring_updates(self):
        with TemporaryDirectory() as directory:
            server = Server(SimpleNamespace(state_dir=directory))
            session = SimpleNamespace(id="fixture", read_native_history=AsyncMock(return_value=[
                {"role": "user", "content": "before"},
                {"role": "toolResult", "tool_call_id": "call", "content": [{"type": "text", "text": "x" * 256}]},
                {"role": "assistant", "content": "Request failed: ordinary recovered text"},
            ]))
            with contextlib.closing(server.db), patch.object(native_history, "TOOL_CONTENT_BYTES", 128):
                updates = await server.native_history(session)
                recorded = list(history(server.db, session.id))
                self.assertEqual(recorded[2]["_meta"], {store.ASSISTANT_ORIGIN_META: "model"})
                self.assertEqual([store.display_recorded_update(update, protected=True) for update in recorded], updates)
                self.assertEqual([updates[0]["content"]["text"], updates[2]["content"]["text"]],
                                 ["before", "Request failed: ordinary recovered text"])
                self.assertEqual(updates[1]["toolCallId"], "call")
                self.assertEqual(updates[1]["content"][0]["content"]["text"], native_history.TOOL_RESULT_OMITTED)

    async def test_underlying_history_errors_use_fixed_diagnostics(self):
        private_detail = "fixture/path SYNTHETIC_BODY iVBORw0KGgo="
        with TemporaryDirectory() as directory:
            server = Server(SimpleNamespace(state_dir=directory))
            native = SimpleNamespace(rpc=AsyncMock(side_effect=BackendError(private_detail, "get_messages")))
            session = SimpleNamespace(id="fixture", read_native_history=AsyncMock(return_value=[
                {"role": "user", "content": "before"},
            ]))
            with contextlib.closing(server.db), patch(
                "aelix_acp.server.cache_recovered_history", side_effect=sqlite3.OperationalError(private_detail)
            ):
                for operation in (Session.read_native_history(native), server.native_history(session)):
                    with self.assertRaises(ProtocolError) as failure:
                        await operation
                    self.assertEqual((failure.exception.code, str(failure.exception), failure.exception.data), (
                        -32603, "Native session history is unavailable", {"kind": "native_history_unavailable"},
                    ))
                    self.assertIsNone(failure.exception.__cause__)

    async def test_cache_is_atomic_and_recorded_cursor_excludes_later_updates(self):
        update = {"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": "recorded"}}
        with TemporaryDirectory() as directory, contextlib.closing(open_store(directory)) as db:
            with self.assertRaises(ValueError):
                await cache_recovered_history(directory, "fixture", [update] * 32 + [{"invalid": float("nan")}])
            self.assertEqual(db.execute("SELECT count(*) FROM recovered_history").fetchone(), (32,))
            self.assertEqual(list(history(db, "fixture")), [])
            await cache_recovered_history(directory, "fixture", [update, update])
            recorded = history(db, "fixture")
            self.assertEqual(next(recorded), update)
            append_event(db, "fixture", update)
            self.assertEqual(list(recorded), [update])
            self.assertEqual(list(history(db, "fixture")), [update, update, update])


    async def test_cache_and_live_event_wait_off_loop_during_external_write_lock(self):
        update = {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "recorded"}}
        with TemporaryDirectory() as directory, contextlib.closing(open_store(directory)) as db, (
            contextlib.closing(sqlite3.connect(Path(directory) / "sessions.sqlite3"))
        ) as blocker:
            entered = asyncio.Event()
            loop = asyncio.get_running_loop()
            transaction = store._recovery_transaction

            def observed(*args, **kwargs):
                loop.call_soon_threadsafe(entered.set)
                return transaction(*args, **kwargs)

            blocker.execute("BEGIN IMMEDIATE")
            with patch.object(store, "_recovery_transaction", observed):
                recovery = asyncio.create_task(cache_recovered_history(directory, "fixture", [update]))
                recording = store.LiveHistory(directory, "other")
                recording.append(update)
                live = asyncio.create_task(recording.flush())
                try:
                    await asyncio.wait_for(entered.wait(), 2)
                    self.assertFalse(recovery.done())
                    self.assertFalse(live.done())
                    self.assertEqual(list(history(db, "fixture")), [])
                finally:
                    blocker.rollback()
                    await asyncio.gather(recovery, live)
            self.assertEqual(list(history(db, "fixture")), [store.with_assistant_origin(update, "model")])
            self.assertEqual(list(history(db, "other")), [update])


    async def test_cancelled_cache_cannot_publish_after_the_writer_is_released(self):
        update = {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "staged"}}
        with TemporaryDirectory() as directory, contextlib.closing(open_store(directory)) as db, (
            contextlib.closing(sqlite3.connect(Path(directory) / "sessions.sqlite3"))
        ) as blocker:
            entered = asyncio.Event()
            loop = asyncio.get_running_loop()
            unhandled = []
            self.addCleanup(loop.set_exception_handler, loop.get_exception_handler())
            loop.set_exception_handler(lambda _, event: unhandled.append(event))
            transaction = store._recovery_transaction

            def observed(*args, **kwargs):
                loop.call_soon_threadsafe(entered.set)
                return transaction(*args, **kwargs)

            blocker.execute("BEGIN IMMEDIATE")
            with patch.object(store, "_recovery_transaction", observed):
                recovery = asyncio.create_task(cache_recovered_history(directory, "fixture", [update]))
                await asyncio.wait_for(entered.wait(), 2)
                recovery.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(recovery, 2)
            blocker.rollback()
            self.assertEqual(unhandled, [])
            self.assertEqual(list(history(db, "fixture")), [])
            await cache_recovered_history(directory, "fixture", [update])
            self.assertEqual(list(history(db, "fixture")), [store.with_assistant_origin(update, "model")])


def history_stop_adapter(mode):
    from aelix_acp import server as server_module

    released = Path("history-probe-release")
    rpc, frames = Session.rpc, Session.rpc_frames
    cache, write_busy = server_module.cache_recovered_history, store._write_busy
    history_command = re.compile(rb'"command"\s*:\s*"get_messages"')

    async def wait_for_release():
        async with asyncio.timeout(12):
            while not released.exists():
                await asyncio.sleep(0.01)

    async def observe_rpc(session, command, **kwargs):
        if command == "abort" or (command == "aelix_control" and kwargs.get("method") == "commands/cancel"):
            report("history_stop_submit")
        answer = await rpc(session, command, **kwargs)
        if command == "abort":
            report("history_abort_ack")
        return answer

    async def pause_history_stream(session):
        reader = session.proc.stdout
        read = reader.read
        prefix = b""

        async def pause_after_prefix(size):
            nonlocal prefix
            chunk = await read(size)
            if not released.exists():
                pieces = chunk.split(b"\n")
                for index, piece in enumerate(pieces):
                    prefix += piece[:256 - len(prefix)]
                    if index < len(pieces) - 1:
                        prefix = b""
                    elif history_command.search(prefix):
                        report("history_partial_line")
                        await wait_for_release()
            return chunk

        reader.read = pause_after_prefix
        async for frame in frames(session):
            yield frame

    async def pause_before_cache(*args):
        report("history_cache_ready")
        await wait_for_release()
        return await cache(*args)

    def observe_busy(error):
        busy = write_busy(error)
        if busy:
            report("history_cache_busy")
        return busy

    with contextlib.ExitStack() as patches:
        patches.enter_context(patch.object(Session, "rpc", observe_rpc))
        if mode == "stream":
            patches.enter_context(patch.object(Session, "rpc_frames", pause_history_stream))
        else:
            patches.enter_context(patch.object(server_module, "cache_recovered_history", pause_before_cache))
            patches.enter_context(patch.object(store, "_write_busy", observe_busy))
        server_module.main()


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
        return filename

    async def start_client(self, private=False, *, stop_probe=None):
        metadata = {
            session_relay.META_KEY: {"version": 1, "notifications": False}, host_bytes.META_KEY: {"version": 1}
        } if private else {}
        if stop_probe:
            client = await ObservedClient.start(self.root, EXECUTABLE, capabilities={"_meta": metadata},
                entry=(str(Path(__file__).resolve()), "--history-stop-probe", stop_probe))
        else:
            client = await AcpClient.start(self.root, EXECUTABLE, capabilities={"_meta": metadata})
        self.addAsyncCleanup(client.close)
        return client

    async def open(self, private=False):
        client = await self.start_client(private)
        await client.request("session/load", self.params, timeout=120)
        return client

    @staticmethod
    def replay(client):
        for frame in client.updates:
            if frame.get("method") == "session/update":
                update = frame["params"]["update"]
                if update["sessionUpdate"] in ("user_message_chunk", "agent_message_chunk", "tool_call", "tool_call_update"):
                    yield update

    async def test_plain_client_replays_text_on_both_sides_of_16_mib(self):
        for size in (15, 17):
            with self.subTest(size=size):
                block = "한글 𐐷 " + "x" * (size * MIB)
                expected = hashlib.sha256(block.encode())
                self.seed([{"role": "user", "content": [{"type": "text", "text": block}]}])
                client = await self.open()
                actual = hashlib.sha256()
                for update in self.replay(client):
                    self.assertEqual(update["sessionUpdate"], "user_message_chunk")
                    actual.update(update["content"]["text"].encode())
                self.assertEqual(actual.digest(), expected.digest())
                await client.close()
                with contextlib.closing(open_store(str(self.root / "state"))) as db, db:
                    db.execute("DELETE FROM recovered_history WHERE session_id=?", (self.session,))

    async def start_stop_reopen(self, text, mode):
        self.seed([{"role": "user", "content": [{"type": "text", "text": text}]}])
        client = await self.start_client(private=True, stop_probe=mode)
        loading = asyncio.create_task(client.request("session/load", self.params))
        release = self.root / "history-probe-release"

        async def settle_load():
            release.touch()
            await asyncio.gather(loading, return_exceptions=True)

        self.addAsyncCleanup(settle_load)
        return client, loading, release

    async def test_stop_reaches_native_while_its_history_line_is_streaming(self):
        text = "streamed history " + "x" * MIB
        client, loading, release = await self.start_stop_reopen(text, "stream")
        await client.observe("history_partial_line")
        with self.assertRaisesRegex(RuntimeError, "Unknown session"):
            await client.request("_aelix/capabilities", {"sessionId": self.session})
        stopping = asyncio.create_task(client.request("session/cancel", {"sessionId": self.session}, timeout=8))
        try:
            await client.observe("history_stop_submit")
            self.assertFalse(loading.done())
            self.assertFalse(stopping.done())  # The ACK is behind the rest of the line on native stdout.
        finally:
            release.touch()
            self.assertEqual(await stopping, {})
        await client.observe("history_abort_ack")
        await loading
        self.assertEqual("".join(update["content"]["text"] for update in self.replay(client)), text)

    async def test_stop_reaches_native_while_fallback_cache_waits_on_sqlite(self):
        text = "history waiting for the cache"
        client, loading, release = await self.start_stop_reopen(text, "cache")
        await client.observe("history_cache_ready")
        with contextlib.closing(sqlite3.connect(self.root / "state/sessions.sqlite3", timeout=0)) as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            try:
                release.touch()
                await client.observe("history_cache_busy")
                with self.assertRaisesRegex(RuntimeError, "Unknown session"):
                    await client.request("_aelix/capabilities", {"sessionId": self.session})
                self.assertEqual(await client.request("session/cancel", {"sessionId": self.session}, timeout=8), {})
                await client.observe("history_abort_ack")
                self.assertFalse(loading.done())
            finally:
                blocker.rollback()
        await loading
        self.assertEqual([update["content"]["text"] for update in self.replay(client)], [text])

    async def test_native_tool_limits_keep_surrounding_history_and_tool_identity(self):
        self.seed([
            {"role": "user", "content": [{"type": "text", "text": "before"}]},
            {"role": "assistant", "content": [{"type": "toolCall", "tool_call_id": "big-call", "tool_name": "read",
                                               "input": {"buffer": "ARG_SENTINEL" + "A" * (4 * MIB)}}]},
            {"role": "toolResult", "tool_call_id": "big-call", "tool_name": "read", "is_error": True,
             "content": [{"type": "text", "text": "RESULT_SENTINEL" + "R" * MIB}]},
            {"role": "assistant", "content": [
                {"type": "toolCall", "tool_call_id": "signed-call", "tool_name": "read", "input": {},
                 "thought_signature": "SIGNATURE_SENTINEL" + "S" * (4 * MIB)},
            ]},
            {"role": "toolResult", "tool_call_id": "signed-call", "tool_name": "read", "is_error": False,
             "content": [{"type": "text", "text": "signed result", "text_signature": "S" * (4 * MIB + 1)}]},
            {"role": "assistant", "content": [{"type": "text", "text": "after"}]},
        ])
        client = await self.open(private=True)
        first, call, result, signed_call, signed_result, last = list(self.replay(client))
        self.assertEqual([first["content"]["text"], last["content"]["text"]], ["before", "after"])
        self.assertEqual((call["toolCallId"], result["toolCallId"], result["status"]), ("big-call", "big-call", "failed"))
        self.assertEqual(call["content"][0]["content"]["text"], native_history.TOOL_ARGUMENTS_OMITTED)
        self.assertEqual(result["content"][0]["content"]["text"], native_history.TOOL_RESULT_OMITTED)
        self.assertNotIn("ARG_SENTINEL", json.dumps(call))
        self.assertNotIn("RESULT_SENTINEL", json.dumps(result))
        self.assertEqual(signed_call["toolCallId"], "signed-call")
        self.assertEqual(signed_result["content"][0]["content"]["text"], "signed result")
        self.assertNotIn("SIGNATURE_SENTINEL", json.dumps(signed_call))

    def seed_truncated(self):
        filename = self.seed([{"role": "user", "content": [{"type": "text", "text": "survived"}]}])
        with filename.open("ab") as stream:
            stream.write(b'{"type":"TAIL_BODY_SENTINEL')
        return filename

    async def test_generic_reopen_relays_the_native_recovery_warning(self):
        filename = self.seed_truncated()
        client = await self.open()
        self.assertEqual([item["content"]["text"] for item in self.replay(client)], ["survived"])
        async with asyncio.timeout(5):
            while os.fsencode(filename) not in client.stderr:
                await asyncio.sleep(0.01)
        await client.close()
        self.assertIn(b"Recovered damaged session file", client.stderr)
        self.assertNotIn(b"TAIL_BODY_SENTINEL", client.stderr)

    async def test_studio_host_reopen_does_not_expose_the_native_recovery_warning(self):
        filename = self.seed_truncated()
        client = await self.open(private=True)
        self.assertEqual([item["content"]["text"] for item in self.replay(client)], ["survived"])
        await client.close()
        self.assertNotIn(b"Recovered damaged session file", client.stderr)
        self.assertNotIn(os.fsencode(filename), client.stderr)
        self.assertNotIn(b"TAIL_BODY_SENTINEL", client.stderr)

    async def test_native_text_below_the_retained_history_budget(self):
        block = "x" * MIB
        self.seed({"role": "user", "content": [{"type": "text", "text": block}]} for _ in range(63))
        client = await self.open(private=True)
        self.assertEqual(sum(len(item["content"]["text"]) for item in self.replay(client)), 63 * MIB)

    async def test_native_over_budget_error_is_fixed_and_recorded_events_bypass_it(self):
        filename = self.seed(
            {"role": "user", "content": [{"type": "text", "text": "BODY_SENTINEL" + "x" * MIB}]}
            for _ in range(65)
        )
        client = await self.start_client(private=True)
        with self.assertRaises(RuntimeError) as failure:
            await client.request("session/load", self.params, timeout=120)
        self.assertEqual(json.loads(str(failure.exception)), {
            "code": -32603, "message": "Native session history is unavailable",
            "data": {"kind": "native_history_unavailable"},
        })
        self.assertEqual(list(self.replay(client)), [])
        await client.close()
        self.assertNotIn(os.fsencode(filename), client.stderr)
        self.assertNotIn(b"BODY_SENTINEL", client.stderr)
        with contextlib.closing(open_store(str(self.root / "state"))) as db:
            append_event(db, self.session, {"sessionUpdate": "user_message_chunk",
                                            "content": {"type": "text", "text": "already recorded"}})
        recorded = await self.open(private=True)
        self.assertEqual([item["content"]["text"] for item in self.replay(recorded)], ["already recorded"])
        with contextlib.closing(open_store(str(self.root / "state"))) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM recovered_history").fetchone(), (0,))

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


if __name__ == "__main__":
    if sys.argv[1:2] == ["--history-stop-probe"]:
        _, mode = sys.argv[1:3]
        del sys.argv[1:3]
        history_stop_adapter(mode)
    else:
        unittest.main()
