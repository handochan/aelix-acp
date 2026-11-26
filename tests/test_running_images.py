"""Image negotiation, Host wire validation and native write boundaries."""

import asyncio
import base64
import hashlib
import json
import ntpath
import runpy
import struct
import sys
import unittest
import weakref
import zlib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory, TemporaryFile
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_session_relay as text_relay

from aelix_acp import image_policy
from aelix_acp.runtime import host_bytes, native_frames, session_relay
from aelix_acp.server import Server
from aelix_acp.session import Session
from aelix_acp.wire.errors import ProtocolError
from aelix_acp.wire.transport import StdoutWriter


def png_chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def malformed_jpegs():
    header = bytes.fromhex("ffd8 ffc0000b080001000101011100")
    return (
        header + bytes.fromhex("ffda000600003f0000"),  # Zero scan components.
        header + bytes.fromhex("ffda0008010200003f0000"),
    )  # Component 2 absent from SOF.


def png(width=1, height=1, before_data=b""):
    return (
        image_policy.PNG_SIGNATURE
        + png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + before_data
        + png_chunk(b"IDAT", b"test")
    )


class ImagePolicyTests(unittest.TestCase):
    def test_bounded_png_validation(self):
        self.assertEqual(image_policy.inspect_image(png(8000, 8000)), "image/png")
        for image, reason in [
            (png(8001), "image-dimensions"),
            (png(0), "image-dimensions"),
            (png()[:-1], "invalid-image-header"),
            (png()[0:28], "invalid-image-header"),
            (png(before_data=png_chunk(b"acTL", b"\0" * 8)), "animated-image"),
            (png(before_data=png_chunk(b"npAD", b"a" * 262_144)), "invalid-image-header"),
            (b"GIF89a", "unsupported-image"),
        ]:
            with self.subTest(reason=reason), self.assertRaisesRegex(image_policy.ImageRejected, reason):
                image_policy.inspect_image(image)

    def test_jpeg_reads_through_scan_header_and_rejects_truncation(self):
        jpeg = bytes.fromhex("ffd8 ffe00002 ffc0000b080001000101011100 ffda0008010100003f00 00")
        self.assertEqual(image_policy.inspect_image(jpeg), "image/jpeg")
        for raw in (
            jpeg[:-1],
            jpeg[:-3],
            jpeg[:19],
            jpeg.replace(b"\x00\x01\x01\x01\x11", b"\x00\x00\x01\x01\x11"),
        ):
            with self.assertRaises(image_policy.ImageRejected):
                image_policy.inspect_image(raw)

    def test_final_payload_counts_all_bytes_and_visual_tokens(self):
        raw = png()
        encoded = "data:image/png;base64," + base64.b64encode(raw).decode()
        image = {"type": "image_url", "image_url": {"url": encoded}}
        payload = {"messages": [{"role": "user", "content": [image]}], "tools": [{"name": "lookup"}]}
        expected = {("image/png", hashlib.sha256(raw).digest(), len(raw)): 1}
        policy = {
            "route": ("fixture", "one", "openai-completions", "https://fixture.invalid"),
            "imageBytes": 1024,
            "sidePixels": 4,
            "rawBytes": 2048,
            "count": 1,
            "bodyBytes": 1024,
            "contextTokens": 1024,
            "imageTokens": 200,
            "outputTokens": 100,
        }
        image_policy.validate_payload(payload, policy, expected)
        for limits, reason in [
            ({"bodyBytes": 10}, "body-limit"),
            ({"contextTokens": 300}, "context-limit"),
            ({"imageBytes": len(raw) - 1}, "image-bytes"),
            ({"count": 0}, "image-limit"),
        ]:
            with self.subTest(reason=reason), self.assertRaisesRegex(image_policy.ImageRejected, reason):
                image_policy.validate_payload(payload, {**policy, **limits}, expected)
        with self.assertRaisesRegex(image_policy.ImageRejected, "provider-image-mismatch"):
            image_policy.validate_payload(payload, policy, {})

    def test_responses_projects_base64_and_data_url_with_text_and_tool_output(self):
        @dataclass
        class Image:
            data: str = ""
            source: str = ""
            mime_type: str = "image/png"

        @dataclass
        class Text:
            text: str

        @dataclass
        class Message:
            role: str
            content: list

        policy = image_policy.VERIFIED_IMAGE_POLICIES[0]
        raw = png()
        encoded = base64.b64encode(raw).decode()
        url = "data:image/png;base64," + encoded
        messages = [
            Message("user", [Text("retain text"), Image(data=encoded)]),
            Message("toolResult", [Image(source=url) for _ in range(19)]),
            Message("user", [Image(data=image_policy.RUNNING_IMAGE_WIRE_PREFIX + encoded)]),
        ]
        projected, expected, stats = image_policy.project_images(
            messages, image_policy.limits(policy), Image, Text
        )
        self.assertEqual(
            stats,
            {"keptCount": 20, "keptRawBytes": 20 * len(raw), "omittedCount": 1, "reasons": {"image_count": 1}},
        )
        self.assertEqual(projected[0].content, [Text("retain text"), Text(image_policy.PLACEHOLDER)])
        self.assertIsInstance(messages[0].content[1], Image)
        self.assertEqual(
            image_policy.decode_image(encoded, "image/png", limit=1024, max_side=8000), next(iter(expected))
        )
        image = {"type": "input_image", "image_url": url, "detail": "auto"}
        request = {
            "input": [
                {"role": "user", "content": [{"type": "input_text", "text": image_policy.PLACEHOLDER}]},
                {
                    "type": "function_call_output",
                    "call_id": "call_fixture",
                    "output": [{"type": "input_text", "text": "mixed"}, *([image] * 19)],
                },
                {"role": "user", "content": [image]},
            ]
        }
        image_policy.validate_payload(request, policy, expected)
        request["input"][0]["content"].append(image)
        with self.assertRaisesRegex(image_policy.ImageRejected, "^provider-image-limit$"):
            image_policy.validate_payload(request, policy, expected)

    def test_responses_checks_raw_budget_after_decode(self):
        policy = image_policy.VERIFIED_IMAGE_POLICIES[0]
        expected = Counter()
        parts = []
        for length in (
            image_policy.MAX_FILE,
            image_policy.MAX_FILE,
            image_policy.MAX_REQUEST_BYTES - 2 * image_policy.MAX_FILE,
        ):
            header = png()
            raw = header + bytes(length - len(header))
            expected["image/png", hashlib.sha256(raw).digest(), length] += 1
            parts.append(
                {"type": "input_image", "image_url": "data:image/png;base64," + base64.b64encode(raw).decode()}
            )
        image_policy.validate_payload({"input": [{"role": "user", "content": tuple(parts)}]}, policy, expected)
        parts[-1]["image_url"] = "data:image/png;base64," + base64.b64encode(raw + b"\0").decode()
        with self.assertRaisesRegex(image_policy.ImageRejected, "^provider-image-limit$"):
            image_policy.validate_payload({"input": [{"role": "user", "content": tuple(parts)}]}, policy, expected)

    def test_responses_rejects_malformed_parts_with_fixed_diagnostics(self):
        policy = image_policy.VERIFIED_IMAGE_POLICIES[0]
        b64 = base64.b64encode(png()).decode()
        good = {"type": "input_image", "image_url": "data:image/png;base64," + b64}
        for part, reason in (
            ({**good, "image_url": b64}, "unmeasurable-provider-payload"),
            ({**good, "image_url": {"url": good["image_url"]}}, "unmeasurable-provider-payload"),
            ({**good, "image_url": "https://example.invalid/private.png"}, "unmeasurable-provider-payload"),
            ({**good, "image_url": "data:image/png;base64,!private!"}, "unmeasurable-image"),
            ({**good, "image_url": "data:image/jpeg;base64," + b64}, "image-mime"),
            ({**good, "file_id": "file-private"}, "unmeasurable-provider-payload"),
            ({**good, "detail": "original"}, "unmeasurable-provider-payload"),
            ({"type": "input_text", "text": ["private"]}, "unmeasurable-provider-payload"),
            ({"type": "image_url", "image_url": {"url": good["image_url"]}}, "unmeasurable-provider-payload"),
            (good, "provider-image-mismatch"),
        ):
            with self.subTest(reason=reason), self.assertRaisesRegex(image_policy.ImageRejected, f"^{reason}$"):
                image_policy.validate_payload({"input": [{"role": "user", "content": [part]}]}, policy, Counter())

    def test_openai_responses_shape_counts_its_output_budget(self):
        policy = {
            **image_policy.VERIFIED_IMAGE_POLICIES[0],
            "route": ("openai", "example", "openai-responses", "https://example.invalid"),
        }
        raw = png()
        encoded = "data:image/png;base64," + base64.b64encode(raw).decode()
        expected = Counter({("image/png", hashlib.sha256(raw).digest(), len(raw)): 1})
        request = {
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "mixed"},
                        {"type": "input_image", "image_url": encoded},
                    ],
                }
            ]
        }
        image_policy.validate_payload(request, policy, expected)
        with self.assertRaisesRegex(image_policy.ImageRejected, "^provider-context-limit$"):
            image_policy.validate_payload(
                {**request, "max_output_tokens": policy["contextTokens"]}, policy, expected
            )
        with self.assertRaisesRegex(image_policy.ImageRejected, "^unmeasurable-provider-payload$"):
            image_policy.validate_payload({"input": "malformed"}, policy, Counter())

    def test_responses_materializes_sequences_and_ignores_tool_schema(self):
        from collections import namedtuple

        policy = image_policy.VERIFIED_IMAGE_POLICIES[0]
        raw = png()
        part = {"type": "input_image", "image_url": "data:image/png;base64," + base64.b64encode(raw).decode()}
        expected = Counter({("image/png", hashlib.sha256(raw).digest(), len(raw)): 20})
        pair = namedtuple("Parts", "text image")({"type": "input_text", "text": "mixed"}, part)
        request = {
            "input": (
                {"role": "user", "content": pair},
                {"type": "function_call_output", "call_id": "call_fixture", "output": (part,) * 19},
            ),
            "tools": [
                {
                    "type": "function",
                    "name": "example",
                    "parameters": {"type": "object", "properties": {"example": {"enum": [part]}}},
                }
            ],
        }
        normalized = image_policy.validate_payload(request, policy, expected)
        self.assertEqual(len(normalized["input"]), 2)
        self.assertIsInstance(normalized["input"][0]["content"], list)
        self.assertIsInstance(normalized["input"][1]["output"], list)
        request["input"][1]["output"] += (part,)
        with self.assertRaisesRegex(image_policy.ImageRejected, "^provider-image-limit$"):
            image_policy.validate_payload(request, policy, expected)

    def test_responses_rejects_unknown_null_and_file_inputs(self):
        policy = image_policy.VERIFIED_IMAGE_POLICIES[0]
        for entries in (
            [None],
            [{"type": "made_up", "role": "user", "content": "x"}],
            [{"type": "input_file", "file_id": "private"}],
            [{"role": "assistant", "content": [None]}],
            [{"role": "user", "content": [{"type": "made_up"}]}],
            [{"role": "user", "content": [{"type": "input_file", "file_id": "private"}]}],
            [{"type": "function_call_output", "call_id": "call_fixture", "output": [None]}],
            [
                {
                    "type": "function_call_output",
                    "call_id": "call_fixture",
                    "output": [{"type": "input_file", "file_id": "private"}],
                }
            ],
            [{"type": "function_call_output", "call_id": "call_fixture", "output": range(1)}],
            [{"type": "reasoning", "summary": [None]}],
        ):
            with (
                self.subTest(entries=entries),
                self.assertRaisesRegex(image_policy.ImageRejected, "^unmeasurable-provider-payload$"),
            ):
                image_policy.validate_payload({"input": entries}, policy, Counter())

    def test_only_running_image_occurrences_are_placeholdered_for_unverified_route(self):
        @dataclass
        class Image:
            data: str
            source: str = ""
            mime_type: str = "image/png"

        @dataclass
        class Text:
            text: str

        @dataclass
        class Message:
            role: str
            content: list

        encoded = base64.b64encode(png()).decode()
        regular = Image(encoded)
        queued = Image(image_policy.RUNNING_IMAGE_WIRE_PREFIX + encoded)
        message = Message("user", [regular, queued])
        replaced = image_policy.preserve_image_origin(message, Image)
        self.assertEqual(replaced.content, [regular, Image(encoded, image_policy.RUNNING_IMAGE_SOURCE)])
        for original in (message, replaced):
            projection, images, stats = image_policy.project_images([original], None, Image, Text)
            self.assertEqual(projection[0].content, [regular, Text(image_policy.PLACEHOLDER)])
            self.assertFalse(images)
            self.assertEqual(stats["reasons"], {"model-unverified": 1})
        budget = image_policy.limits(image_policy.VERIFIED_IMAGE_POLICIES[0])
        projection, images, _ = image_policy.project_images([message], budget, Image, Text)
        self.assertEqual(projection[0].content, replaced.content)
        self.assertEqual(images.total(), 2)

    def test_full_request_validator_counts_ordinary_images_and_rejects_remote(self):
        policy = image_policy.VERIFIED_IMAGE_POLICIES[0]
        raw = png()
        part = {"type": "input_image", "image_url": "data:image/png;base64," + base64.b64encode(raw).decode()}
        payload = {"input": [{"role": "user", "content": [part] * 26}]}
        expected = Counter({("image/png", hashlib.sha256(raw).digest(), len(raw)): 20})
        with self.assertRaisesRegex(image_policy.ImageRejected, "^provider-image-limit$"):
            image_policy.validate_payload(payload, policy, expected)
        payload["input"][0]["content"] = [part, {**part, "image_url": "https://example.invalid/image.png"}]
        with self.assertRaisesRegex(image_policy.ImageRejected, "^unmeasurable-provider-payload$"):
            image_policy.validate_payload(payload, policy, expected)

    def test_running_history_marker_crosses_read_boundary(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            path.write_bytes(
                json.dumps({"text": '"source": "' + image_policy.RUNNING_IMAGE_SOURCE + '"'}).encode()
            )
            self.assertFalse(image_policy.has_running_image(path))
            marker = json.dumps({"source": image_policy.RUNNING_IMAGE_SOURCE}).encode()
            path.write_bytes(b" " * (65536 - len(marker) // 2) + marker)
            self.assertTrue(image_policy.has_running_image(path))


class HostBytesTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_and_finish_retry_same_reader_and_offset(self):
        raw = png()
        digest = hashlib.sha256(raw).hexdigest()
        parts = [(0, raw[:30], False), (30, raw[30:], True)]
        replies = [
            {
                "offset": offset,
                "byteLength": len(raw),
                "sha256": digest,
                "eof": eof,
                "data": base64.b64encode(chunk).decode(),
            }
            for offset, chunk, eof in parts
        ]
        call = AsyncMock(side_effect=[replies[0], TimeoutError(), replies[1], TimeoutError(), {"released": True}])
        source = host_bytes.HostBytes(SimpleNamespace(client_request=call))
        self.assertEqual(await source.acquire("s", "b", "g", len(raw), current=lambda: True), (raw, digest))
        sent = [entry.args[1] for entry in call.await_args_list]
        self.assertEqual(sent[1], sent[2])
        self.assertEqual(sent[3], sent[4])
        self.assertEqual({item["readerId"] for item in sent}, {sent[0]["readerId"]})
        self.assertEqual([item["offset"] for item in sent], [0, 30, 30, len(raw), len(raw)])
        self.assertEqual(call.await_args.kwargs["max_response_bytes"], 192 * 1024)

    async def test_fence_after_lock_and_before_retry(self):
        current = True

        async def timed_out(*args, **kwargs):
            nonlocal current
            current = False
            raise TimeoutError()

        call = AsyncMock(side_effect=timed_out)
        source = host_bytes.HostBytes(SimpleNamespace(client_request=call))
        with self.assertRaisesRegex(host_bytes.InvalidGrant, "stale-turn"):
            await source.acquire("s", "b", "g", 100, current=lambda: current)
        self.assertEqual(call.await_count, 1)

        current = True
        async with source.read_lock:
            waiter = asyncio.create_task(source.acquire("s", "b", "g", 100, current=lambda: current))
            await asyncio.sleep(0)
            current = False
        with self.assertRaisesRegex(host_bytes.InvalidGrant, "stale-turn"):
            await waiter
        self.assertEqual(call.await_count, 1)

    async def test_fence_after_acp_writer_credit(self):
        writer = StdoutWriter.__new__(StdoutWriter)
        writer.credit = asyncio.Semaphore(0)
        writer.queue = SimpleNamespace(put_nowait=AsyncMock())
        writer.pending = set()

        def guard():
            raise host_bytes.InvalidGrant("stale-turn")

        send = asyncio.create_task(writer.write(b"pull", before_queue=guard))
        await asyncio.sleep(0)
        writer.credit.release()
        with self.assertRaisesRegex(host_bytes.InvalidGrant, "stale-turn"):
            await send
        self.assertFalse(writer.pending)
        self.assertFalse(writer.credit.locked())
        writer.queue.put_nowait.assert_not_called()

    async def test_bad_length_digest_eof_are_never_finished(self):
        raw = png()
        reply = {
            "offset": 0,
            "byteLength": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "eof": True,
            "data": base64.b64encode(raw).decode(),
        }
        for changes in (
            {"offset": 1},
            {"byteLength": len(raw) + 1},
            {"sha256": "0" * 64},
            {"eof": False},
            {"data": "!"},
            {"byteLength": True},
        ):
            call = AsyncMock(return_value={**reply, **changes})
            source = host_bytes.HostBytes(SimpleNamespace(client_request=call))
            with self.subTest(changes=changes), self.assertRaises(host_bytes.InvalidGrant):
                await source.acquire("s", "b", "g", len(raw) + 2, current=lambda: True)
            self.assertEqual(call.await_count, 1)


class ImageRelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Reuse the verified text fixture without inheriting its test cases.
        await text_relay.RelayTests.asyncSetUp(self)
        self.server.initialized = False
        offer = text_relay.initialize_offer()
        offer["clientCapabilities"]["_meta"][host_bytes.META_KEY] = {"version": 1}
        result = await self.server.dispatch("initialize", offer)
        self.assertEqual(
            result["agentCapabilities"]["_meta"][session_relay.META_KEY]["requests"],
            [*text_relay.AD["requests"], *session_relay.IMAGE_REQUESTS],
        )
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        (Path(temp.name) / "image-ready").touch()
        self.session.server = self.server
        self.session.id = "session-1"
        self.session.socket_dir = temp.name
        self.session.image_generation = "generation"
        self.session.image_epoch = self.session.image_turn_id = None
        self.session.aborts = 0
        self.session.state["model"] = {
            "provider": "fixture",
            "id": "one",
            "api": "openai-completions",
            "baseUrl": "https://fixture.invalid",
            "input": ["text", "image"],
        }
        self.server.image_policies = (
            {
                "route": ["fixture", "one", "openai-completions", "https://fixture.invalid"],
                "count": 20,
                "imageBytes": 3_750_000,
                "sidePixels": 8000,
                "rawBytes": 8 << 20,
                "bodyBytes": 12 << 20,
                "contextTokens": 196608,
                "imageTokens": 8192,
                "outputTokens": 4096,
            },
        )
        raw = png()
        self.server.host_bytes.acquire = AsyncMock(return_value=(raw, hashlib.sha256(raw).hexdigest()))

        async def rpc(method, **kwargs):
            if method == "get_state":
                return {"isStreaming": True, "model": self.session.state["model"]}
            if hooks := kwargs.get("_image_write"):
                current, issued = hooks
                self.assertTrue(current())
                issued()
            return {}

        self.session.rpc = AsyncMock(side_effect=rpc)

    params = text_relay.RelayTests.params
    request = text_relay.RelayTests.request
    input = text_relay.RelayTests.input
    assert_rpc_error = text_relay.RelayTests.assert_rpc_error

    async def submit(self, number=1, **changes):
        return await self.request(
            "_aelix/session/input/steer-images",
            {**self.input(number, message=""), "grants": ["file-1"], **changes},
        )

    async def test_caption_pixels_and_replay_do_not_read_again(self):
        receipt = await self.submit()
        self.assertEqual(
            receipt,
            {
                "accepted": True,
                "delivery": "native-queue",
                "turnId": self.turn_id,
                "inputKind": "pixels",
                "captionAdded": True,
            },
        )
        self.assertEqual(self.session.rpc.await_args.kwargs["message"], image_policy.CAPTION)
        self.assertEqual(await self.submit(), receipt)
        self.server.host_bytes.acquire.assert_awaited_once()
        self.assertIsNotNone(next(iter(self.relay.receipts.values())).content_fingerprint)
        lookup = await self.request(session_relay.LOOKUP_METHOD, {"requestId": self.input()["requestId"]})
        self.assertEqual(lookup, {"status": "completed", "result": receipt})
        with self.assertRaisesRegex(ProtocolError, "Conflicting requestId"):
            await self.submit(grants=["different"])
        await self.assert_rpc_error(
            "_aelix/session/input/follow-up-images", {**self.input(message=""), "grants": ["file-1"]}, -32602
        )
        self.assertFalse((await self.submit(2, message="  text  "))["captionAdded"])
        self.assertEqual(self.session.rpc.await_args.kwargs["message"], "  text  ")

    async def test_stop_and_model_change_between_read_and_write(self):
        delegate = self.server.host_bytes.acquire.side_effect

        async def stopped(*args, **kwargs):
            self.session.cancelled = True
            return png(), "0" * 64

        self.server.host_bytes.acquire.side_effect = stopped
        self.assertEqual(await self.submit(), {"accepted": False, "reason": "stale-turn"})
        self.assertEqual([entry.args[0] for entry in self.session.rpc.await_args_list], ["get_state"])
        self.server.host_bytes.acquire.side_effect = delegate
        self.session.cancelled = False
        self.session.state["model"] = {**self.session.state["model"], "baseUrl": "https://other.invalid"}
        self.assertEqual(await self.submit(2), {"accepted": False, "reason": "model-unverified"})

    async def test_lost_native_reply_preserves_original_unknown(self):
        default = self.session.rpc.side_effect

        async def broken(command, **kwargs):
            if command == "get_state":
                return await default(command, **kwargs)
            kwargs["_image_write"][1]()
            raise BrokenPipeError()

        self.session.rpc.side_effect = broken
        for _ in range(2):
            with self.assertRaises(ProtocolError) as error:
                await self.submit()
            self.assertEqual(error.exception.code, session_relay.OUTCOME_UNKNOWN)
        self.server.host_bytes.acquire.assert_awaited_once()
        self.assertEqual(
            await self.request(session_relay.LOOKUP_METHOD, {"requestId": self.input()["requestId"]}),
            {"status": "unknown"},
        )

    async def test_cancel_after_image_write_retains_unknown_at_original_key(self):
        default = self.session.rpc.side_effect
        written = asyncio.Event()
        writes = []

        async def stall_after_write(command, **kwargs):
            if command == "get_state":
                return await default(command, **kwargs)
            kwargs["_image_write"][1]()
            writes.append(command)
            written.set()
            await asyncio.Future()

        self.session.rpc.side_effect = stall_after_write
        request = asyncio.create_task(self.submit())
        original = {"requestId": self.input()["requestId"]}
        try:
            await asyncio.wait_for(written.wait(), 5)
            self.assertEqual(await self.request(session_relay.LOOKUP_METHOD, original), {"status": "pending"})
        finally:
            request.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await request
        self.assertEqual(await self.request(session_relay.LOOKUP_METHOD, original), {"status": "unknown"})
        self.assertEqual(writes, ["steer"])
        self.server.host_bytes.acquire.assert_awaited_once()

    async def test_timeout_after_write_does_not_reissue(self):
        default = self.session.rpc.side_effect

        async def stalled(command, **kwargs):
            if command == "get_state":
                return await default(command, **kwargs)
            kwargs["_image_write"][1]()
            await asyncio.Event().wait()

        self.session.rpc.side_effect = stalled
        with patch.object(session_relay, "IMAGE_REQUEST_SECONDS", 0.01):
            for _ in range(2):
                with self.assertRaises(ProtocolError) as error:
                    await self.submit()
                self.assertEqual(error.exception.code, session_relay.OUTCOME_UNKNOWN)
        self.server.host_bytes.acquire.assert_awaited_once()
        self.assertEqual(
            await self.request(session_relay.LOOKUP_METHOD, {"requestId": self.input()["requestId"]}),
            {"status": "unknown"},
        )

    async def test_malformed_jpeg_sos_never_writes_a_queue(self):
        for index, raw in enumerate(malformed_jpegs(), 1):
            self.server.host_bytes.acquire.return_value = raw, hashlib.sha256(raw).hexdigest()
            self.assertEqual(await self.submit(index), {"accepted": False, "reason": "invalid-image-header"})
        self.session.rpc.assert_not_awaited()

    async def test_order_shared_by_text_and_images_per_mode(self):
        original_rpc = self.session.rpc.side_effect
        original_grant = self.server.host_bytes.acquire.return_value
        for index, mode in enumerate(("steer", "follow-up"), 1):
            with self.subTest(mode=mode):
                accepting, release, written, ack = (asyncio.Event() for _ in range(4))
                writes = []

                async def acquire(*args, accepting=accepting, release=release, **kwargs):
                    accepting.set()
                    await release.wait()
                    return original_grant

                async def rpc(method, *, writes=writes, written=written, ack=ack, **kwargs):
                    result = await original_rpc(method, **kwargs)
                    if method != "get_state":
                        writes.append(kwargs["message"])
                        if "_image_write" in kwargs:
                            written.set()
                            await ack.wait()
                    return result

                self.session.rpc.side_effect = rpc
                self.server.host_bytes.acquire.side_effect = acquire
                prefix = "_aelix/session/input/"
                image = asyncio.create_task(
                    self.request(
                        prefix + mode + "-images",
                        {**self.input(index * 10, message="first image"), "grants": ["original"]},
                    )
                )
                await accepting.wait()
                text = asyncio.create_task(
                    self.request(prefix + mode, self.input(index * 10 + 1, message="next text"))
                )
                other = "follow-up" if mode == "steer" else "steer"
                self.assertTrue(
                    (await self.request(prefix + other, self.input(index * 10 + 2, message="other mode")))[
                        "accepted"
                    ]
                )
                self.assertEqual(writes, ["other mode"])
                release.set()
                await written.wait()
                self.assertTrue((await asyncio.wait_for(text, 1))["accepted"])
                self.assertFalse(image.done())  # Wire order is sufficient, even before the first ACK.
                ack.set()
                self.assertTrue((await image)["accepted"])
                self.assertEqual(writes, ["other mode", "first image", "next text"])

    async def test_fence_cancels_first_middle_finish_and_cleans_acp_buffers(self):
        class Buffer(bytearray):
            pass

        raw = png()
        for number, boundary in enumerate(("first", "middle", "finish"), 1):
            old_relay = self.session.input_relay = self.relay
            self.session.invalidate_image_acceptance = Session.invalidate_image_acceptance.__get__(self.session)
            self.session.cancelled = False
            old_relay.turn_id = self.turn_id
            entered = asyncio.Event()
            seen, buffers = [], []

            def alloc(buffers=buffers):
                body = Buffer()
                buffers.append(weakref.ref(body))
                return body

            async def reverse(method, params, *, seen=seen, boundary=boundary, entered=entered, **kwargs):
                offset = params["offset"]
                seen.append((offset, params.get("finish") is True))
                if {"first": offset == 0, "middle": offset == 30, "finish": params.get("finish") is True}[
                    boundary
                ]:
                    entered.set()
                    await asyncio.Event().wait()
                end = min(offset + 30, len(raw))
                return {
                    "offset": offset,
                    "byteLength": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "eof": end == len(raw),
                    "data": base64.b64encode(raw[offset:end]).decode(),
                }

            self.server.host_bytes = host_bytes.HostBytes(SimpleNamespace(client_request=reverse))
            with patch.object(host_bytes, "bytearray", alloc, create=True):
                pending = asyncio.create_task(self.submit(number))
                await asyncio.wait_for(entered.wait(), 1)
                self.assertIsNotNone(buffers[0]())
                if boundary == "first":
                    self.session.void_questions = lambda predicate: None
                    self.session.context = SimpleNamespace(manual_compaction=False)
                    self.session.tool_arrived = asyncio.Event()
                    self.session.management_task = None
                    self.session.permission_connections = self.session.permissions = self.session.ui_tasks = set()
                    self.session.controls = SimpleNamespace(capabilities={})
                    self.session.abort = AsyncMock()
                    self.session.cancel_generation = 0
                    self.session.begin_cancel = Session.begin_cancel.__get__(self.session)
                    self.session.cancel_native = Session.cancel_native.__get__(self.session)
                    await Session.cancel(self.session)
                elif boundary == "middle":
                    self.session.image_generation = "replacement"
                    self.session.interventions = {}
                    Session.raise_fence(self.session)
                else:
                    session_relay.bind_session(self.session, {}, "binding-2")
                self.assertEqual(await asyncio.wait_for(pending, 1), {"accepted": False, "reason": "stale-turn"})
            self.assertEqual(len(seen), number)
            self.assertIsNone(buffers[0]())
            self.assertFalse(old_relay.acquiring_images)
            self.assertFalse(old_relay.image_acceptances)
            self.assertFalse(self.server.host_bytes.read_lock.locked())
            self.session.changing = 0
        await self.assert_rpc_error(
            session_relay.LOOKUP_METHOD, {"requestId": self.input(3)["requestId"]}, session_relay.SCOPE_DENIED
        )
        self.session.rpc.assert_not_awaited()

    async def test_default_policy_allows_only_the_verified_codex_route(self):
        default = Server(SimpleNamespace(state_dir=self.session.socket_dir))
        self.addCleanup(default.db.close)
        self.server.image_policies = default.image_policies
        route = dict(zip(("provider", "id", "api", "baseUrl"), image_policy.VERIFIED_IMAGE_POLICIES[0]["route"]))
        model = {**route, "input": ["text", "image"]}
        for index, (field, value) in enumerate(
            (
                ("provider", "openai"),
                ("id", "gpt-6-terra"),
                ("api", "openai-responses"),
                ("baseUrl", "https://example.invalid/backend-api"),
                ("baseUrl", route["baseUrl"] + "/codex"),
            ),
            1,
        ):
            self.session.state["model"] = {**model, field: value}
            self.assertEqual(await self.submit(index), {"accepted": False, "reason": "model-unverified"})
        self.server.host_bytes.acquire.assert_not_awaited()
        self.session.rpc.assert_not_awaited()
        self.session.state["model"] = model
        self.assertTrue((await self.submit(6))["accepted"])

    async def test_image_methods_require_a_host_offer_even_with_a_binding(self):
        self.server.host_bytes = None
        with self.assertRaises(ProtocolError) as error:
            await self.submit()
        self.assertEqual(error.exception.code, -32601)


class HostFixtureTests(unittest.TestCase):
    def test_tool_filename_is_portable_and_wire_markers_still_escape(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "test_running_images_host.py"
        fixture = runpy.run_path(str(script))
        markers = fixture["STANDARD_MARKERS"]
        marker_variants = fixture["marker_variants"]

        filename = markers["tool"] + ".txt"
        self.assertNotRegex(filename, r'[<>:"/\\|?*]')
        if hasattr(ntpath, "isreserved"):
            self.assertFalse(ntpath.isreserved(filename))
        for kind in ("assistant", "rpcError"):
            marker = markers[kind]
            self.assertIn('"', marker)
            self.assertIn("\\", marker)
            escaped = json.dumps(marker, ensure_ascii=False)[1:-1].encode()
            self.assertIn(escaped, marker_variants(marker))
            self.assertNotEqual(escaped, marker.encode())

    def test_codex_smoke_counts_unmatched_images_and_duplicate_digests(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "test_running_images_codex.py"
        smoke = runpy.run_path(str(script))
        verify = smoke["verify_image_requests"]
        error = smoke["ProbeFailed"]
        text = {"stage": "wire", "inputImageCount": 0, "images": []}
        known = {"stage": "wire", "inputImageCount": 1, "images": ["steer"]}
        unknown = {"stage": "wire", "inputImageCount": 1, "images": []}
        self.assertEqual(verify([text]), [])
        self.assertEqual(verify([text, known], "steer"), [known])
        with self.assertRaises(error):
            verify([text, unknown])
        for unexpected in (unknown, {**known, "inputImageCount": 2}, {**known, "images": ["follow"]}):
            with self.subTest(unexpected=unexpected), self.assertRaises(error):
                verify([known, unexpected], "steer")


class NativeFrameTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        server = Server(SimpleNamespace(state_dir=temporary.name))
        self.addCleanup(server.db.close)
        self.worker = Session(server, "native-frame-tests", temporary.name)

    async def assert_oversized_tools(self, body, cases):
        # Feed an actual subprocess pipe at the deployed reader limit. Build
        # the synthetic input outside the timing window.
        self.assertGreater(len(body), native_frames.FRAME_LIMIT)
        with TemporaryFile() as stream:
            stream.write(b'{"messages":["ignored"],"type":"agent_end"}\n')
            for index, (prefix, suffix, call_id, _) in enumerate(cases):
                stream.write(prefix)
                stream.write(body)
                stream.write(suffix + b"\n")
                reply = {
                    "type": "response",
                    "command": "steer",
                    "success": True,
                    "id": f"{index:032x}",
                    "data": {"acked": call_id},
                }
                stream.write(json.dumps(reply).encode() + b"\n")
            stream.seek(0)
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                "import shutil, sys; shutil.copyfileobj(sys.stdin.buffer, sys.stdout.buffer)",
                stdin=stream,
                stdout=asyncio.subprocess.PIPE,
                limit=65536,
            )

        loop = asyncio.get_running_loop()
        pending = {f"{index:032x}": loop.create_future() for index in range(len(cases))}
        worker = self.worker
        worker.proc = process
        worker.private_host = True
        worker.pending = pending
        worker._close_task = True
        worker.event = AsyncMock()
        # Each timer is queued before a scan. Measure how many slices can
        # run before the loop services it, as well as its elapsed delay.
        ticks = []
        slices = largest_slice = 0
        timer_idle = asyncio.Event()
        timer_idle.set()
        feed = native_frames.ToolEnvelope.feed

        def tick(started):
            nonlocal slices
            ticks.append((loop.time() - started, slices))
            slices = 0
            timer_idle.set()

        def observe_scan(envelope, data):
            nonlocal largest_slice, slices
            if timer_idle.is_set():
                timer_idle.clear()
                loop.call_later(0, tick, loop.time())
            slices += 1
            largest_slice = max(largest_slice, len(data))
            feed(envelope, data)

        try:
            async with asyncio.timeout(60):
                with patch.object(native_frames.ToolEnvelope, "feed", observe_scan):
                    await worker.read_rpc()
                self.assertEqual(await process.wait(), 0)
                await timer_idle.wait()
        finally:
            if process.returncode is None:
                process.kill()
                await process.stdout.read()
                await process.wait()

        self.assertIsNone(worker.failure)
        self.assertEqual(
            [(entry.args[0]["toolCallId"], entry.args[0]["isError"]) for entry in worker.event.await_args_list],
            [(call_id, failed) for _, _, call_id, failed in cases],
        )
        self.assertEqual(
            [future.result() for future in pending.values()], [{"acked": call_id} for _, _, call_id, _ in cases]
        )
        self.assertTrue(ticks)
        self.assertLessEqual(largest_slice, 8 * 1024)
        # A ready continuation may run just ahead of the due timer.
        self.assertLessEqual(max(count for _, count in ticks), 2)
        # Check elapsed time without treating a rare VM scheduling pause as
        # a stall in the scanner. The slice checks cover every callback.
        elapsed = sorted(gap for gap, _ in ticks)
        p95 = elapsed[(95 * len(elapsed) + 99) // 100 - 1]
        self.assertLess(
            p95, 0.030, f"Scanner timer gaps: p95 {p95 * 1000:.1f} ms, max {elapsed[-1] * 1000:.1f} ms"
        )

    async def test_oversized_tool_string_status_ack_and_responsiveness(self):
        body = b'"' + b"x" * (native_frames.FRAME_LIMIT + 8192) + b'"'
        prefix = b'{"tool_call_id":"good","result":{"content":['
        await self.assert_oversized_tools(
            body,
            (
                (
                    prefix,
                    b'],"details":{"isError":true},"is_error":false},"is_error":false,"type":"tool_execution_end"}',
                    "good",
                    False,
                ),
                (
                    prefix.replace(b'"good"', b'"outer"'),
                    b'],"is_error":false},"is_error":true,"type":"tool_execution_end"}',
                    "outer",
                    True,
                ),
            ),
        )

    async def test_oversized_tool_array_status_ack_and_responsiveness(self):
        record = (
            json.dumps(
                {"nested": [{"isError": False}, {"escaped": '\\"', "items": [1, 2, 3], "text": "x" * 256}]}
            ).encode()
            + b","
        )
        body = b"[" + record * (native_frames.FRAME_LIMIT // len(record) + 1024) + b"{}]"
        await self.assert_oversized_tools(
            body,
            (
                (
                    b'{"type":"tool_execution_end","toolCallId":"array","result":{"details":',
                    b',"isError":true},"isError":false}',
                    "array",
                    True,
                ),
            ),
        )

    async def test_serialized_native_line_check_uses_exact_utf8_bytes(self):
        written = []

        def write(raw):
            written.append(raw)
            worker.pending[json.loads(raw)["id"]].set_result({})

        pipe = SimpleNamespace(write=write, drain=AsyncMock())
        worker = self.worker
        worker.proc = SimpleNamespace(stdin=pipe, returncode=None)
        handlers = (lambda: True, lambda: None)
        message = '한\n"\\'
        await worker.rpc("steer", message=message, _image_write=handlers)
        actual = written.pop()
        with patch.object(image_policy, "MAX_NATIVE_LINE", len(actual)):
            await worker.rpc("steer", message=message, _image_write=handlers)
        written.clear()
        with (
            patch.object(image_policy, "MAX_NATIVE_LINE", len(actual) - 1),
            self.assertRaisesRegex(image_policy.ImageRejected, "native-line-limit"),
        ):
            await worker.rpc("steer", message=message, _image_write=handlers)
        with self.assertRaisesRegex(image_policy.ImageRejected, "stale-turn"):
            await worker.rpc("steer", message=message, _image_write=(lambda: False, lambda: None))
        self.assertEqual(written, [])
