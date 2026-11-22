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
from aelix_acp.server import Server, main

initialize = Server.__init__

def without_image_policy(self, args):
    initialize(self, args)
    self.image_policies = ()

Server.__init__ = without_image_policy
main()
"""

MOCK_CODEX = """
import base64
import hashlib
import json
from pathlib import Path

import httpx

def setup(api):
    from aelix_ai.providers import openai_codex_responses as codex

    def capture(request):
        assert request.method == "POST" and str(request.url) == "https://chatgpt.com/backend-api/codex/responses"
        body = json.loads(request.content)
        images = []
        for message in body["input"]:
            for part in message.get("content", []):
                if part["type"] != "input_image":
                    continue
                mime, encoded = part["image_url"].split(",", 1)
                raw = base64.b64decode(encoded, validate=True)
                images.append([mime, len(raw), hashlib.sha256(raw).hexdigest()])
        content = json.dumps(body["input"], sort_keys=True).encode()
        with Path("requests.jsonl").open("a", encoding="ascii") as file:
            file.write(json.dumps({"inputDigest": hashlib.sha256(content).hexdigest(), "images": images}) + "\\n")
        events = [{"type": "response.completed", "response": {"status": "completed", "output": [],
                   "usage": {"input_tokens": 1, "output_tokens": 0}}}]
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
            content="".join("data: " + json.dumps(event) + "\\n\\n" for event in events))

    codex._get_account_id = lambda _: "fixture-account"
    codex._create_codex_client = lambda _: httpx.AsyncClient(transport=httpx.MockTransport(capture))
"""


@unittest.skipUnless(EXECUTABLE, "Set AELIX_ACP_TEST_AELIX to the pinned native executable")
class NativeImagePromptTests(unittest.IsolatedAsyncioTestCase):
    async def test_ordinary_images_match_base_with_and_without_host_negotiation(self):
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
        for host in (False, True):
            with self.subTest(host=host):
                base = await self.capture(images, host=host, base=True, baseline_source=baseline_source)
                candidate = await self.capture(images, host=host, base=False)
                self.assertEqual(candidate, base)

    async def capture(self, images, *, host, base, baseline_source=None):
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
                                        "id": "gpt-6-luna",
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
                        "defaultModel": "gpt-6-luna",
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
                opened = await client.request("session/new", params)
                expected = []
                for mime, raw in images:
                    result = await client.request(
                        "session/prompt",
                        {
                            "sessionId": opened["sessionId"],
                            "prompt": [
                                {"type": "text", "text": "ordinary attachment"},
                                {"type": "image", "mimeType": mime, "data": base64.b64encode(raw).decode("ascii")},
                            ],
                        },
                    )
                    self.assertEqual(result, {"stopReason": "end_turn"})
                    expected.append([f"data:{mime};base64", len(raw), hashlib.sha256(raw).hexdigest()])
                    wire = json.loads((root / "requests.jsonl").read_text(encoding="ascii").splitlines()[-1])
                    self.assertEqual(wire["images"], expected)
                return (root / "requests.jsonl").read_text(encoding="ascii").splitlines()
            finally:
                await client.close()
