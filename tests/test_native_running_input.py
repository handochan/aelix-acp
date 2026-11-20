"""Stage-one receipts through the real native ACP stdio boundary."""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from support import AcpClient
from test_session_relay import FOLLOW_UP, LOOKUP, META, STATE, STEER, initialize_offer, request_id

EXECUTABLE = os.environ.get("AELIX_ACP_TEST_AELIX")
HOLD_AFTER_DRAIN = """
import asyncio
from pathlib import Path

def setup(api):
    async def hold(event, ctx):
        gate = Path(ctx.cwd, "hold-agent-end")
        if gate.is_file():
            Path(ctx.cwd, "last-drain-passed").touch()
            while gate.is_file():
                await asyncio.sleep(0.005)
    async def queue_update(event, ctx):
        if Path(ctx.cwd, "reject-queue-hook").is_file():
            raise RuntimeError("fixture callback failed after queueing")
    api.on("agent_end", hold)
    api.on("queue_update", queue_update)
"""


@unittest.skipUnless(EXECUTABLE, "Set AELIX_ACP_TEST_AELIX to the pinned native executable")
class NativeRunningInputTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="acp-relay-", dir="/tmp" if os.name != "nt" else None)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / "home").mkdir()
        (self.root / "agent").mkdir()
        self.model_requests = asyncio.Queue()
        self.gates = []
        self.listener = await asyncio.start_server(self.model, "127.0.0.1", 0)
        self.addAsyncCleanup(self.close_model)
        port = self.listener.sockets[0].getsockname()[1]
        model = {"id": "one", "name": "Fixture", "input": ["text"], "contextWindow": 32768, "maxTokens": 2048}
        (self.root / "agent/models.json").write_text(json.dumps({"providers": {"fixture": {
            "baseUrl": f"http://127.0.0.1:{port}/v1", "api": "openai-completions",
            "apiKey": "synthetic-fixture-key", "models": [model],
        }}}), encoding="utf-8")
        (self.root / "agent/settings.json").write_text(json.dumps({
            "defaultProvider": "fixture", "defaultModel": "one", "compaction": {"enabled": False},
        }), encoding="utf-8")
        extension = self.root / "hold.py"
        extension.write_text(HOLD_AFTER_DRAIN, encoding="utf-8")
        self.client = await AcpClient.start(self.root, EXECUTABLE, extensions=[extension],
                                            capabilities=initialize_offer()["clientCapabilities"])
        self.addAsyncCleanup(self.client.close)
        self.binding = "binding-first"
        created = await self.client.request("session/new", self.open_params(self.binding), timeout=120)
        self.assertEqual(created["_meta"][META], {"bindingId": self.binding})
        self.session_id = created["sessionId"]

    async def close_model(self):
        for gate in self.gates:
            gate.set()
        self.listener.close()
        await self.listener.wait_closed()

    async def model(self, reader, writer):
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            length = next(int(line.split(b":", 1)[1]) for line in header.splitlines()
                          if line.lower().startswith(b"content-length:"))
            request = json.loads(await reader.readexactly(length))
            users = [message["content"] for message in request["messages"] if message["role"] == "user"]
            gate = asyncio.Event()
            self.gates.append(gate)
            self.model_requests.put_nowait((users, gate))
            await gate.wait()

            def frame(delta, finish_reason):
                return "data: " + json.dumps({
                    "id": "fixture", "object": "chat.completion.chunk", "created": 0, "model": "one",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
                }) + "\n\n"

            body = (frame({"role": "assistant", "content": "native answer"}, None)
                    + frame({}, "stop") + "data: [DONE]\n\n").encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n"
                         + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    def open_params(self, binding):
        return {"cwd": str(self.root), "mcpServers": [], "_meta": {META: {"bindingId": binding}}}

    async def relay(self, method, payload, *, binding=None):
        return await self.client.request(method, {
            "sessionId": self.session_id, "bindingId": binding or self.binding, "payload": payload,
        })

    def prompt(self, text):
        return asyncio.create_task(self.client.request("session/prompt", {
            "sessionId": self.session_id, "prompt": [{"type": "text", "text": text}],
        }))

    async def active_turn(self):
        async with asyncio.timeout(10):
            while True:
                state = await self.relay(STATE, {})
                if state["active"]:
                    return state["turnId"]
                await asyncio.sleep(0.01)

    async def model_turn(self):
        return await asyncio.wait_for(self.model_requests.get(), 15)

    @staticmethod
    def input(turn_id, number, text):
        return {"turnId": turn_id, "requestId": request_id(number), "message": text}

    def user_chunks(self):
        return [frame["params"]["update"]["content"]["text"] for frame in self.client.updates
                if frame["method"] == "session/update"
                and frame["params"]["update"]["sessionUpdate"] == "user_message_chunk"]

    async def test_native_repeat_receipts_dedup_and_reopen(self):
        self.assertEqual(await self.relay(STATE, {}), {"active": False, "turnId": None})
        message = "same sentence"
        prompt = self.prompt(message)
        users, first_gate = await self.model_turn()
        self.assertEqual(users, [message])
        turn_id = await self.active_turn()
        steer = self.input(turn_id, 1, message)
        expected = {"turnId": turn_id, "accepted": True, "delivery": "native-queue"}
        first, duplicate = await asyncio.gather(self.relay(STEER, steer), self.relay(STEER, steer))
        self.assertEqual(first, expected)
        self.assertEqual(duplicate, expected)
        self.assertEqual(await self.relay(FOLLOW_UP, self.input(turn_id, 2, message)), expected)
        self.assertFalse(prompt.done())
        self.assertEqual(self.user_chunks(), [])
        first_gate.set()
        for count in (2, 3):
            users, gate = await self.model_turn()
            self.assertEqual(users, [message] * count)
            gate.set()
        self.assertEqual(await prompt, {"stopReason": "end_turn"})
        self.assertEqual(await self.relay(STATE, {}), {"active": False, "turnId": None})
        self.assertEqual(await self.relay(STEER, self.input(turn_id, 3, "stale")),
                         {"accepted": False, "reason": "stale-turn"})
        key = {"requestId": steer["requestId"]}
        self.assertEqual(await self.relay(LOOKUP, key), {"status": "completed", "result": expected})
        self.assertEqual(await self.relay(STEER, steer), expected)
        self.assertEqual(self.user_chunks(), [])
        loaded = await self.client.request("session/load", {
            **self.open_params("binding-second"), "sessionId": self.session_id,
        }, timeout=120)
        self.assertEqual(loaded["_meta"][META], {"bindingId": "binding-second"})
        self.assertEqual(self.user_chunks(), [message])
        with self.assertRaisesRegex(RuntimeError, '"code": -33102'):
            await self.relay(LOOKUP, key)
        self.binding = "binding-second"
        self.assertEqual(await self.relay(LOOKUP, key), {"status": "unavailable"})
        self.assertEqual(await self.relay(STATE, {}), {"active": False, "turnId": None})

    async def test_write_after_last_drain_receives_queue_receipt_only(self):
        hold = self.root / "hold-agent-end"
        hold.touch()
        self.addCleanup(hold.unlink, missing_ok=True)
        first = self.prompt("first prompt")
        _, gate = await self.model_turn()
        turn_id = await self.active_turn()
        gate.set()
        async with asyncio.timeout(10):
            while not (self.root / "last-drain-passed").is_file():
                await asyncio.sleep(0.01)
        self.assertEqual(await self.relay(STEER, self.input(turn_id, 1, "late queue input")),
                         {"turnId": turn_id, "accepted": True, "delivery": "native-queue"})
        hold.unlink()
        self.assertEqual(await first, {"stopReason": "end_turn"})
        self.assertEqual(await self.relay(STATE, {}), {"active": False, "turnId": None})
        self.assertTrue(self.model_requests.empty())
        second = self.prompt("explicit next prompt")
        users, gate = await self.model_turn()
        self.assertEqual(users, ["first prompt", "explicit next prompt", "late queue input"])
        second_turn = await self.active_turn()
        self.assertNotEqual(second_turn, turn_id)
        self.assertEqual(await self.relay(FOLLOW_UP, self.input(turn_id, 2, "old turn")),
                         {"accepted": False, "reason": "stale-turn"})
        gate.set()
        self.assertEqual(await second, {"stopReason": "end_turn"})
        self.assertEqual(self.user_chunks(), [])

    async def test_cancel_clears_undrained_input_and_preserves_receipt(self):
        prompt = self.prompt("prompt to cancel")
        _, cancelled_gate = await self.model_turn()
        turn_id = await self.active_turn()
        steer = self.input(turn_id, 1, "withdrawn queued input")
        receipt = await self.relay(STEER, steer)
        self.assertEqual(receipt, {"turnId": turn_id, "accepted": True, "delivery": "native-queue"})
        await self.client.request("session/cancel", {"sessionId": self.session_id})
        self.assertEqual(await prompt, {"stopReason": "cancelled"})
        cancelled_gate.set()
        self.assertEqual(await self.relay(STATE, {}), {"active": False, "turnId": None})
        self.assertEqual(await self.relay(LOOKUP, {"requestId": steer["requestId"]}),
                         {"status": "completed", "result": receipt})
        self.assertEqual(await self.relay(STEER, self.input(turn_id, 2, "stale after cancellation")),
                         {"accepted": False, "reason": "stale-turn"})
        self.assertTrue(self.model_requests.empty())
        next_prompt = self.prompt("prompt after cancellation")
        users, gate = await self.model_turn()
        self.assertEqual(users, ["prompt after cancellation"])
        gate.set()
        self.assertEqual(await next_prompt, {"stopReason": "end_turn"})
        self.assertEqual(self.user_chunks(), [])

    async def test_native_queue_hook_error_is_unknown_and_never_replayed(self):
        prompt = self.prompt("ordinary input")
        _, first_gate = await self.model_turn()
        turn_id = await self.active_turn()
        marker = self.root / "reject-queue-hook"
        marker.touch()
        steer = self.input(turn_id, 1, "input before callback error")
        with self.assertRaisesRegex(RuntimeError, '"code": -33100'):
            await self.relay(STEER, steer)
        marker.unlink()
        self.assertEqual(await self.relay(LOOKUP, {"requestId": steer["requestId"]}), {"status": "unknown"})
        with self.assertRaisesRegex(RuntimeError, '"code": -33100'):
            await self.relay(STEER, steer)
        first_gate.set()
        users, gate = await self.model_turn()
        self.assertEqual(users, ["ordinary input", "input before callback error"])
        gate.set()
        self.assertEqual(await prompt, {"stopReason": "end_turn"})
        self.assertTrue(self.model_requests.empty())
        self.assertEqual(self.user_chunks(), [])
