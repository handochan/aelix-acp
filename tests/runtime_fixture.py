"""Shared real-process fixture; the model endpoint is synthetic and local."""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from support import AcpClient


class RuntimeFixture(unittest.IsolatedAsyncioTestCase):
    executable_env = "AELIX_ACP_TEST_AELIX"
    runtime_profile = "control-v1"

    async def asyncSetUp(self):
        self.executable = os.environ[self.executable_env]
        self.temp = tempfile.TemporaryDirectory(prefix="acp-native-", dir="/tmp" if os.name != "nt" else None)
        self.root = Path(self.temp.name).resolve()
        (self.root / "home").mkdir()
        (self.root / "agent").mkdir()
        self.model_requests = []
        self.write_path = self.root / "approved.txt"
        self.failures = 0
        self.model_gate = None
        self.listener = await asyncio.start_server(self.model, "127.0.0.1", 0)
        port = self.listener.sockets[0].getsockname()[1]
        models = {"providers": {"fixture": {"baseUrl": f"http://127.0.0.1:{port}/v1", "api": "openai-completions",
            "apiKey": "synthetic-fixture-key", "models": [
                {"id": "one", "name": "Fixture One", "reasoning": True, "input": ["text", "image"],
                 "contextWindow": 32768, "maxTokens": 2048},
                {"id": "two", "name": "Fixture Two", "reasoning": False, "input": ["text"],
                 "contextWindow": 32768, "maxTokens": 2048}]}}}
        (self.root / "agent/models.json").write_text(json.dumps(models))
        (self.root / "agent/settings.json").write_text(json.dumps({"defaultProvider": "fixture", "defaultModel": "one"}))
        (self.root / "agent/auth.json").write_text(json.dumps({"fixture": {"type": "api_key", "key": "synthetic-fixture-key"}}))
        extension = self.root / "commands.py"
        extension.write_text('''import asyncio
async def echo(args, ctx): return "echo:" + args
async def choose(args, ctx):
    selected = await ctx.ui.select("Pick", ["one", "two"])
    return "selected:" + str(selected)
async def wait(args, ctx): await asyncio.Event().wait()
def setup(api):
    api.register_command("echo", description="Echo", handler=echo)
    api.register_command("choose", description="Choose", handler=choose)
    api.register_command("wait", description="Wait", handler=wait)
''')
        self.client = await AcpClient.start(self.root, self.executable, extensions=[extension],
                                          capabilities={"_meta": {"aelix/clientActions": True}}, runtime_profile=self.runtime_profile)
        created = await self.client.request("session/new", {"cwd": str(self.root), "mcpServers": []}, timeout=120)
        self.session = created["sessionId"]
        self.created = created

    async def asyncTearDown(self):
        await self.client.close()
        self.listener.close()
        await self.listener.wait_closed()
        self.temp.cleanup()

    async def model(self, reader, writer):
        try:
            header = (await reader.readuntil(b"\r\n\r\n")).decode()
            length = int(next(line.split(":", 1)[1].strip() for line in header.split("\r\n") if line.lower().startswith("content-length:")))
            request = json.loads(await reader.readexactly(length))
            self.model_requests.append(request)
            if self.model_gate is not None:
                await self.model_gate.wait()
            messages = request.get("messages", [])
            last = messages[-1] if messages else {}
            content = str(last.get("content", ""))
            if last.get("role") == "user" and "WRITE_TEST" in content:
                delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": "call_" + str(len(self.model_requests)),
                    "type": "function", "function": {"name": "write", "arguments": json.dumps({
                        "path": str(self.write_path), "content": "approved write"})}}]}
                stop = "tool_calls"
            else:
                delta, stop = {"role": "assistant", "content": "NATIVE_OK"}, "stop"
            def event(delta, finish):
                return "data: " + json.dumps({"id": "fixture", "object": "chat.completion.chunk", "created": 0,
                    "model": request.get("model", "one"), "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}) + "\n\n"
            body = (event(delta, None) + event({}, stop) + "data: [DONE]\n\n").encode()
            if self.failures:
                self.failures -= 1
                body = b'data: {"error":{"message":"overloaded fixture","type":"overloaded_error"}}\n\n'
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\nContent-Length: "
                         + str(len(body)).encode() + b"\r\n\r\n" + body)
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    async def prompt(self, text, key=None):
        return await self.client.request("session/prompt", {"sessionId": self.session,
            "prompt": [{"type": "text", "text": text}], **({"_meta": {"aelix/requestId": key}} if key else {})})

    async def config(self, key, value):
        return await self.client.request("session/set_config_option", {"sessionId": self.session, "configId": key, "value": value})

    async def manage(self, action, arguments=None):
        return await self.client.request("_aelix/manage", {"sessionId": self.session, "action": action, "arguments": arguments or {}})
