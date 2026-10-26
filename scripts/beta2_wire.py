"""Actual native/installed ACP subprocess probes against a synthetic loopback model."""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import secrets
import signal
import tempfile


def isolated_environment(root):
    env = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT", "WINDIR", "TMP", "TEMP") if k in os.environ}
    env.update(HOME=str(root / "home"), USERPROFILE=str(root / "home"),
               AELIX_CODING_AGENT_DIR=str(root / "agent"),
               AELIX_CODING_AGENT_SESSION_DIR=str(root / "sessions"),
               AELIX_SETTINGS_PATH=str(root / "agent/settings.json"),
               AELIX_AUTH_PATH=str(root / "agent/auth.json"), PYTHONUNBUFFERED="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    return env


class Peer:
    def __init__(self, process, acp=False):
        self.process, self.acp = process, acp
        self.counter, self.events, self.pending = 0, [], {}
        self.changed = asyncio.Event()
        self.tasks = [asyncio.create_task(self.read()), asyncio.create_task(self.drain_errors())]

    @classmethod
    async def start(cls, argv, root, env, acp=False):
        process = await asyncio.create_subprocess_exec(*map(str, argv), cwd=root, env=env,
            stdin=-1, stdout=-1, stderr=-1, limit=16 * 1024 * 1024,
            **({"start_new_session": True} if os.name != "nt" else {}))
        return cls(process, acp)

    async def send(self, frame):
        self.process.stdin.write((json.dumps(frame) + "\n").encode())
        await self.process.stdin.drain()

    async def read(self):
        try:
            while line := await self.process.stdout.readline():
                frame = json.loads(line)
                if frame.get("method") == "session/request_permission":
                    await self.send({"jsonrpc": "2.0", "id": frame["id"], "result": {
                        "outcome": {"outcome": "selected", "optionId": "reject-once"}}})
                elif (future := self.pending.get(frame.get("id"))) is not None:
                    future.set_result(frame)
                else:
                    self.events.append(frame)
                    self.changed.set()
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(RuntimeError("Qualification child closed its protocol stream"))

    async def drain_errors(self):
        while await self.process.stderr.read(8192):
            pass  # Never copy provider diagnostics or raw transcripts into the receipt.

    async def request(self, operation, /, **params):
        self.counter += 1
        key = str(self.counter)
        future = asyncio.get_running_loop().create_future()
        self.pending[key] = future
        try:
            frame = {"jsonrpc": "2.0", "id": key, "method": operation, "params": params} if self.acp else {
                "id": key, "type": operation, **params}
            await self.send(frame)
            return await asyncio.wait_for(future, 30)
        finally:
            self.pending.pop(key, None)

    async def until(self, predicate):
        async with asyncio.timeout(30):
            while not predicate():
                self.changed.clear()
                await self.changed.wait()

    async def close(self):
        if self.process.returncode is None:
            self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), 8)
            except TimeoutError:
                if os.name != "nt":
                    os.killpg(self.process.pid, signal.SIGTERM)
                else:
                    self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), 3)
                except TimeoutError:
                    if os.name != "nt":
                        os.killpg(self.process.pid, signal.SIGKILL)
                    else:
                        self.process.kill()
                    await self.process.wait()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


class Fixture:
    def __init__(self, root):
        self.root, self.requests, self.handlers = root, [], set()
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    async def start(self):
        self.server = await asyncio.start_server(self.respond, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        for name in ("home", "agent", "workspace"):
            (self.root / name).mkdir()
        models = {"providers": {"fixture": {"baseUrl": f"http://127.0.0.1:{port}/v1", "api": "openai-completions",
            "apiKey": "synthetic-fixture-key", "models": [{"id": name, "name": name, "reasoning": True,
                "input": ["text"], "contextWindow": 32768, "maxTokens": 1024} for name in ("one", "two")]}}}
        for name, value in (("models.json", models), ("settings.json", {"defaultProvider": "fixture", "defaultModel": "one"}),
                            ("auth.json", {"fixture": {"type": "api_key", "key": "synthetic-fixture-key"}})):
            (self.root / "agent" / name).write_text(json.dumps(value))
        self.env = isolated_environment(self.root)

    async def respond(self, reader, writer):
        task = asyncio.current_task()
        self.handlers.add(task)
        try:
            header = (await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)).decode()
            length = int(next(l.split(":", 1)[1] for l in header.split("\r\n") if l.lower().startswith("content-length:")))
            request = json.loads(await reader.readexactly(length))
            self.requests.append(request)
            if "QUALIFICATION_HOLD" in json.dumps(request["messages"][-1]):
                self.entered.set()
                await self.release.wait()
            chunks = []
            for delta, finish in (({"role": "assistant", "content": "QUALIFICATION_OK"}, None), ({}, "stop")):
                chunks.append({"id": "fixture", "object": "chat.completion.chunk", "created": 0,
                    "model": request["model"], "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                    "usage": {"prompt_tokens": 17, "completion_tokens": 3, "total_tokens": 20}})
            body = ("".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n").encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\nContent-Length: "
                         + str(len(body)).encode() + b"\r\n\r\n" + body)
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
            self.handlers.discard(task)

    async def close(self):
        self.release.set()
        self.server.close()
        await self.server.wait_closed()
        for task in list(self.handlers):
            task.cancel()
        await asyncio.gather(*self.handlers, return_exceptions=True)


def native_data(frame):
    if frame.get("success") is not True:
        raise AssertionError("Native command failed: " + str(frame.get("command")))
    return frame.get("data") or {}


async def probe_native(executable, manifest):
    with tempfile.TemporaryDirectory(prefix="aelix-beta2-") as temp:
        root = Path(temp).resolve()
        fixture = Fixture(root)
        await fixture.start()
        token = secrets.token_hex(24)
        ready = root / "probe.json"
        fixture.env.update(BETA2_PROBE_READY=str(ready), BETA2_PROBE_TOKEN=token)
        peer = await Peer.start([executable, "--mode", "rpc", "--offline", "--no-extensions", "--no-agents",
            "--no-skills", "--no-context-files", "-e", Path(__file__).with_name("beta2_probe.py"),
            "--provider", "fixture", "--model", "one"], root / "workspace", fixture.env)
        checks, gaps = [], {}
        try:
            state = native_data(await peer.request("get_state"))
            assert state["model"]["id"] == "one" and ready.exists()
            checks.append("native_cli_and_extension_start")
            for command, fragment in manifest["expectedClassicRpcGaps"].items():
                params = {"provider": "fixture", "modelId": "two"} if command == "set_model" else {}
                frame = await peer.request(command, **params)
                assert frame.get("success") is False and fragment.casefold() in frame.get("error", "").casefold(), frame
                gaps[command] = frame["error"]
            native_data(await peer.request("set_auto_retry", enabled=False))
            native_data(await peer.request("set_thinking_level", level="high"))
            assert native_data(await peer.request("get_state"))["thinkingLevel"] == "high"
            checks.append("classic_reasoning_set_and_get")

            async def prompt(text):
                start = len(peer.events)
                native_data(await peer.request("prompt", message=text))
                await peer.until(lambda: any(e.get("type") == "agent_end" for e in peer.events[start:]))

            await prompt("QUALIFICATION_HISTORY_MARKER")
            assert any(e.get("type") == "message_update" for e in peer.events)
            checks.append("actual_native_stream_with_synthetic_model")

            async def extension(action):
                reader, writer = await asyncio.open_connection("127.0.0.1", json.loads(ready.read_text())["port"])
                try:
                    writer.write((json.dumps({"token": token, "action": action}) + "\n").encode())
                    await writer.drain()
                    return json.loads(await asyncio.wait_for(reader.readline(), 10))["result"]
                finally:
                    writer.close()
                    await writer.wait_closed()

            snapshot = await extension("snapshot")
            assert set(snapshot["models"]) == {"one", "two"}
            assert snapshot["context"] and snapshot["context"]["context_window"] == 32768
            assert snapshot["context"]["tokens"] is not None
            checks.append("public_extension_registry_and_context_usage")
            assert (await extension("switch"))["accepted"] is True
            assert native_data(await peer.request("get_state"))["model"]["id"] == "two"
            await prompt("Confirm the earlier marker")
            assert fixture.requests[-1]["model"] == "two" and "QUALIFICATION_HISTORY_MARKER" in json.dumps(fixture.requests[-1])
            checks.append("public_extension_model_switch_preserves_native_history")
            pending = asyncio.create_task(prompt("QUALIFICATION_HOLD"))
            await asyncio.wait_for(fixture.entered.wait(), 15)
            native_data(await peer.request("abort"))
            await asyncio.wait_for(pending, 15)
            checks.append("native_abort_completes_held_turn")
            return {"checksPassed": checks, "classicRpcKnownGaps": gaps,
                    "contextSource": "public ExtensionContext; synthetic provider token usage",
                    "modelRoute": "test-only public extension API probe, not production ACP forwarding"}
        finally:
            fixture.release.set()
            await peer.close()
            await fixture.close()


async def probe_adapter(adapter, native, candidate=False):
    with tempfile.TemporaryDirectory(prefix="acp-beta2-") as temp:
        root = Path(temp).resolve()
        fixture = Fixture(root)
        await fixture.start()
        peer = await Peer.start([adapter, "--aelix", native, "--provider", "fixture", "--model", "one",
            "--state-dir", root / "state"], root / "workspace", fixture.env, acp=True)
        try:
            initialized = await peer.request("initialize", protocolVersion=1, clientCapabilities={})
            assert "result" in initialized
            created = await peer.request("session/new", cwd=str(root / "workspace"), mcpServers=[])
            if candidate:
                error = created.get("error", {})
                assert error.get("code") == -32004 and "runtime control v1 is required" in error.get("message", ""), created
                return {"outcome": "expected_preview_blocker", "reason": "PR #1 requires native control-v1; no legacy override used"}
            result = created["result"]
            session = result["sessionId"]
            assert result["models"]["currentModelId"] == "fixture/one"
            response = await peer.request("session/prompt", sessionId=session,
                                          prompt=[{"type": "text", "text": "QUALIFICATION_ACP"}])
            assert response["result"]["stopReason"] == "end_turn"
            assert any("QUALIFICATION_OK" in e.get("params", {}).get("update", {}).get("content", {}).get("text", "") for e in peer.events)
            rejected = await peer.request("session/set_model", sessionId=session, modelId="fixture/two")
            assert rejected.get("error", {}).get("code") == -32602
            pending = asyncio.create_task(peer.request("session/prompt", sessionId=session,
                prompt=[{"type": "text", "text": "QUALIFICATION_HOLD"}]))
            await asyncio.wait_for(fixture.entered.wait(), 15)
            assert "result" in await peer.request("session/cancel", sessionId=session)
            assert (await asyncio.wait_for(pending, 15))["result"]["stopReason"] == "cancelled"
            return {"checksPassed": ["installed_acp_start_and_approval_bridge", "native_launch_model_selector",
                "native_text_stream_through_acp", "acp_stop_cancels_actual_native_turn"],
                "knownGaps": ["model switching is launch-only", "no reasoning configOptions forwarding", "no context usage UI forwarding"],
                "nativeAndAdapterInterpretersSeparate": Path(adapter).parent != Path(native).parent}
        finally:
            fixture.release.set()
            await peer.close()
            await fixture.close()
