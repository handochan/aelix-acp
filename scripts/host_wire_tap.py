"""TEST ONLY transparent ACP stdio tap; records structure, never payload values.

An explicitly enabled synthetic reverse RPC tests the host's unsupported path.
It is intercepted locally on the return path, never delivered to native ACP.
"""
import asyncio
import json
import os
from pathlib import Path
import signal
import sys
import time


async def main():
    child = await asyncio.create_subprocess_exec(*sys.argv[1:], stdin=-1, stdout=-1, stderr=-1, start_new_session=True)
    log = Path(os.environ["PROBE_WIRE_LOG"])
    output = asyncio.Lock()

    def record(direction, frame):
        params = frame.get("params", {})
        entry = {"direction": direction, "method": frame.get("method"), "hasId": "id" in frame,
                 "paramKeys": sorted(params), "metaKeys": sorted(params.get("_meta", {})), "observedAtMonotonic": time.monotonic()}
        if isinstance(params.get("update"), dict):
            entry["sessionUpdate"] = params["update"].get("sessionUpdate")
        if isinstance(frame.get("error"), dict):
            entry["errorCode"] = frame["error"].get("code")
        if isinstance(frame.get("result"), dict):
            result = frame["result"]
            entry["resultKeys"] = sorted(result)
            entry["stopReason"] = result.get("stopReason")
        entry["fixtureReverseReply"] = frame.get("id") == "probe-reverse-fixture"
        with log.open("a") as stream:
            stream.write(json.dumps(entry) + "\n")

    async def emit(line):
        async with output:
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()

    async def outgoing():
        while line := await child.stdout.readline():
            frame = json.loads(line)
            record("adapter_to_host", frame)
            await emit(line)

    async def incoming():
        injected = False
        reader = asyncio.StreamReader(limit=16 * 1024 * 1024)
        await asyncio.get_running_loop().connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
        while line := await reader.readline():
            frame = json.loads(line)
            record("host_to_adapter", frame)
            if frame.get("id") == "probe-reverse-fixture":
                continue
            child.stdin.write(line)
            await child.stdin.drain()
            # Inject after the first prompt: the host's active reader now owns
            # this connection (the startup handshake has a separate reader).
            if not injected and frame.get("method") == "session/prompt":
                injected = True
                reverse = {"jsonrpc": "2.0", "id": "probe-reverse-fixture", "method": "_aelix/client_action",
                           "params": {"sessionId": frame["params"]["sessionId"], "action": "dialog", "arguments": {}}}
                record("fixture_to_host", reverse)
                await emit((json.dumps(reverse) + "\n").encode())
        child.stdin.close()

    async def errors():
        while await child.stderr.read(8192):
            pass  # no provider diagnostics in this tap or the host logs

    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGHUP):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    tasks = [asyncio.create_task(f()) for f in (incoming, outgoing, errors, stop.wait)]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()  # surface malformed output/reader failures
    finally:
        child.stdin.close()
        if child.returncode is None:
            try:
                await asyncio.wait_for(child.wait(), 3)
            except TimeoutError:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(child.wait(), 3)
                except TimeoutError:
                    os.killpg(child.pid, signal.SIGKILL)
                    await child.wait()
        # The leader can exit before its native child. This is our own group.
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
