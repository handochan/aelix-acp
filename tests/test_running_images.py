"""Image negotiation, Host wire validation and native write boundaries."""

import asyncio
import base64
import hashlib
import json
import struct
import unittest
import zlib
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_session_relay as text_relay

from aelix_acp import image_policy
from aelix_acp.runtime import host_bytes, native_frames, session_relay
from aelix_acp.session import Session
from aelix_acp.wire.errors import ProtocolError


def png_chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


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
            current, issued = kwargs["_image_write"]
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

    async def test_image_methods_require_a_host_offer_even_with_a_binding(self):
        self.server.host_bytes = None
        with self.assertRaises(ProtocolError) as error:
            await self.submit()
        self.assertEqual(error.exception.code, -32601)


class NativeFrameTests(unittest.IsolatedAsyncioTestCase):
    async def test_oversized_events_drain_to_the_next_response(self):
        stream = asyncio.StreamReader()
        identity = b"0" * 32
        reply = b'{"type":"response","command":"steer","success":true,"id":"' + identity + b'"}\n'
        stream.feed_data(
            b'{"messages":["' + b"x" * 130_000 + b'"],"type":"agent_end"}\n'
            b'{"tool_call_id":"tool","result":"' + b"x" * 130_000 + b'","type":"tool_execution_end"}\n' + reply
        )
        stream.feed_eof()
        with patch.object(native_frames, "FRAME_LIMIT", 1024):
            results = [frame async for frame in native_frames.read_frames(stream)]
        self.assertEqual(len(results), 2)
        self.assertEqual(native_frames.TOOL_ID.match(results[0][0])[1], b'"tool"')
        self.assertTrue(native_frames.TOOL_END.search(results[0][1]))
        self.assertEqual(results[1], (reply, None))

    async def test_serialized_native_line_check_uses_exact_utf8_bytes(self):
        written = []

        def write(raw):
            written.append(raw)
            worker.pending[json.loads(raw)["id"]].set_result({})

        pipe = SimpleNamespace(write=write, drain=AsyncMock())
        worker = SimpleNamespace(
            proc=SimpleNamespace(stdin=pipe, returncode=None),
            pending={},
            interventions={},
            rpc_closed=False,
            turn_active=False,
        )
        handlers = (lambda: True, lambda: None)
        message = '한\n"\\'
        await Session.rpc(worker, "steer", message=message, _image_write=handlers)
        actual = written.pop()
        with patch.object(image_policy, "MAX_NATIVE_LINE", len(actual)):
            await Session.rpc(worker, "steer", message=message, _image_write=handlers)
        written.clear()
        with (
            patch.object(image_policy, "MAX_NATIVE_LINE", len(actual) - 1),
            self.assertRaisesRegex(image_policy.ImageRejected, "native-line-limit"),
        ):
            await Session.rpc(worker, "steer", message=message, _image_write=handlers)
        with self.assertRaisesRegex(image_policy.ImageRejected, "stale-turn"):
            await Session.rpc(worker, "steer", message=message, _image_write=(lambda: False, lambda: None))
        self.assertEqual(written, [])
