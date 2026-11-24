"""Check the pinned SDK's final encoding with the ACP policy and a loopback provider."""

import asyncio
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from support import AcpClient

EXECUTABLE = os.environ.get("AELIX_ACP_TEST_AELIX")
PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNgYGAAAAAEAAH2FzhVAAAAAElFTkSuQmCC"
)
MODEL_CONTENT_TYPE = "application/x-fixture"
MULTIPART = "multipart/form-data"
JSON_CHARSET = "application/json; charset=utf-8"
PROMPT = '한글 "quote" \\ slash\n line\t control \x01'

FIXTURE = """
import hashlib
import json
from pathlib import Path
from runpy import run_path

from aelix_agent_core.harness.hooks import BeforeProviderPayloadResult, BeforeProviderRequestResult

def setup(api):
    root = Path.cwd()
    policy_file = Path(__file__).parents[0] / "acp-policy-path"
    encode = run_path(policy_file.read_text())["provider_body_bytes"]

    async def before_request(event, ctx):
        case = json.loads((root / "case.json").read_text())
        return BeforeProviderRequestResult(stream_options={"headers": case["providerHeaders"]})

    async def before_payload(event, ctx):
        case = json.loads((root / "case.json").read_text())
        payload = {**event.payload, "extra_headers": case["extraHeaders"]}
        data = encode(payload, event.model.api)
        (root / "measured.json").write_text(json.dumps([len(data), hashlib.sha256(data).hexdigest()]))
        return BeforeProviderPayloadResult(payload=payload)

    api.on("before_provider_request", before_request)
    api.on("before_provider_payload", before_payload)
"""


@unittest.skipUnless(EXECUTABLE, "Set AELIX_ACP_TEST_AELIX to the pinned native executable")
class NativeSdkImageEncodingTests(unittest.IsolatedAsyncioTestCase):
    async def test_completions_content_type(self):
        await self.check_sdk("openai-completions")

    async def test_responses_content_type(self):
        await self.check_sdk("openai-responses")

    async def check_sdk(self, api):
        with tempfile.TemporaryDirectory(prefix="acp-sdk-images-", dir="/tmp" if os.name != "nt" else None) as tmp:
            root = Path(tmp).resolve()
            (root / "home").mkdir()
            (root / "agent").mkdir()
            requests = []

            async def endpoint(reader, writer):
                try:
                    head = (await reader.readuntil(b"\r\n\r\n")).decode("ascii").splitlines()
                    headers = [line.split(":", 1) for line in head[1:] if ":" in line]
                    length = next(int(value) for name, value in headers if name.lower() == "content-length")
                    body = await reader.readexactly(length)
                    media = [value.strip() for name, value in headers if name.lower() == "content-type"]
                    requests.append((head[0], media, body))
                    if api == "openai-completions":
                        event = {"id": "fixture", "object": "chat.completion.chunk", "model": "vision",
                                 "created": 0, "choices": [{"index": 0, "delta": {"content": "ok"},
                                                            "finish_reason": "stop"}]}
                    else:
                        event = {"type": "response.completed", "response": {
                            "status": "completed", "output": [], "usage": {"input_tokens": 1, "output_tokens": 0},
                        }}
                    response = ("data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n").encode()
                    writer.write(
                        b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n"
                        + f"Content-Length: {len(response)}\r\n\r\n".encode() + response
                    )
                    await writer.drain()
                finally:
                    writer.close()
                    await writer.wait_closed()

            listener = await asyncio.start_server(endpoint, "127.0.0.1", 0)
            async with listener:
                base = f"http://127.0.0.1:{listener.sockets[0].getsockname()[1]}/v1"
                model = {"id": "vision", "name": "Vision fixture", "input": ["text", "image"],
                         "contextWindow": 196608, "maxTokens": 4096,
                         "headers": {"Content-Type": MODEL_CONTENT_TYPE}}
                (root / "agent/models.json").write_text(json.dumps({"providers": {"fixture": {
                    "api": api, "apiKey": "fixture-key", "baseUrl": base, "models": [model],
                }}}))
                (root / "agent/settings.json").write_text(json.dumps({
                    "defaultProvider": "fixture", "defaultModel": "vision",
                    "compaction": {"enabled": False}, "retry": {"enabled": False},
                }))
                policy = {"route": ["fixture", "vision", api, base], "imageBytes": 2048, "sidePixels": 8000,
                          "rawBytes": 8192, "count": 20, "bodyBytes": 256 << 10,
                          "contextTokens": 196608, "imageTokens": 8192, "outputTokens": 4096}
                entry = root / "entry.py"
                entry.write_text(
                    "from aelix_acp.server import Server, main\n"
                    "original = Server.__init__\n"
                    "def init(self, args):\n"
                    "    original(self, args)\n"
                    f"    self.image_policies = ({policy!r},)\n"
                    "Server.__init__ = init\n"
                    "main()\n"
                )
                fixture = root / "fixture.py"
                fixture.write_text(FIXTURE)
                (root / "acp-policy-path").write_text(
                    str(Path(__file__).resolve().parents[1] / "src/aelix_acp/image_policy.py")
                )
                offer = {"_meta": {"aelix/sessionRelay": {"version": 1, "notifications": False},
                                   "aelix/hostBytes": {"version": 1}}}
                client = await AcpClient.start(root, EXECUTABLE, extensions=(fixture,), capabilities=offer,
                                               entry=(str(entry),))
                try:
                    for label, provider_headers, extra_headers, expected_media in (
                        ("model defaults", {}, {}, None),
                        ("provider multipart", {"Content-Type": MULTIPART}, {}, None),
                        ("extra multipart", {"Content-Type": JSON_CHARSET}, {"Content-Type": MULTIPART}, None),
                        ("case variants", {"Content-Type": JSON_CHARSET}, {"cOnTeNt-TyPe": MULTIPART}, None),
                        ("other media", {"Content-Type": JSON_CHARSET}, {"Content-Type": "text/plain"}, None),
                        ("JSON override", {"Content-Type": MULTIPART}, {"Content-Type": JSON_CHARSET}, [JSON_CHARSET]),
                        ("duplicate JSON", {"Content-Type": JSON_CHARSET}, {"content-type": "APPLICATION/JSON"}, None),
                        ("combined media", {}, {"Content-Type": "application/json; charset=utf-8, text/plain"}, None),
                        ("quoted parameter", {}, {"Content-Type": 'APPLICATION/JSON; profile="a,b"'},
                         ['APPLICATION/JSON; profile="a,b"']),
                    ):
                        with self.subTest(label=label):
                            (root / "case.json").write_text(json.dumps({
                                "providerHeaders": provider_headers, "extraHeaders": extra_headers,
                            }))
                            (root / "measured.json").unlink(missing_ok=True)
                            before = len(requests)
                            opened = await client.request("session/new", {"cwd": str(root), "mcpServers": [],
                                "_meta": {"aelix/sessionRelay": {"bindingId": "sdk-encoding"}}})
                            session_id = opened["sessionId"]
                            prompt = {"sessionId": session_id, "prompt": [
                                {"type": "text", "text": PROMPT},
                                *[{"type": "image", "mimeType": "image/png", "data": PNG}] * 2,
                            ]}
                            if expected_media is None:
                                with self.assertRaisesRegex(RuntimeError, r'"code": -33002'):
                                    await client.request("session/prompt", prompt)
                                self.assertEqual(len(requests), before, "Unverifiable request reached the provider")
                            else:
                                self.assertEqual(await client.request("session/prompt", prompt), {"stopReason": "end_turn"})
                                self.assertEqual(len(requests), before + 1)
                                path, media, wire = requests[-1]
                                self.assertIn("/v1/" + ("chat/completions" if api.endswith("completions") else "responses"), path)
                                self.assertEqual(media, expected_media)
                                self.assertEqual(wire.count(PNG.encode("ascii")), 2)
                                measured = json.loads((root / "measured.json").read_text())
                                self.assertEqual(measured, [len(wire), hashlib.sha256(wire).hexdigest()])
                            self.assertTrue((root / "measured.json").is_file(), "The native payload hook did not run")
                            await client.request("session/close", {"sessionId": session_id})
                finally:
                    await client.close()
