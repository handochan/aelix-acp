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
HOLD_SECONDS = float(os.environ.get("AELIX_ACP_CONTENTION_SECONDS", "0.3"))
SOURCE = os.environ.get("AELIX_ACP_CONTENTION_SOURCE")
METRIC = b"control-contention "
TOOL_ID = "call_control_contention"
DELTA = "Fixture delta under contention."


def report(label):
    import sys

    print(METRIC.decode() + json.dumps([label, perf_counter_ns()]), file=sys.stderr, flush=True)


def adapter():
    from unittest.mock import patch

    from aelix_acp.server import Server, main
    from aelix_acp.session import Session

    native_event = Session.event
    native_rpc = Session.rpc
    handle = Server.handle
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

    async def observe_handle(server, message):
        if message.get("method") == "session/cancel":
            report("stop_received")
        return await handle(server, message)

    async def observe_request(server, method, *args, **kwargs):
        answer = await request(server, method, *args, **kwargs)
        if method == "session/request_permission":
            report("allow_received")
        return answer

    with contextlib.ExitStack() as patches:
        patches.enter_context(patch.object(Session, "event", observe_event))
        patches.enter_context(patch.object(Session, "rpc", observe_rpc))
        patches.enter_context(patch.object(Server, "handle", observe_handle))
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
        async with asyncio.timeout(15), self.metric_arrived:
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
        }, timeout=25))

    @contextlib.asynccontextmanager
    async def writer_lock(self):
        with contextlib.closing(sqlite3.connect(self.root / "state/sessions.sqlite3", timeout=0)) as db:
            db.execute("BEGIN IMMEDIATE")
            release = {}
            hold = asyncio.Event()

            async def unlock():
                await hold.wait()
                await asyncio.sleep(HOLD_SECONDS)
                release["file"] = self.proof.exists()
                db.rollback()
                release["time"] = perf_counter_ns()

            timer = asyncio.create_task(unlock())
            try:
                yield hold, release
            finally:
                hold.set()
                await timer

    @staticmethod
    def measurements(case, start, inputs, native, release, **extra):
        if os.environ.get("AELIX_ACP_CONTENTION_REPORT") != "1":
            return

        def elapsed(stamp):
            return round((stamp - start) / 1e6, 2)

        print(json.dumps({"case": case, "acp_received_ms": elapsed(inputs), "native_done_ms": elapsed(native),
                          "lock_released_ms": elapsed(release["time"]), **extra}))

    async def test_allow_once_reaches_native_with_an_external_writer(self):
        asked = asyncio.Event()
        allow = asyncio.Event()

        async def answer(_):
            asked.set()
            await allow.wait()
            return "allow-once"

        self.client.permission = answer
        prompt = self.prompt("WRITE_FIXTURE")
        await asyncio.wait_for(asked.wait(), 15)
        self.assertFalse(self.proof.exists())
        async with self.writer_lock() as (hold, release):
            start = perf_counter_ns()
            hold.set()
            allow.set()
            received = await self.client.observe("allow_received")
            completed = await self.client.observe("native_tool_end")
        self.assertEqual((await prompt)["stopReason"], "end_turn")
        self.assertEqual(self.proof.read_text(encoding="utf-8"), "approved")
        self.measurements("allow-once", start, received, completed, release, file_before_release=release["file"])

    async def test_stop_reaches_running_native_with_an_external_writer(self):
        prompt = self.prompt("STOP_FIXTURE")
        await asyncio.wait_for(self.model_started.wait(), 15)
        async with self.writer_lock() as (hold, release):
            self.send_delta.set()
            await self.client.observe("native_delta")
            self.assertFalse(prompt.done())
            start = perf_counter_ns()
            hold.set()
            await self.client.request("session/cancel", {"sessionId": self.session})
            client_done = perf_counter_ns()
            received = await self.client.observe("stop_received")
            aborted = await self.client.observe("native_abort_ack")
        self.assertFalse(self.finish_stream.is_set())
        self.assertEqual((await prompt)["stopReason"], "cancelled")
        self.measurements("running-stop", start, received, aborted, release,
                          acp_stop_reply_ms=round((client_done - start) / 1e6, 2))


if __name__ == "__main__":
    adapter()
