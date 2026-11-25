"""Probe actual ACP stdio, native approval, and running Stop with SQLite contention."""

import asyncio
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from time import perf_counter_ns
import unittest

from support import AcpClient

EXECUTABLE = os.environ.get("AELIX_ACP_TEST_AELIX")
HOLD_SECONDS = os.environ.get("AELIX_ACP_CONTENTION_SECONDS")
SOURCE = os.environ.get("AELIX_ACP_CONTENTION_SOURCE")
METRIC = b"control-contention "
TOOL_ID = "call_control_contention"
DELTA = "Fixture delta under contention."
REQUEST_ID = "control-contention-turn"


def report(label):
    import sys

    print(METRIC.decode() + json.dumps([label, perf_counter_ns()]), file=sys.stderr, flush=True)


def adapter():
    from unittest.mock import patch

    from aelix_acp.server import Server, main
    from aelix_acp.session import Session

    native_event = Session.event
    native_rpc = Session.rpc
    admit_cancel = Server.admit_cancel
    request = Server.client_request

    async def observe_event(session, event):
        kind = event.get("type")
        if kind == "tool_execution_end" and (event.get("toolCallId") or event.get("tool_call_id")) == TOOL_ID:
            report("native_tool_end")
        if kind == "message_update":
            update = event.get("assistantMessageEvent") or event.get("assistant_message_event") or {}
            if update.get("type") == "text_delta" and update.get("delta") == DELTA:
                report("native_delta")
        return await native_event(session, event)

    async def observe_rpc(session, command, **kwargs):
        answer = await native_rpc(session, command, **kwargs)
        if command == "abort":
            report("native_abort_ack")
        return answer

    def observe_cancel(server, request_id, params):
        report("stop_received")
        return admit_cancel(server, request_id, params)

    async def observe_request(server, method, *args, **kwargs):
        answer = await request(server, method, *args, **kwargs)
        if method == "session/request_permission":
            report("allow_received")
        return answer

    with contextlib.ExitStack() as patches:
        patches.enter_context(patch.object(Session, "event", observe_event))
        patches.enter_context(patch.object(Session, "rpc", observe_rpc))
        patches.enter_context(patch.object(Server, "admit_cancel", observe_cancel))
        patches.enter_context(patch.object(Server, "client_request", observe_request))
        main()


class ObservedClient(AcpClient):
    def __init__(self, process):
        super().__init__(process)
        self.metrics = {}
        self.metric_arrived = asyncio.Condition()

    async def read_errors(self):
        while line := await self.process.stderr.readline():
            self.stderr.extend(line)
            if line.startswith(METRIC):
                key, time = json.loads(line[len(METRIC):])
                async with self.metric_arrived:
                    self.metrics.setdefault(key, time)
                    self.metric_arrived.notify_all()

    async def observe(self, key):
        async with asyncio.timeout(8), self.metric_arrived:
            await self.metric_arrived.wait_for(lambda: key in self.metrics)
            return self.metrics[key]


@unittest.skipUnless(EXECUTABLE, "Set AELIX_ACP_TEST_AELIX to the pinned native executable")
class NativeControlContention(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory(prefix="acp-control-", dir="/tmp" if os.name != "nt" else None)
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        (self.root / "home").mkdir()
        agent = self.root / "agent"
        agent.mkdir()
        self.proof = self.root / "approved.txt"
        self.model_started = asyncio.Event()
        self.send_delta = asyncio.Event()
        self.finish_stream = asyncio.Event()
        self.listener = await asyncio.start_server(self.model, "127.0.0.1", 0)
        port = self.listener.sockets[0].getsockname()[1]
        (agent / "settings.json").write_text(json.dumps({
            "defaultProvider": "fixture", "defaultModel": "one", "compaction": {"enabled": False},
        }), encoding="utf-8")
        (agent / "models.json").write_text(json.dumps({"providers": {"fixture": {
            "baseUrl": f"http://127.0.0.1:{port}/v1", "api": "openai-completions", "apiKey": "fixture",
            "models": [{"id": "one", "name": "Fixture", "input": ["text"]}],
        }}}), encoding="utf-8")
        env = {"PYTHONPATH": str(Path(SOURCE).resolve())} if SOURCE else None
        self.client = await ObservedClient.start(self.root, EXECUTABLE, entry=(str(Path(__file__).resolve()),),
            extra_env=env, capabilities={"_meta": {
                "aelix/sessionRelay": {"version": 1, "notifications": False}, "aelix/hostBytes": {"version": 1},
            }})
        self.addAsyncCleanup(self.client.close)
        created = await self.client.request("session/new", {"cwd": str(self.root), "mcpServers": []})
        self.session = created["sessionId"]

    async def asyncTearDown(self):
        self.send_delta.set()
        self.finish_stream.set()
        self.listener.close()
        await self.listener.wait_closed()

    async def model(self, reader, writer):
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            size = next(int(line.split(b":", 1)[1]) for line in headers.lower().splitlines()
                        if line.startswith(b"content-length:"))
            messages = json.loads(await reader.readexactly(size))["messages"]
            last = messages[-1]
            stop_test = last["role"] == "user" and "STOP_FIXTURE" in str(last.get("content"))
            write_test = last["role"] == "user" and "WRITE_FIXTURE" in str(last.get("content"))
            delta = {"role": "assistant", "content": DELTA if stop_test else "Fixture reply."}
            finish = "stop"
            if write_test:
                delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": TOOL_ID,
                    "type": "function", "function": {"name": "write", "arguments": json.dumps({
                        "path": str(self.proof), "content": "approved",
                    })}}]}
                finish = "tool_calls"

            def frame(delta, reason):
                return ("data: " + json.dumps({
                    "id": "fixture", "object": "chat.completion.chunk", "created": 0, "model": "one",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": reason}],
                }) + "\n\n").encode()

            first = frame(delta, None)
            rest = frame({}, finish) + b"data: [DONE]\n\n"
            headers = (f"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                       f"Content-Length: {len(first) + len(rest)}\r\nConnection: close\r\n\r\n").encode()
            writer.write(headers)
            await writer.drain()
            if stop_test:
                self.model_started.set()
                await self.send_delta.wait()
            writer.write(first)
            await writer.drain()
            if stop_test:
                await self.finish_stream.wait()
            writer.write(rest)
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    def prompt(self, text):
        return asyncio.create_task(self.client.request("session/prompt", {
            "sessionId": self.session, "prompt": [{"type": "text", "text": text}],
            "_meta": {"aelix/requestId": REQUEST_ID},
        }, timeout=25))

    @contextlib.asynccontextmanager
    async def writer_lock(self):
        with contextlib.closing(sqlite3.connect(self.root / "state/sessions.sqlite3", timeout=0)) as db:
            async with asyncio.timeout(8):
                while True:
                    try:
                        db.execute("BEGIN IMMEDIATE")
                        break
                    except sqlite3.OperationalError as error:
                        if error.sqlite_errorcode != sqlite3.SQLITE_BUSY:
                            raise
                        await asyncio.sleep(0.01)
            release = {}
            hold = asyncio.Event()

            def unlock():
                release["file"] = self.proof.exists()
                db.rollback()
                release["time"] = perf_counter_ns()

            async def timed_unlock():
                await hold.wait()
                await asyncio.sleep(float(HOLD_SECONDS))
                unlock()

            timer = asyncio.create_task(timed_unlock()) if HOLD_SECONDS else None
            try:
                yield hold, release, db
            finally:
                if timer is None:
                    unlock()
                else:
                    hold.set()
                    await timer

    def receipt_state(self, db):
        return db.execute("SELECT state FROM turns WHERE session_id=? AND request_key=?",
                          (self.session, REQUEST_ID)).fetchone()

    async def check_reopen(self, text, result):
        await self.client.request("session/close", {"sessionId": self.session})
        self.client.updates.clear()
        await self.client.request("session/load", {"sessionId": self.session, "cwd": str(self.root), "mcpServers": []})
        updates = [e["params"]["update"] for e in self.client.updates if e.get("method") == "session/update"]
        self.assertIn(text, [u["content"]["text"] for u in updates if u["sessionUpdate"] == "user_message_chunk"])
        count = len(self.client.requests)
        self.assertEqual(await self.prompt(text), result)
        self.assertEqual(len(self.client.requests), count)
        return updates

    @staticmethod
    def measurements(case, start, inputs, native, release, **extra):
        if os.environ.get("AELIX_ACP_CONTENTION_REPORT") != "1":
            return

        def elapsed(stamp):
            return round((stamp - start) / 1e6, 2)

        print(json.dumps({"case": case, "acp_received_ms": elapsed(inputs), "native_done_ms": elapsed(native),
                          "lock_released_ms": elapsed(release["time"]), **extra}))

    async def check_permission(self, option, *, writes):
        asked = asyncio.Event()
        reply = asyncio.Event()

        async def answer(_):
            asked.set()
            await reply.wait()
            return option

        self.client.permission = answer
        prompt = self.prompt("WRITE_FIXTURE")
        await asyncio.wait_for(asked.wait(), 15)
        self.assertFalse(self.proof.exists())
        async with self.writer_lock() as (hold, release, db):
            start = perf_counter_ns()
            hold.set()
            reply.set()
            received = await self.client.observe("allow_received")
            completed = await self.client.observe("native_tool_end")
            if not HOLD_SECONDS:
                self.assertEqual(self.receipt_state(db), ("running",))
                self.assertEqual(self.proof.exists(), writes)
        result = await prompt
        self.assertEqual(result["stopReason"], "end_turn")
        self.assertEqual(self.proof.exists(), writes)
        if writes:
            self.assertEqual(self.proof.read_text(encoding="utf-8"), "approved")
        self.measurements(option, start, received, completed, release, file_before_release=release["file"])
        if not HOLD_SECONDS:
            updates = await self.check_reopen("WRITE_FIXTURE", result)
            statuses = [u["status"] for u in updates if u.get("toolCallId") == TOOL_ID]
            self.assertEqual(statuses, ["pending", "pending", "in_progress", "completed"] if writes else
                             ["pending", "pending", "failed"])

    async def test_allow_once_reaches_native_with_an_external_writer(self):
        await self.check_permission("allow-once", writes=True)

    async def test_reject_reaches_native_with_an_external_writer(self):
        await self.check_permission("reject-once", writes=False)

    async def test_stop_reaches_running_native_with_an_external_writer(self):
        prompt = self.prompt("STOP_FIXTURE")
        await asyncio.wait_for(self.model_started.wait(), 15)
        async with self.writer_lock() as (hold, release, db):
            self.send_delta.set()
            await self.client.observe("native_delta")
            self.assertFalse(prompt.done())
            start = perf_counter_ns()
            hold.set()
            await self.client.request("session/cancel", {"sessionId": self.session}, timeout=8)
            client_done = perf_counter_ns()
            received = await self.client.observe("stop_received")
            aborted = await self.client.observe("native_abort_ack")
            if not HOLD_SECONDS:
                self.assertEqual(self.receipt_state(db), ("running",))
        self.assertFalse(self.finish_stream.is_set())
        result = await prompt
        self.assertEqual(result["stopReason"], "cancelled")
        self.measurements("running-stop", start, received, aborted, release,
                          acp_stop_reply_ms=round((client_done - start) / 1e6, 2))
        if not HOLD_SECONDS:
            updates = await self.check_reopen("STOP_FIXTURE", result)
            self.assertIn(DELTA, [u["content"]["text"] for u in updates if u["sessionUpdate"] == "agent_message_chunk"])


if __name__ == "__main__":
    adapter()
