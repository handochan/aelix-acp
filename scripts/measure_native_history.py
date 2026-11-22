"""Measure pinned-native history recovery through ACP stdio on macOS or Linux."""

import argparse
import asyncio
import base64
import contextlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
from time import perf_counter_ns
from types import SimpleNamespace
from unittest.mock import patch

from spike_image_window import make_image, sse

MIB = 1024**2
HISTORY_ID = "history"
TOOL_ID = "history-permission"
METRIC = "acp-history-metric "
COMMAND = re.compile(rb'"command"\s*:\s*"get_messages"')


def now():
    return perf_counter_ns()


def ms(end, start):
    return round((end - start) / 1_000_000, 3)


def emit(label, **values):
    print(METRIC + json.dumps({"label": label, "time": now(), **values}), file=sys.stderr, flush=True)


async def adapter(aelix, root, target_bytes, contended):
    import resource

    from aelix_acp import server as server_module
    from aelix_acp.runtime import store
    from aelix_acp.session import Session

    lag_ns = 0
    in_load = False

    class InstrumentedServer(server_module.Server):
        async def handle(self, message):
            nonlocal in_load
            method = message.get("method")
            if method == "session/cancel":
                emit("stop_input")
            if method == "session/load":
                in_load = True
                try:
                    return await super().handle(message)
                finally:
                    in_load = False
            return await super().handle(message)

        async def client_request(self, method, *args, **kwargs):
            result = await super().client_request(method, *args, **kwargs)
            if method == "session/request_permission":
                emit("permission_input")
            return result

    create_process = asyncio.create_subprocess_exec

    async def spawn(*args, **kwargs):
        process = await create_process(*args, **kwargs)
        read = process.stdout.read
        pipe_limit = kwargs["limit"]
        prefix = b""
        size = 0
        opened = False

        async def observe_read(length):
            nonlocal prefix, size, opened
            chunk = await read(length)
            pieces = chunk.split(b"\n")
            for index, piece in enumerate(pieces):
                if len(prefix) < 256:
                    prefix += piece[:256 - len(prefix)]
                size += len(piece)
                history_line = COMMAND.search(prefix) is not None
                if history_line and not opened and size >= target_bytes * 3 // 4:
                    emit("history_progress", reader_limit=pipe_limit)
                    opened = True
                if index < len(pieces) - 1:
                    if history_line:
                        emit("history_end", bytes=size + 1)
                    size, prefix, opened = 0, b"", False
            return chunk

        process.stdout.read = observe_read
        return process

    rpc = Session.rpc
    event = Session.event

    async def observe_rpc(session, command, **kwargs):
        abort = command == "abort" and session.id == HISTORY_ID
        if abort:
            emit("abort_submit")
        result = await rpc(session, command, **kwargs)
        if abort:
            emit("abort_ack")
        return result

    async def observe_event(session, frame):
        if frame.get("type") == "tool_execution_end" and (
            frame.get("tool_call_id") or frame.get("toolCallId")
        ) == TOOL_ID:
            emit("native_permission_tool_end")
        return await event(session, frame)

    async def heartbeat():
        nonlocal lag_ns
        while True:
            started = now()
            await asyncio.sleep(0.002)
            if in_load:
                lag_ns = max(lag_ns, now() - started - 2_000_000)

    server = InstrumentedServer(SimpleNamespace(
        aelix=aelix, state_dir=str(root / "state"), profile="coding", provider=None, model=None,
        extension=[], permission_mode="default", trust_project=False, turn_timeout=45,
    ))
    with contextlib.ExitStack() as patches:
        patches.enter_context(patch.object(asyncio, "create_subprocess_exec", spawn))
        patches.enter_context(patch.object(Session, "rpc", observe_rpc))
        patches.enter_context(patch.object(Session, "event", observe_event))
        if contended:
            cache = server_module.cache_recovered_history
            transaction = store._recovery_transaction
            waiting = False

            async def gate_cache(*args):
                emit("cache_ready")
                while not (root / "lock-held").exists():
                    await asyncio.sleep(0.001)
                return await cache(*args)

            def observe_transaction(*args, **kwargs):
                nonlocal waiting
                if not waiting:
                    emit("cache_write_attempt")
                    waiting = True
                return transaction(*args, **kwargs)

            patches.enter_context(patch.object(server_module, "cache_recovered_history", gate_cache))
            patches.enter_context(patch.object(store, "_recovery_transaction", observe_transaction))
        timer = asyncio.create_task(heartbeat())
        try:
            await server.run()
        finally:
            timer.cancel()
            await asyncio.gather(timer, return_exceptions=True)
            rss_bytes = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            emit("summary", rss_mib=round(rss_bytes / (MIB if sys.platform == "darwin" else 1024), 2),
                 max_loop_delay_ms=ms(lag_ns, 0))


class Peer:
    def __init__(self, process):
        self.process = process
        self.pending = {}
        self.sequence = 0
        self.metrics = {}
        self.changed = asyncio.Condition()
        self.permission = asyncio.get_running_loop().create_future()
        self.tasks = [asyncio.create_task(self.read()), asyncio.create_task(self.read_metrics())]

    async def read(self):
        while line := await self.process.stdout.readline():
            frame = json.loads(line)
            if frame.get("method") == "session/request_permission":
                self.permission.set_result(frame["id"])
            elif future := self.pending.get(frame.get("id")):
                future.set_result(frame)

    async def read_metrics(self):
        while line := await self.process.stderr.readline():
            if line.startswith(METRIC.encode()):
                value = json.loads(line[len(METRIC):])
                async with self.changed:
                    self.metrics[value["label"]] = value
                    self.changed.notify_all()

    async def metric(self, label):
        async with asyncio.timeout(90), self.changed:
            await self.changed.wait_for(lambda: label in self.metrics)
        return self.metrics[label]

    async def send(self, frame):
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", **frame}).encode() + b"\n")
        await self.process.stdin.drain()

    async def call(self, method, **params):
        self.sequence += 1
        identifier = self.sequence
        self.pending[identifier] = future = asyncio.get_running_loop().create_future()
        try:
            await self.send({"id": identifier, "method": method, "params": params})
            return await asyncio.wait_for(future, 90)
        finally:
            self.pending.pop(identifier)

    async def close(self):
        self.process.stdin.close()
        try:
            await asyncio.wait_for(self.process.wait(), 10)
        except TimeoutError:
            self.process.kill()
            await self.process.wait()
        await asyncio.gather(*self.tasks)


def seed(root, size_mib, kind):
    from aelix_acp.runtime.store import open_store

    filename = root / "transcript.jsonl"
    timestamp = "2026-01-01T00:00:00Z"
    if kind == "image":
        data = base64.b64encode(make_image("measure.png", 3 * MIB // 4)).decode()
        block = {"type": "image", "mime_type": "image/png", "data": data}
    else:
        block = {"type": "text", "text": "x" * (MIB // 4)}
    with filename.open("w", encoding="utf-8") as output:
        header = {"type": "session", "version": 3, "id": "fixture", "timestamp": timestamp, "cwd": str(root)}
        output.write(json.dumps(header) + "\n")
        parent = None
        for index in range(size_mib * (1 if kind == "image" else 4)):
            entry_id = f"{index:08x}"
            entry = {"type": "message", "id": entry_id, "parentId": parent, "timestamp": timestamp,
                     "message": {"role": "user", "content": [{"type": "text", "text": "fixture"}, block]}}
            output.write(json.dumps(entry) + "\n")
            parent = entry_id
    with contextlib.closing(open_store(str(root / "state"))) as db, db:
        db.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?)",
                   (HISTORY_ID, str(root), "coding", str(filename), "fixture", 1))


async def trial(root, aelix, size, kind, contended):
    proof = root / "approved.txt"

    async def endpoint(reader, writer):
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            length = next(int(line.split(b":", 1)[1]) for line in headers.lower().splitlines()
                          if line.startswith(b"content-length:"))
            messages = json.loads(await reader.readexactly(length))["messages"]
            if messages[-1]["role"] == "user":
                body = sse("one", tool_call_id=TOOL_ID, tool_name="write",
                           arguments={"path": str(proof), "content": "approved"})
            else:
                body = sse("one")
            header = (f"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                      f"Connection: close\r\nContent-Length: {len(body)}\r\n\r\n").encode()
            writer.write(header + body)
            await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    listener = await asyncio.start_server(endpoint, "127.0.0.1", 0)
    agent = root / "agent"
    agent.mkdir()
    (agent / "settings.json").write_text(json.dumps({
        "defaultProvider": "fixture", "defaultModel": "one", "compaction": {"enabled": False},
    }), encoding="utf-8")
    port = listener.sockets[0].getsockname()[1]
    (agent / "models.json").write_text(json.dumps({"providers": {"fixture": {
        "baseUrl": f"http://127.0.0.1:{port}/v1", "api": "openai-completions", "apiKey": "fixture",
        "models": [{"id": "one", "name": "Fixture", "input": ["text", "image"]}],
    }}}), encoding="utf-8")
    seed(root, size, kind)
    env = {key: os.environ[key] for key in ("PATH", "TMPDIR", "LANG", "PYTHONPATH") if key in os.environ}
    env.update(HOME=str(root), USERPROFILE=str(root), AELIX_CODING_AGENT_DIR=str(agent),
               AELIX_SETTINGS_PATH=str(agent / "settings.json"))
    proc = await asyncio.create_subprocess_exec(
        sys.executable, __file__, "--adapter", str(aelix), str(root), str(size * MIB), str(int(contended)),
        cwd=root, env=env, stdin=-1, stdout=-1, stderr=-1,
    )
    peer = Peer(proc)
    blocker = None
    try:
        params = {"cwd": str(root), "mcpServers": []}
        initialized = await peer.call("initialize", protocolVersion=1, clientCapabilities={"_meta": {
            "aelix/sessionRelay": {"version": 1, "notifications": False}, "aelix/hostBytes": {"version": 1},
        }})
        assert initialized["result"]["agentCapabilities"]["_meta"]["aelix/hostBytes"] == {"version": 1}
        sibling = (await peer.call("session/new", **params))["result"]["sessionId"]
        prompt = asyncio.create_task(peer.call("session/prompt", sessionId=sibling,
                                               prompt=[{"type": "text", "text": "write fixture"}]))
        permission_id = await asyncio.wait_for(peer.permission, 20)
        started = now()
        load = asyncio.create_task(peer.call("session/load", **params, sessionId=HISTORY_ID))
        progress = await peer.metric("history_progress")
        if contended:
            await peer.metric("cache_ready")
            blocker = sqlite3.connect(root / "state/sessions.sqlite3", timeout=0)
            blocker.execute("BEGIN IMMEDIATE")
            (root / "lock-held").touch()
            await peer.metric("cache_write_attempt")

        async def stop():
            sent = now()
            reply = await peer.call("session/cancel", sessionId=HISTORY_ID)
            return sent, now(), "result" in reply

        stopping = asyncio.create_task(stop())
        permission_sent = now()
        await peer.send({"id": permission_id, "result": {"outcome": {
            "outcome": "selected", "optionId": "reject-once" if contended else "allow-once",
        }}})
        # Both the native tool event and ACP Stop response must arrive before
        # releasing the external writer. Rejection leaves no file.
        tool_end = await peer.metric("native_permission_tool_end")
        stop_sent, stop_end, stop_ok = await stopping
        if blocker is not None:
            assert not load.done(), "Cache completed while an external writer still held its slot"
            blocker.rollback()
        load_response = await load
        load_ms = ms(now(), started)
        assert "result" in await prompt
        assert proof.exists() != contended
    finally:
        if blocker is not None:
            blocker.close()
        await peer.close()
        listener.close()
        await listener.wait_closed()

    def timing(label, start):
        entry = peer.metrics.get(label)
        return ms(entry["time"], start) if entry is not None else None

    outcome = load_response.get("error", {})
    return {
        "kind": kind, "target_mib": size, "contention": contended, "reader_limit": progress["reader_limit"],
        "frame_bytes": peer.metrics["history_end"]["bytes"],
        "load": outcome.get("data", {}).get("kind") or outcome.get("message", "ok"),
        "load_ms": load_ms, "stop_ok": stop_ok, "stop_ms": ms(stop_end, stop_sent),
        "stop_input_ms": timing("stop_input", stop_sent), "native_abort_submit_ms": timing("abort_submit", stop_sent),
        "native_abort_ack_ms": timing("abort_ack", stop_sent), "native_line_end_ms": timing("history_end", stop_sent),
        "permission_input_ms": timing("permission_input", permission_sent),
        "native_permission_tool_end_ms": ms(tool_end["time"], permission_sent),
        "file_written": proof.exists(), **{key: peer.metrics["summary"][key]
                                         for key in ("rss_mib", "max_loop_delay_ms")},
    }


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--adapter":
        _, _, aelix, root, target, contended = sys.argv
        asyncio.run(adapter(aelix, Path(root), int(target), contended == "1"))
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aelix", type=Path, required=True)
    parser.add_argument("--mib", type=int, required=True)
    parser.add_argument("--kind", choices=("text", "image"), required=True)
    parser.add_argument("--contended", action="store_true")
    args = parser.parse_args()
    # Resolve PYTHONPATH before the adapter switches to the isolated workspace.
    os.environ["PYTHONPATH"] = os.pathsep.join(str(Path(item).resolve()) for item in sys.path if item)
    with tempfile.TemporaryDirectory(prefix="acp-history-perf-") as path:
        result = asyncio.run(trial(Path(path).resolve(), args.aelix.resolve(), args.mib, args.kind, args.contended))
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
