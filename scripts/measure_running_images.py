"""Measure the ACP process, native workers and HTTP fixture on the production reader path."""

import argparse
import asyncio
import base64
import contextlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from time import perf_counter

from spike_image_window import make_image, sse

MIB = 1024**2
ROOT = Path(__file__).resolve().parents[1]
H1 = {"aelix/sessionRelay": {"version": 1, "notifications": False}, "aelix/hostBytes": {"version": 1}}


async def adapter(aelix, root, url):
    from types import SimpleNamespace

    from aelix_acp.server import Server

    class MeasureServer(Server):
        async def dispatch_request(self, method, params):
            if method == "fixture/get-messages":
                answer = await self.session(params).rpc("get_messages")
                return {"count": len(answer["messages"])}
            return await super().dispatch_request(method, params)

    server = MeasureServer(
        SimpleNamespace(
            aelix=aelix,
            state_dir=str(root / "state"),
            profile="coding",
            provider=None,
            model=None,
            extension=[],
            permission_mode="yolo",
            trust_project=False,
            turn_timeout=60,
        )
    )
    server.image_policies = (
        {
            "route": ["fixture", "one", "openai-completions", url],
            "imageBytes": 3_750_000,
            "sidePixels": 8000,
            "rawBytes": 8 * MIB,
            "count": 20,
            "bodyBytes": 12 * MIB,
            "contextTokens": 196608,
            "imageTokens": 8192,
            "outputTokens": 4096,
        },
    )
    await server.run()


class Peer:
    def __init__(self, proc):
        self.proc = proc
        self.pending = {}
        self.sequence = 0
        self.reader = asyncio.create_task(self.read())

    async def read(self):
        while line := await self.proc.stdout.readline():
            frame = json.loads(line)
            if future := self.pending.get(frame.get("id")):
                future.set_result(frame)

    async def call(self, method, **params):
        self.sequence += 1
        identifier = self.sequence
        self.pending[identifier] = future = asyncio.get_running_loop().create_future()
        try:
            message = {"jsonrpc": "2.0", "id": identifier, "method": method, "params": params}
            self.proc.stdin.write((json.dumps(message) + "\n").encode())
            await self.proc.stdin.drain()
            reply = await future
            assert "error" not in reply, (method, reply.get("error"))
            return reply["result"]
        finally:
            self.pending.pop(identifier)

    async def close(self):
        self.proc.stdin.close()
        await self.proc.wait()
        await self.reader
        assert self.proc.returncode == 0


def rss_snapshot(adapter_pid):
    output = subprocess.check_output(["ps", "-ax", "-o", "pid=,ppid=,rss="], text=True)
    rows = [tuple(map(int, row.split())) for row in output.splitlines() if len(row.split()) == 3]
    rss = {pid: kib for pid, _, kib in rows}
    native = [kib for _, parent, kib in rows if parent == adapter_pid]
    return {
        "fixture": rss.get(os.getpid(), 0),
        "acp": rss.get(adapter_pid, 0),
        "nativeTotal": sum(native),
        "nativeLargest": max(native, default=0),
    }


async def scenario(root, aelix, batches, workers, history_rpc):
    held = asyncio.Queue()
    release = asyncio.Event()
    phase = "history"
    peaks = {}
    settled = False

    async def endpoint(reader, writer):
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            length = next(
                int(row.split(b":", 1)[1])
                for row in headers.splitlines()
                if row.lower().startswith(b"content-length:")
            )
            request = json.loads(await reader.readexactly(length))
            if phase == "stop":
                held.put_nowait(None)
                await release.wait()
            payload = sse(request["model"])
            writer.write(
                f"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {len(payload)}\r\n"
                "Connection: close\r\n\r\n".encode()
                + payload
            )
            await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    listener = await asyncio.start_server(endpoint, "127.0.0.1", 0)
    url = f"http://127.0.0.1:{listener.sockets[0].getsockname()[1]}/v1"
    agent = root / "agent"
    agent.mkdir()
    (agent / "models.json").write_text(
        json.dumps(
            {
                "providers": {
                    "fixture": {
                        "baseUrl": url,
                        "api": "openai-completions",
                        "apiKey": "fixture-key",
                        "models": [
                            {
                                "id": "one",
                                "name": "Fixture",
                                "input": ["text", "image"],
                                "contextWindow": 196608,
                                "maxTokens": 4096,
                            }
                        ],
                    }
                }
            }
        )
    )
    (agent / "settings.json").write_text(
        json.dumps(
            {
                "defaultProvider": "fixture",
                "defaultModel": "one",
                "compaction": {"enabled": False},
                "retry": {"enabled": False},
            }
        )
    )
    env = {key: os.environ[key] for key in ("PATH", "TMPDIR", "LANG") if key in os.environ}
    env.update(PYTHONPATH=str(ROOT / "src"), HOME=str(root), AELIX_CODING_AGENT_DIR=str(agent))
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(Path(__file__).resolve()),
        "--adapter",
        str(aelix),
        str(root),
        url,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        env=env,
    )
    peer = Peer(proc)

    async def sample():
        while not settled:
            sample_phase = phase
            row = await asyncio.to_thread(rss_snapshot, proc.pid)
            peak = peaks.setdefault(sample_phase, dict.fromkeys(row, 0))
            for process, kib in row.items():
                peak[process] = max(peak[process], kib)
            await asyncio.sleep(0.025)

    sampler = asyncio.create_task(sample())
    try:
        await peer.call("initialize", protocolVersion=1, clientCapabilities={"_meta": H1})
        identifiers = []
        for _ in range(workers):
            opened = await peer.call(
                "session/new",
                cwd=str(root),
                mcpServers=[],
                _meta={"aelix/sessionRelay": {"bindingId": f"binding-{len(identifiers)}"}},
            )
            identifiers.append(opened["sessionId"])
        image = {
            "type": "image",
            "mimeType": "image/png",
            "data": base64.b64encode(make_image("memory.png", 2 * MIB)).decode(),
        }

        async def prompt(session_id, text, pixels):
            return await peer.call(
                "session/prompt",
                sessionId=session_id,
                prompt=[{"type": "text", "text": text}, *([image, image] if pixels else [])],
            )

        for index in range(batches):
            await asyncio.gather(*(prompt(session_id, f"history {index}", True) for session_id in identifiers))
        phase = "get_messages"
        if history_rpc:
            counts = await asyncio.gather(*(peer.call("fixture/get-messages", sessionId=s) for s in identifiers))
            assert all(row["count"] >= batches * 2 for row in counts)
        phase = "stop"
        running = [
            asyncio.create_task(prompt(session_id, "active Stop boundary", False)) for session_id in identifiers
        ]
        for _ in identifiers:
            await asyncio.wait_for(held.get(), timeout=20)
        start = perf_counter()
        await asyncio.gather(*(peer.call("session/cancel", sessionId=s) for s in identifiers))
        assert all(item["stopReason"] == "cancelled" for item in await asyncio.gather(*running))
        stop_ms = round((perf_counter() - start) * 1000)
        phase = "after_stop"
        release.set()
        assert all(
            item["stopReason"] == "end_turn"
            for item in await asyncio.gather(
                *(prompt(session_id, "after Stop", False) for session_id in identifiers)
            )
        )
    finally:
        settled = True
        release.set()
        await sampler
        await peer.close()
        listener.close()
        await listener.wait_closed()
    return {
        "historyMiBPerWorker": batches * 4,
        "workers": workers,
        "getMessages": history_rpc,
        "stopAllMs": stop_ms,
        "sampledPeakMiBByPhase": {
            phase: {key: round(kib / 1024, 1) for key, kib in row.items()} for phase, row in peaks.items()
        },
    }


async def run(aelix):
    # Fresh fixture per row keeps prior HTTP/json allocations out of subsequent peaks.
    for batches, workers, history_rpc in ((1, 1, False), (8, 1, False), (8, 1, True), (8, 2, False), (8, 2, True)):
        result = await asyncio.create_subprocess_exec(
            sys.executable,
            str(Path(__file__).resolve()),
            "--case",
            str(aelix),
            str(batches),
            str(workers),
            str(int(history_rpc)),
        )
        assert await result.wait() == 0


def main():
    if sys.argv[1:2] == ["--adapter"]:
        _, _, aelix, root, url = sys.argv
        asyncio.run(adapter(aelix, Path(root), url))
    elif sys.argv[1:2] == ["--case"]:
        _, _, aelix, batches, workers, history_rpc = sys.argv
        with tempfile.TemporaryDirectory(prefix="acp-image-rss-") as root:
            result = asyncio.run(
                scenario(Path(root).resolve(), Path(aelix), int(batches), int(workers), bool(int(history_rpc)))
            )
        print(json.dumps(result), flush=True)
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--aelix", type=Path, required=True)
        asyncio.run(run(parser.parse_args().aelix.resolve()))


if __name__ == "__main__":
    main()
