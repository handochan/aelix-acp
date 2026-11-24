"""Exercise A-1 through the protected listener of the H-1 draft Host."""

import argparse
import asyncio
import base64
import contextlib
import hashlib
import json
import os
import secrets
import socket
import sqlite3
import sys
import tempfile
from pathlib import Path
from urllib import error, request

STANDARD_MARKERS = {
    "assistant": r"private-assistant-delta-문-\"",
    "tool": "private-tool-path-문",
    "rpcError": r"private-standard-error-문-\"",
}


def marker_variants(marker):
    variants = {marker}
    for _ in range(2):
        variants.update(
            json.dumps(value, ensure_ascii=ascii_only)[1:-1]
            for value in tuple(variants)
            for ascii_only in (False, True)
        )
    return {value.encode("utf-8") for value in variants}


async def cancel_h1_http_after_handoff(root, studio, bun, caller_config, handoffs):
    fixture = Path(__file__).resolve().parent / "fixtures/running_input_host.ts"
    log_path = root / "studio-caller.log"
    with log_path.open("wb") as log:
        caller = await asyncio.create_subprocess_exec(
            str(bun), "--tsconfig-override", str(studio / "tsconfig.json"), str(fixture),
            cwd=studio, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=log,
        )

    async def command(text):
        caller.stdin.write(text.encode("utf-8") + b"\n")
        await caller.stdin.drain()

    async def result():
        line = await caller.stdout.readline()
        assert line, log_path.read_text(encoding="utf-8", errors="replace")
        return json.loads(line)

    ack_writer = None
    try:
        async with asyncio.timeout(20):
            await command(json.dumps(caller_config))
            # A native ACK marks handoff. The actual Studio transport aborts its
            # fetch while ACP still holds the unsettled H1 receipt.
            handoff = asyncio.create_task(handoffs.get())
            sent = asyncio.create_task(result())
            try:
                await asyncio.wait({handoff, sent}, return_when=asyncio.FIRST_COMPLETED)
                if sent.done():
                    raise AssertionError(f"Studio finished before native handoff: {sent.result()}")
                ack_reader, ack_writer = handoff.result()
                assert await ack_reader.readline() == b"written 1\n"
                await command("cancel")
                observed = await sent
            finally:
                for task in (handoff, sent):
                    if not task.done():
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await task
            receipt = observed["receipt"]
            assert receipt["status"] == "unknown", receipt
            assert receipt["failure"] == {"status": 0} and observed["preserveDraft"]
            assert receipt["requestId"] == caller_config["requestId"] and observed["writes"] == 1
            await command("lookup")
            assert await result() == {"status": "pending"}
            ack_writer.write(b"!")
            await ack_writer.drain()
            assert await ack_reader.readline() == b"settled 1\n"
            await command("lookup")
            found = await result()
            assert found["writes"] == 1 and found["result"]["status"] == "completed", found
            final = found["result"]["receipt"]
            for key in ("bindingId", "turnId", "requestId", "mode"):
                assert receipt[key] == final[key], found
            assert final["status"] == "queued" and final["captionAdded"], found
            caller.stdin.close()
            assert await caller.wait() == 0, log_path.read_text(encoding="utf-8", errors="replace")
            return receipt, observed["grant"]
    finally:
        caller.stdin.close()
        if ack_writer is not None:
            ack_writer.close()
            await ack_writer.wait_closed()
        if caller.returncode is None:
            caller.kill()
            await caller.wait()


async def host_run(root, host_bin, aelix, studio, bun, *, expect_sdk_leak=False):
    from spike_image_window import Probe, sse
    from test_running_images import key

    for item in ("home", "agent", "workspace", "host", "images"):
        (root / item).mkdir()
    probe = Probe(root, aelix)
    probe.add_image("private-file-marker.png", 250000)
    fixture = root / "images/private-file-marker.png"
    raw = fixture.read_bytes()
    prompt_marker = "private-synthetic-prompt-marker"
    error_marker = 'private-provider-error-문-"'
    standard = STANDARD_MARKERS
    tool_path = root / "workspace" / (standard["tool"] + ".txt")
    tool_path.write_text("Synthetic tool result.", encoding="utf-8")
    wire_control = root / "standard-frame-markers.jsonl"
    inject_tool = False
    reject = False

    async def endpoint(reader, writer):
        nonlocal inject_tool
        try:
            header = (await reader.readuntil(b"\r\n\r\n")).splitlines()
            length = next(
                int(line.split(b":", 1)[1]) for line in header if line.lower().startswith(b"content-length:")
            )
            payload = json.loads(await reader.readexactly(length))
            _, images = probe.summarize(payload["messages"], provider=True)
            if not probe.requests:
                probe.requests.append({"images": []})
                probe.arrivals.put_nowait(None)
                await probe.gate.wait()
            else:
                probe.requests.append({"images": [item[0] for item in images]})
            if reject:
                response = json.dumps(
                    {"error": {"message": error_marker, "type": "invalid_request_error"}}
                ).encode()
                status, media = "400 Bad Request", "application/json"
            else:
                if inject_tool:
                    inject_tool = False
                    response = sse(
                        payload["model"],
                        tool_call_id="trace_control",
                        tool_name="read",
                        arguments={"path": str(tool_path)},
                    )
                else:
                    response = sse(payload["model"], text=standard["assistant"])
                status, media = "200 OK", "text/event-stream"
            header = (
                f"HTTP/1.1 {status}\r\nContent-Type: {media}\r\nContent-Length: {len(response)}\r\n"
                "Connection: close\r\n\r\n"
            )
            writer.write(header.encode("ascii") + response)
            await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    listener = await asyncio.start_server(endpoint, "127.0.0.1", 0)
    url = f"http://127.0.0.1:{listener.sockets[0].getsockname()[1]}/v1"
    (root / "agent/models.json").write_text(
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
                                "name": "Feature Fixture",
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
    (root / "agent/settings.json").write_text(
        json.dumps(
            {
                "defaultProvider": "fixture",
                "defaultModel": "one",
                "compaction": {"enabled": False},
                "retry": {"enabled": False},
            }
        )
    )
    handoffs = asyncio.Queue()
    handoff_listener = await asyncio.start_server(
        lambda reader, writer: handoffs.put_nowait((reader, writer)), "127.0.0.1", 0
    )
    handoff_port = handoff_listener.sockets[0].getsockname()[1]
    source = Path(__file__).resolve().parents[1] / "src"
    policy = {
        "route": ["fixture", "one", "openai-completions", url],
        "imageBytes": 3750000,
        "sidePixels": 8000,
        "rawBytes": 8 << 20,
        "count": 20,
        "bodyBytes": 12 << 20,
        "contextTokens": 196608,
        "imageTokens": 8192,
        "outputTokens": 4096,
    }
    launcher = root / "launch.py"
    launcher.write_text(f"""import asyncio
import json
import sys
from pathlib import Path
sys.path.insert(0, {str(source)!r})
import aelix_acp.server as server

rpc = server.Session.rpc
image_writes = 0
settlement = {{}}

async def hold_image_receipt(self, command, *, _image_write=None, **kwargs):
    if _image_write is None:
        return await rpc(self, command, **kwargs)
    current, issued = _image_write

    def count_write():
        global image_writes
        issued()
        image_writes += 1

    # A real native ACK proves the grant was acquired and stdin consumed.
    result = await rpc(self, command, _image_write=(current, count_write), **kwargs)
    reader, writer = await asyncio.open_connection("127.0.0.1", {handoff_port})
    settlement[asyncio.current_task()] = writer
    writer.write(f"written {{image_writes}}\\n".encode("ascii"))
    await writer.drain()
    assert await reader.readexactly(1) == b"!"
    return result

server.Session.rpc = hold_image_receipt

class TestServer(server.Server):
    def __init__(self, args):
        super().__init__(args)
        self.image_policies = ({policy!r},)
        self.fixture_prompt_ids = set()

    async def handle(self, message):
        identifier = message.get("id")
        if message.get("method") == "session/prompt":
            self.fixture_prompt_ids.add(identifier)
        try:
            await super().handle(message)
        finally:
            self.fixture_prompt_ids.discard(identifier)
            writer = settlement.pop(asyncio.current_task(), None)
            if writer is not None:
                writer.write(f"settled {{image_writes}}\\n".encode("ascii"))
                await writer.drain()
                writer.close()
                await writer.wait_closed()

    async def send(self, message, **kwargs):
        # Positive control at the ACP/Host boundary, after normal ACP sanitization.
        markers = {standard!r}
        error = message.get("error")
        if isinstance(error, dict) and message.get("id") in self.fixture_prompt_ids:
            assert {error_marker!r} not in str(error)
            message = {{**message, "error": {{**error, "message": markers["rpcError"]}}}}
        wire = json.dumps(message, ensure_ascii=False)
        observed = [key for key, value in markers.items() if json.dumps(value, ensure_ascii=False)[1:-1] in wire]
        if observed:
            with Path({str(wire_control)!r}).open("a", encoding="utf-8") as out:
                out.write(json.dumps(observed) + "\\n")
        await super().send(message, **kwargs)

server.Server = TestServer
server.main()
""")
    token = secrets.token_hex(32)
    env = {
        name: os.environ[name] for name in ("PATH", "SYSTEMROOT", "WINDIR", "TMP", "TEMP") if name in os.environ
    }
    env.update(
        HOME=str(root / "home"),
        USERPROFILE=str(root / "home"),
        AELIX_CODING_AGENT_DIR=str(root / "agent"),
        AELIX_SETTINGS_PATH=str(root / "agent/settings.json"),
        AIONUI_DISABLE_AUTO_UPDATE="1",
        OPENAI_LOG="debug",
    )

    def get(path, method, body):
        r = request.Request(
            f"http://127.0.0.1:{port}" + path,
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"x-aionui-studio-auth": token, "Content-Type": "application/json"},
        )
        try:
            with request.urlopen(r, timeout=30) as response:
                return response.status, json.load(response)
        except error.HTTPError as e:
            return e.code, json.load(e)

    async def api(path, method="GET", body=None):
        status, result = await asyncio.to_thread(get, path, method, body)
        assert status in (200, 201, 202), (path, status, result.get("code"))
        return result["data"]

    async def start():
        nonlocal port
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        with (root / "host-captured.log").open("ab") as log:
            proc = await asyncio.create_subprocess_exec(
                str(host_bin),
                "--local",
                "--studio-auth",
                "--port",
                str(port),
                "--data-dir",
                str(root / "host"),
                "--managed-resources-mode",
                "bundled",
                "--log-level",
                "trace",
                cwd=root / "workspace",
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=log,
                stderr=log,
            )
        proc.stdin.write(token.encode())
        await proc.stdin.drain()
        proc.stdin.close()
        try:
            async with asyncio.timeout(45):
                while proc.returncode is None:
                    try:
                        await api("/api/assistants")
                        return proc
                    except OSError:
                        await asyncio.sleep(0.1)
            raise AssertionError(f"Host exited during startup ({proc.returncode})")
        except BaseException:
            if proc.returncode is None:
                proc.terminate()
                await proc.wait()
            raise

    port = None
    proc = await start()
    proc.terminate()
    await proc.wait()
    with contextlib.closing(sqlite3.connect(root / "host/aionui-backend.db")) as db, db:
        db.execute(
            """INSERT INTO agent_metadata
            (id,agent_id,user_id,name,backend,agent_type,agent_source,command,args,created_at,updated_at)
            VALUES ('fixture-aelix','fixture-aelix',NULL,'Fixture ACP','aelix','acp','builtin',?,?,1,1)""",
            (
                sys.executable,
                json.dumps(
                    [
                        str(launcher),
                        "--aelix",
                        str(aelix),
                        "--permission-mode",
                        "yolo",
                        "--state-dir",
                        str(root / "acp"),
                    ]
                ),
            ),
        )
    proc = await start()
    try:
        created = await api(
            "/api/conversations",
            "POST",
            {
                "type": "acp",
                "name": "A-1 local qualification",
                "extra": {
                    "agent_id": "fixture-aelix",
                    "backend": "aelix",
                    "workspace": str(root / "workspace"),
                    "custom_workspace": True,
                },
            },
        )
        scope = "/api/conversations/" + created["id"]
        await api(scope + "/runtime/ensure", "POST")

        async def wait_idle():
            async with asyncio.timeout(10):
                while True:
                    runtime = (await api(scope)).get("runtime") or {}
                    if (
                        runtime.get("state") in ("idle", None)
                        and not runtime.get("is_processing")
                        and runtime.get("turn_id") is None
                    ):
                        return
                    await asyncio.sleep(0.05)

        await api(scope + "/messages", "POST", {"content": prompt_marker})
        await asyncio.wait_for(probe.arrivals.get(), 10)
        receipt, grant = await cancel_h1_http_after_handoff(
            root, studio, bun,
            {"port": port, "token": token, "conversationId": created["id"],
             "requestId": key(1), "path": str(fixture)},
            handoffs,
        )
        assert grant["sha256"] == hashlib.sha256(raw).hexdigest()
        binding = receipt["bindingId"]
        probe.gate.set()
        async with asyncio.timeout(10):
            while len(probe.requests) < 2:
                await asyncio.sleep(0.05)
        assert probe.requests[-1]["images"] == ["private-file-marker.png"]
        async with asyncio.timeout(5):
            while True:
                payload = {"bindingId": binding, "method": "_aelix/session/input/image-state", "payload": {}}
                code, answer = await asyncio.to_thread(get, scope + "/acp-extensions", "POST", payload)
                if code == 200 and answer["data"]["result"]["status"] == "available":
                    break
                await asyncio.sleep(0.05)
        await wait_idle()
        inject_tool = True
        seen = len(probe.requests)
        await api(scope + "/messages", "POST", {"content": "exercise standard tool frame"})
        async with asyncio.timeout(10):
            while len(probe.requests) < seen + 2:
                await asyncio.sleep(0.05)
        await wait_idle()
        seen = len(probe.requests)
        reject = True
        await api(scope + "/messages", "POST", {"content": "receive injected provider error"})
        async with asyncio.timeout(15):
            while len(probe.requests) <= seen or '"rpcError"' not in wire_control.read_text(encoding="utf-8"):
                await asyncio.sleep(0.05)
            state_payload = {"bindingId": binding, "method": "_aelix/session/input/image-state", "payload": {}}
            while True:
                code, response = await asyncio.to_thread(get, scope + "/acp-extensions", "POST", state_payload)
                if code == 409 and response.get("code") == "EXT_NO_LIVE_TASK":
                    break
                if code == 200 and response["data"]["result"]["status"] == "unavailable":
                    break
                await asyncio.sleep(0.05)
    finally:
        probe.gate.set()
        handoff_listener.close()
        await handoff_listener.wait_closed()
        listener.close()
        await listener.wait_closed()
        proc.terminate()
        await proc.wait()
    logs = (root / "host-captured.log").read_bytes()
    logs += b"".join(p.read_bytes() for p in (root / "host/logs").glob("*") if p.is_file())
    assert b"TRACE" in logs
    observed = {
        name for line in wire_control.read_text(encoding="utf-8").splitlines() for name in json.loads(line)
    }
    assert observed == set(standard), (observed, set(standard))
    for marker in ("private-file-marker.png", prompt_marker, error_marker):
        assert not any(variant in logs for variant in marker_variants(marker)), (
            "Private fixture marker in diagnostics"
        )
    leaked = sorted(
        name for name, marker in standard.items() if any(variant in logs for variant in marker_variants(marker))
    )
    if expect_sdk_leak:
        assert leaked, "The old SDK TRACE positive control did not log any standard frame marker"
    else:
        assert not leaked, f"Standard frame markers in Host diagnostics: {leaked}"
    assert base64.b64encode(raw)[:76] not in logs
    assert hashlib.sha256(raw).hexdigest().encode() not in logs
    assert grant["id"].encode() not in logs
    print(
        json.dumps(
            {
                "hostBytes": True,
                "idempotentPrepare": True,
                "providerImages": 1,
                "httpCancelledOutcome": receipt["status"],
                "originalKeyRecovered": True,
                "nativeImageWrites": 1,
                "observedStandardFrames": sorted(observed),
                "loggedStandardMarkers": leaked,
            }
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", type=Path, required=True)
    parser.add_argument("--aelix", type=Path, required=True)
    parser.add_argument("--studio", type=Path, required=True, help="Studio checkout with its dependencies installed")
    parser.add_argument("--bun", default="bun", help="Bun executable")
    parser.add_argument("--expect-sdk-leak", action="store_true", help="positive control for the old H-1 binary")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="acp-h1-pair-") as location:
        asyncio.run(
            host_run(
                Path(location).resolve(),
                args.host.resolve(),
                args.aelix.resolve(),
                args.studio.resolve(),
                args.bun,
                expect_sdk_leak=args.expect_sdk_leak,
            )
        )


if __name__ == "__main__":
    main()
