"""H1 registration, binding, and request receipt tests."""
import asyncio
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from aelix_acp.runtime import session_relay
from test_studio_resources import CASES
from aelix_acp.server import Server
from aelix_acp.wire.errors import ProtocolError
from aelix_acp.wire.transport import encode_frame

AD = {
    "version": 1,
    "requests": [
        "_aelix/session/input/state",
        "_aelix/session/input/steer",
        "_aelix/session/input/follow-up",
        "_aelix/session/requests/lookup",
    ],
    "notifications": [],
}
META = "aelix/sessionRelay"
STATE, STEER, FOLLOW_UP, LOOKUP = AD["requests"]
AD["requests"] += [name for name in CASES["relay-discovery"]["response"]["body"]["data"]["requests"]
                   if not name.startswith("_aelix/session/selections/") and name not in AD["requests"]]


def request_id(number):
    return f"01a1f392-16c0-7000-8000-{number:012x}"


def initialize_offer(notifications=False):
    return {"protocolVersion": 1, "clientCapabilities": {"_meta": {
        META: {"version": 1, "notifications": notifications},
    }}}


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.server = Server(SimpleNamespace(profile="coding", state_dir=temp.name))
        self.addCleanup(self.server.db.close)
        self.server.send = AsyncMock()
        self.session = SimpleNamespace(
            input_relay=None, cancelled=False, changing=0, rpc_closed=False,
            studio_resources=SimpleNamespace(uncertain=False),
            proc=SimpleNamespace(returncode=None), rpc=AsyncMock(return_value={}),
            state={"steeringMode": "one-at-a-time", "followUpMode": "all"},
        )
        self.now = 123.0
        self.relay = session_relay.SessionRelay(self.session, "binding-1", clock=lambda: self.now)
        self.session.input_relay = self.relay
        self.server.sessions["session-1"] = self.session
        self.relay.observe_prompt(True)
        self.turn_id = self.relay.turn_id
        self.initialized = await self.server.dispatch("initialize", initialize_offer())

    def params(self, payload, *, binding="binding-1", session="session-1"):
        return {"sessionId": session, "bindingId": binding, "payload": payload}

    def input(self, number=1, **changes):
        return {"requestId": request_id(number), "turnId": self.turn_id, "message": "두 파일", **changes}

    async def request(self, method, payload, **params):
        return await self.server.dispatch(method, self.params(payload, **params))

    async def assert_rpc_error(self, method, payload, code, **params):
        with self.assertRaises(ProtocolError) as failure:
            await self.request(method, payload, **params)
        self.assertEqual(failure.exception.code, code)
        return failure.exception

    async def test_registration_snapshot_for_h1_and_h2(self):
        self.assertEqual(self.initialized["agentCapabilities"]["_meta"][META], AD)
        self.assertEqual(list(session_relay.REQUESTS), AD["requests"])
        self.assertEqual(session_relay.advertisement(initialize_offer(True)["clientCapabilities"]), AD)
        for invalid in (None, {}, {"version": 2, "notifications": False},
                        {"version": True, "notifications": False}, {"version": 1, "notifications": 0}):
            self.assertIsNone(session_relay.advertisement({"_meta": {META: invalid}}))

    async def test_directions_schema_and_binding_fail_closed(self):
        for suffix in ("dispatch", "rpc", "manage", "shell", "files/read", "input/follow_up", "children/steer"):
            await self.assert_rpc_error("_aelix/session/" + suffix, self.input(), -32601)
        await self.assert_rpc_error("_aelix/manage", {"action": "settings/set"}, -32602)
        await self.assert_rpc_error(STEER, self.input(), -33102, binding="binding-2")
        await self.assert_rpc_error(STEER, self.input(), -33102, session="different-session")
        self.server.starting_ids.add("session-1")
        await self.assert_rpc_error(STEER, self.input(), -33102)
        self.server.starting_ids.clear()
        await self.assert_rpc_error(STEER, self.input(command="bash"), -32602)
        await self.assert_rpc_error(FOLLOW_UP, self.input(images=[]), -32602)
        await self.assert_rpc_error(STEER, self.input(message="  "), -32602)
        await self.assert_rpc_error(LOOKUP, {"requestId": request_id(1), "value": True}, -32602)
        await self.assert_rpc_error(STEER, self.input(requestId="8e5b23fe-1744-4506-84cb-b6921fd4d054"), -32602)
        await self.server.handle({"jsonrpc": "2.0", "method": STEER, "params": self.params(self.input())})
        self.server.send.assert_not_awaited()
        self.server.relay_enabled = False
        await self.assert_rpc_error(STEER, self.input(), -32601)
        self.session.rpc.assert_not_awaited()
        self.assertFalse(self.relay.receipts)

    async def test_binding_echo_requires_a_supported_session(self):
        raw = {META: {"bindingId": "bind-new"}}
        binding = session_relay.open_binding(raw)
        response = {"sessionId": "session-1", "_meta": {"other": 42}}
        self.assertEqual(session_relay.bind_session(self.session, response, binding)["_meta"],
                         {**raw, "other": 42})
        self.session.state.clear()
        self.session.input_relay = None
        self.assertEqual(session_relay.bind_session(self.session, {}, binding), {})
        self.assertIsNone(self.session.input_relay)
        self.assertIsNone(session_relay.open_binding(None))
        with self.assertRaises(ProtocolError):
            session_relay.open_binding({META: {"bindingId": "bind-new", "owner": "other"}})

    async def test_concurrent_dedup_and_turn_scope(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def native(*args, **kwargs):
            entered.set()
            await release.wait()

        self.session.rpc.side_effect = native
        self.assertEqual(await self.request(STATE, {}), {"active": True, "turnId": self.turn_id})
        self.assertEqual(await self.request(LOOKUP, {"requestId": request_id(1)}), {"status": "unavailable"})
        first = asyncio.create_task(self.request(STEER, self.input()))
        await entered.wait()
        duplicate = asyncio.create_task(self.request(STEER, self.input()))
        await asyncio.sleep(0)
        self.assertEqual(await self.request(LOOKUP, {"requestId": request_id(1)}), {"status": "pending"})
        self.assertEqual(str(await self.assert_rpc_error(FOLLOW_UP, self.input(), -32602)), "Conflicting requestId.")
        await self.assert_rpc_error(STEER, self.input(message="changed"), -32602)
        self.relay.observe_prompt(False)
        release.set()
        result = {"turnId": self.turn_id, "accepted": True, "delivery": "native-queue"}
        self.assertEqual(await first, result)
        self.assertEqual(await duplicate, result)
        self.assertEqual(await self.request(STATE, {}), {"active": False, "turnId": None})
        self.assertEqual(await self.request(STEER, self.input()), result)
        self.assertEqual(await self.request(LOOKUP, {"requestId": request_id(1)}),
                         {"status": "completed", "result": result})
        self.assertEqual(await self.request(STEER, self.input(2)), {"accepted": False, "reason": "stale-turn"})
        self.session.rpc.assert_awaited_once_with("steer", message="두 파일", _timeout=None)

    async def test_outcome_unknown_is_retained_without_retry(self):
        self.session.rpc.side_effect = ProtocolError("internal raw native text")
        error = await self.assert_rpc_error(FOLLOW_UP, self.input(), -33100)
        self.assertEqual(str(error), "Native queue delivery is unknown.")
        self.assertEqual(await self.request(LOOKUP, {"requestId": request_id(1)}), {"status": "unknown"})
        await self.assert_rpc_error(FOLLOW_UP, self.input(), -33100)
        self.session.rpc.assert_awaited_once_with("follow_up", message="두 파일", _timeout=None)

    async def test_capacity_and_monotonic_expiry_keep_pending_slots(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def native(*args, **kwargs):
            entered.set()
            await release.wait()

        self.session.rpc.side_effect = native
        first = asyncio.create_task(self.request(STEER, self.input(0)))
        await entered.wait()
        self.relay.observe_prompt(False)
        for number in range(1, session_relay.MAX_RECEIPTS):
            self.assertEqual(await self.request(STEER, self.input(number)),
                             {"accepted": False, "reason": "stale-turn"})
        clock_key = {"requestId": request_id(1)}
        self.now += 599.999
        self.assertEqual((await self.request(LOOKUP, clock_key))["status"], "completed")
        self.assertEqual(await self.request(STEER, self.input(256)),
                         {"accepted": False, "reason": "dedup-capacity"})
        self.now += 0.001
        self.assertEqual(await self.request(LOOKUP, clock_key), {"status": "unavailable"})
        self.assertEqual(await self.request(LOOKUP, {"requestId": request_id(0)}), {"status": "unavailable"})
        self.assertEqual(len(self.relay.receipts), 256)
        self.assertEqual(await self.request(STEER, self.input(256)), {"accepted": False, "reason": "stale-turn"})
        self.assertEqual(len(self.relay.receipts), 2)  # Unsettled even though lookup expired.
        duplicate = asyncio.create_task(self.request(STEER, self.input(0)))
        await asyncio.sleep(0)
        self.assertFalse(duplicate.done())
        release.set()
        result = await first
        self.assertTrue(result["accepted"])
        self.assertEqual(await duplicate, result)
        self.session.rpc.assert_awaited_once()

    async def test_cancellation_of_duplicate_does_not_cancel_original(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def native(*args, **kwargs):
            entered.set()
            await release.wait()

        self.session.rpc.side_effect = native
        first = asyncio.create_task(self.request(STEER, self.input()))
        await entered.wait()
        duplicate = asyncio.create_task(self.request(STEER, self.input()))
        await asyncio.sleep(0)
        duplicate.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await duplicate
        self.assertEqual(await self.request(LOOKUP, {"requestId": request_id(1)}), {"status": "pending"})
        release.set()
        self.assertTrue((await first)["accepted"])
        self.session.rpc.assert_awaited_once()

    async def test_lookup_uses_actual_output_encoder_and_response_id(self):
        key = request_id(1)
        response_id = "recheck-검증"
        result = {"status": "completed", "result": {"accepted": True, "detail": ""}}
        message = {"jsonrpc": "2.0", "id": response_id, "method": LOOKUP, "params": self.params({"requestId": key})}
        base = len(encode_frame({"jsonrpc": "2.0", "id": response_id, "result": result})) - 1
        for delta in (-1, 0, 1):
            with self.subTest(delta=delta):
                result["result"]["detail"] = "x" * (session_relay.MAX_RELAY_FRAME - base + delta)
                outcome = asyncio.get_running_loop().create_future()
                outcome.set_result(result)
                self.relay.receipts[session_relay.request_key(key)] = session_relay.Receipt(self.now, b"", outcome)
                await self.server.handle(message)
                sent = self.server.send.await_args.args[0]
                self.assertEqual(sent["id"], response_id)
                if delta <= 0:
                    self.assertEqual(len(encode_frame(sent)) - 1, session_relay.MAX_RELAY_FRAME + delta)
                    self.assertEqual(sent["result"], result)
                else:
                    self.assertEqual(sent["result"], {"status": "unavailable", "reason": "result-too-large"})
                self.assertEqual(await self.request(LOOKUP, {"requestId": key}), result)
        self.session.rpc.assert_not_awaited()
