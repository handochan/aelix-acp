"""Compare normal Codex image requests with the pre-A-1b adapter."""

import base64
import hashlib
import io
import json
import os
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from support import AcpClient

EXECUTABLE = os.environ.get("AELIX_ACP_TEST_AELIX")
H1 = "aelix/sessionRelay"
BASELINE = """
from aelix_acp import image_policy
from aelix_acp.server import Server, main

initialize = Server.__init__

def without_image_policy(self, args):
    initialize(self, args)
    self.image_policies = ()

# The base has no eligible route and does not inspect queued provenance
# when the worker did not negotiate a live image policy.
Server.__init__ = without_image_policy
image_policy.has_running_image = lambda _: False
main()
"""

MOCK_CODEX = """
import base64
from dataclasses import replace
import hashlib
import json
from pathlib import Path

import httpx
from aelix_agent_core.harness.hooks import MessageEndEventResult
from aelix_agent_core.types import AgentTool
from aelix_ai.messages import ImageContent, TextContent
from aelix_ai.tools import ToolResult

PLACEHOLDER = "[Earlier image omitted from this request due to the image limit.]"
REMOTE = "https://example.invalid/ordinary-tool-image.png"
ORIGIN = "aelix-acp:running-image:v1"

def setup(api):
    from aelix_ai.providers import openai_codex_responses as codex

    async def fixture_image(*args, **kwargs):
        pixels = base64.b64encode(Path("tool.png").read_bytes()).decode("ascii")
        return ToolResult(content=[ImageContent(mime_type="image/png", data=pixels)])

    async def fixture_remote(*args, **kwargs):
        return ToolResult(content=[ImageContent(source=REMOTE)])

    async def seed_history(event, ctx):
        message = event.message
        if message.role == "user" and message.content and isinstance(message.content[0], TextContent):
            if message.content[0].text == "seed queued provenance":
                content = [replace(block, source=ORIGIN) if isinstance(block, ImageContent) else block
                           for block in message.content]
                return MessageEndEventResult(message=replace(message, content=content))
        return None

    for name, execute in (("fixture_image", fixture_image), ("fixture_remote", fixture_remote)):
        api.register_tool(AgentTool(
            name=name, description="Return an offline test image",
            parameters={"type": "object", "properties": {}}, execute=execute,
        ))
    api.on("message_end", seed_history)

    def capture(request):
        assert request.method == "POST" and str(request.url) == "https://chatgpt.com/backend-api/codex/responses"
        body = json.loads(request.content)
        images = []
        placeholders = 0
        tool_images = 0
        latest = ""
        has_tool_output = False
        for message in body["input"]:
            tool_output = message.get("type") == "function_call_output"
            has_tool_output |= tool_output
            for part in message.get("output" if tool_output else "content", []):
                if part["type"] == "input_text":
                    placeholders += part["text"].count(PLACEHOLDER)
                    if message.get("role") == "user":
                        latest = part["text"]
                if part["type"] != "input_image":
                    continue
                url = part["image_url"]
                tool_images += int(tool_output)
                if url.startswith("data:"):
                    mime, encoded = url.split(",", 1)
                    raw = base64.b64decode(encoded, validate=True)
                    images.append([mime, len(raw), hashlib.sha256(raw).hexdigest()])
                else:
                    images.append([url])
        content = json.dumps(body["input"], sort_keys=True).encode()
        with Path("requests.jsonl").open("a", encoding="ascii") as file:
            row = {"inputDigest": hashlib.sha256(content).hexdigest(), "images": images,
                   "toolImages": tool_images, "placeholders": placeholders}
            file.write(json.dumps(row) + "\\n")
        response = {"status": "completed", "output": [], "usage": {"input_tokens": 1, "output_tokens": 0}}
        events = []
        tool = {"exercise fixture tool": "fixture_image", "exercise remote tool": "fixture_remote"}.get(latest)
        if tool and not has_tool_output:
            item = {"type": "function_call", "id": "fc_fixture", "call_id": "call_fixture",
                    "name": tool, "arguments": "{}"}
            events.append({"type": "response.output_item.done", "output_index": 0, "item": item})
            response["output"].append(item)
        events.append({"type": "response.completed", "response": response})
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
            content="".join("data: " + json.dumps(event) + "\\n\\n" for event in events))

    codex._get_account_id = lambda _: "fixture-account"
    codex._create_codex_client = lambda _: httpx.AsyncClient(transport=httpx.MockTransport(capture))
"""


@unittest.skipUnless(EXECUTABLE, "Set AELIX_ACP_TEST_AELIX to the pinned native executable")
class NativeImagePromptTests(unittest.IsolatedAsyncioTestCase):
    async def test_image_policy_matrix_against_base(self):
        from PIL import Image

        def fixture(fmt):
            with io.BytesIO() as output:
                Image.new("RGB", (1, 1), "white").save(output, fmt)
                return output.getvalue()

        gif, png = fixture("GIF"), fixture("PNG")
        gif_comment = b"!\xfe" + (b"\xff" + b"x" * 255) * 16384 + b"\0"
        png_chunk = b"npAD" + bytes(4_296_339 - len(png) - 12)
        images = [
            ("image/gif", gif),
            ("image/png", png),
            ("image/gif", gif[:-1] + gif_comment + gif[-1:]),
            (
                "image/png",
                png[:-12]
                + struct.pack(">I", len(png_chunk) - 4)
                + png_chunk
                + struct.pack(">I", zlib.crc32(png_chunk))
                + png[-12:],
            ),
        ]
        baseline_source = os.environ.get("AELIX_ACP_TEST_BASE_SRC")
        for host, qualified in ((False, True), (False, False), (True, True), (True, False)):
            with self.subTest(host=host, qualified=qualified):
                base = await self.capture(
                    images, host=host, qualified=qualified, base=True, baseline_source=baseline_source
                )
                candidate = await self.capture(images, host=host, qualified=qualified, base=False)
                self.assertEqual(candidate["tool"], base["tool"])
                if host and qualified:
                    self.assertEqual(candidate["resume"], base["resume"])
                    self.assertEqual(candidate["remote"], "rejected")
                else:
                    self.assertEqual(candidate["ordinary"], base["ordinary"])
                    self.assertEqual(candidate["remote"], base["remote"])
                    self.assertEqual(candidate["resume"]["images"], base["resume"]["images"][:-1])
                    self.assertEqual(candidate["resume"]["placeholders"], 1)

    async def capture(self, images, *, host, qualified, base, baseline_source=None):
        limited = host and qualified and not base
        model = "gpt-6-luna" if qualified else "fixture-vision"
        with tempfile.TemporaryDirectory(
            prefix="acp-prompt-images-", dir="/tmp" if os.name != "nt" else None
        ) as path:
            root = Path(path).resolve()
            (root / "home").mkdir()
            (root / "agent").mkdir()
            (root / "agent/models.json").write_text(
                json.dumps(
                    {
                        "providers": {
                            "openai-codex": {
                                "baseUrl": "https://chatgpt.com/backend-api",
                                "api": "openai-codex-responses",
                                "apiKey": "synthetic-fixture",
                                "models": [
                                    {
                                        "id": model,
                                        "name": "Fixture",
                                        "input": ["text", "image"],
                                        "contextWindow": 1_050_000,
                                        "maxTokens": 128_000,
                                    }
                                ],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            (root / "agent/settings.json").write_text(
                json.dumps(
                    {
                        "defaultProvider": "openai-codex",
                        "defaultModel": model,
                        "compaction": {"enabled": False},
                        "retry": {"enabled": False},
                    }
                ),
                encoding="utf-8",
            )
            transport = root / "mock-codex.py"
            transport.write_text(MOCK_CODEX, encoding="utf-8")
            options = {}
            if base and baseline_source:
                options["extra_env"] = {"PYTHONPATH": str(Path(baseline_source).resolve())}
            elif base:
                entry = root / "base-adapter.py"
                entry.write_text(BASELINE, encoding="utf-8")
                options["entry"] = (str(entry),)
            meta = {H1: {"version": 1, "notifications": False}, "aelix/hostBytes": {"version": 1}}
            client = await AcpClient.start(
                root, EXECUTABLE, extensions=[transport], capabilities={"_meta": meta} if host else {}, **options
            )
            try:
                params = {"cwd": str(root), "mcpServers": []}
                if host:
                    params["_meta"] = {H1: {"bindingId": "fixture-binding"}}
                (root / "tool.png").write_bytes(images[1][1])
                capture_path = root / "requests.jsonl"

                def rows():
                    return [json.loads(line) for line in capture_path.read_text(encoding="ascii").splitlines()]

                async def prompt(sid, text, image=None):
                    content = [{"type": "text", "text": text}]
                    if image:
                        mime, raw = image
                        content.append({"type": "image", "mimeType": mime, "data": base64.b64encode(raw).decode()})
                    result = await client.request("session/prompt", {"sessionId": sid, "prompt": content})
                    self.assertEqual(result, {"stopReason": "end_turn"})
                    return rows()[-1]

                async def new():
                    return (await client.request("session/new", params))["sessionId"]

                async def close(sid):
                    await client.request("session/close", {"sessionId": sid})

                def describe(image):
                    mime, raw = image
                    return [f"data:{mime};base64", len(raw), hashlib.sha256(raw).hexdigest()]

                ordinary = []
                expected = []
                sid = await new()
                for index, image in enumerate(images, 1):
                    mime, raw = image
                    wire = await prompt(sid, "ordinary attachment", image)
                    if limited and (mime != "image/png" or len(raw) > 3_750_000):
                        expected = []
                    else:
                        expected.append(describe(image))
                    self.assertEqual(wire["images"], expected)
                    self.assertEqual(wire["placeholders"], index - len(expected))
                    ordinary.append(wire)
                await close(sid)

                sid = await new()
                png = images[1]
                await prompt(sid, "ordinary attachment", png)
                tool = await prompt(sid, "exercise fixture tool")
                self.assertEqual(tool["toolImages"], 1)
                self.assertEqual(tool["images"], [describe(png)] * 2)
                await prompt(sid, "seed queued provenance", png)
                await close(sid)
                await client.request("session/load", {**params, "sessionId": sid})
                resumed = await prompt(sid, "replay history")
                self.assertEqual(resumed["toolImages"], 1)
                self.assertEqual(resumed["images"], [describe(png)] * (3 if base or limited else 2))
                await close(sid)

                sid = await new()
                if limited:
                    before = len(rows())
                    with self.assertRaisesRegex(RuntimeError, r'"code": -33002'):
                        await prompt(sid, "exercise remote tool")
                    self.assertEqual(len(rows()), before + 1)
                    remote = "rejected"
                else:
                    remote = await prompt(sid, "exercise remote tool")
                    self.assertEqual(remote["images"], [["https://example.invalid/ordinary-tool-image.png"]])
                return {"ordinary": ordinary, "tool": tool, "resume": resumed, "remote": remote}
            finally:
                await client.close()
