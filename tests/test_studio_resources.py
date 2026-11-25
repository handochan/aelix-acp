"""D16 v1 session resources and their shared H1 receipts."""
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from aelix_acp.runtime import session_relay
from aelix_acp.runtime.profile_refs import profile_ref
from aelix_acp.server import Server, Session
from aelix_acp.wire.errors import BackendError, ProtocolError

FIXTURE = Path(__file__).with_name("fixtures") / "studio-7-d16-v1.json"


def load_cases():
    cases = {}

    def expand(value):
        if isinstance(value, list):
            return [expand(item) for item in value]
        if isinstance(value, dict):
            if set(value) == {"$fixture"}:
                name, pointer = value["$fixture"].split("#", 1)
                target = cases[name]
                for piece in pointer.split("/")[1:]:
                    key = piece.replace("~1", "/").replace("~0", "~")
                    target = target[int(key) if isinstance(target, list) else key]
                return deepcopy(target)
            return {key: expand(item) for key, item in value.items()}
        return value

    for case in json.loads(FIXTURE.read_bytes()):
        cases[case["id"]] = expand(case)
    return cases


CASES = load_cases()
QUIET = CASES["profiles-show"]["result"]["profile"]
PROJECT = CASES["load-drift-allow"]["storedAfter"]["agentPreview"]
REFS = [vector["ref"] for vector in CASES["profile-ref"]["vectors"]]


class ResourceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.server = Server(SimpleNamespace(profile="coding", state_dir=temporary.name))
        self.addCleanup(self.server.db.close)
        self.server.send = AsyncMock()
        await self.server.dispatch("initialize", {"protocolVersion": 1, "clientCapabilities": {
            "_meta": {session_relay.META_KEY: {"version": 1, "notifications": False}},
        }})
        self.session = Session(self.server, "s-42", temporary.name)
        self.server.sessions[self.session.id] = self.session
        self.session.proc = SimpleNamespace(returncode=None)
        self.now = 100.0
        self.relay = session_relay.SessionRelay(self.session, "b-21", clock=lambda: self.now)
        self.session.input_relay = self.relay
        self.resources = self.session.studio_resources
        self.session.controls.capabilities = {
            **CASES["native-guard"]["capabilitiesFragment"], "activeAgent": None,
            "methods": ["agents/list", "agents/show", "agents/use", "tools/set_active", "resources/list"],
        }
        self.session.controls.refresh = AsyncMock()
        self.session.commands = AsyncMock()
        self.session.update = AsyncMock()
        self.session.save = AsyncMock()
        self.session.rpc = AsyncMock(side_effect=self.native)
        self.resources.inspect = AsyncMock(return_value=deepcopy(QUIET))
        self.session.active_tools = ["read"]

    async def native(self, command, *, method, params):
        self.assertEqual(command, "aelix_control")
        if method == "agents/use":
            self.session.controls.capabilities["activeAgent"] = None if params["name"] == "--none" else params["name"]
            return {"activeRef": params.get("expectedProfile"), "message": "Selected"}
        if method == "resources/list":
            return {"tools": [{"name": "read"}, {"name": "write"}]}
        if method == "tools/set_active":
            return {"activeTools": params["names"]}
        self.fail(method)

    async def call(self, example, *, payload=None, binding=None, wire=False):
        post = CASES[example]["post"]
        params = {"sessionId": self.session.id, "bindingId": binding or post["bindingId"],
                  "payload": post["payload"] if payload is None else payload}
        if wire:
            await self.server.handle({"jsonrpc": "2.0", "id": 7, "method": post["method"], "params": params})
            return self.server.send.call_args.args[0]
        return await self.server.dispatch(post["method"], params)

    async def assert_result(self, example, **kwargs):
        result = await self.call(example, **kwargs)
        self.assertEqual(result, CASES[example]["result"])

    async def test_fixture_hash_and_profile_ref_vectors(self):
        self.assertEqual(hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
                         "c5341c0d677faa21ba8294ec3350edabc3a15942889529df4e8c6b4a00f53627")
        self.assertEqual(len(CASES), 137)
        for vector in CASES["profile-ref"]["vectors"]:
            ref = vector["ref"]
            self.assertEqual(profile_ref(ref["name"], ref["scope"], vector["sourceUri"],
                                         vector["rawUtf8"].encode()), ref)

    async def test_show_guarded_use_and_clear(self):
        await self.assert_result("profiles-show")
        self.assertEqual(await self.call("profiles-use", wire=True), CASES["relay-envelope"]["acpResponse"])
        self.session.rpc.assert_awaited_once_with(
            "aelix_control", method="agents/use", params=CASES["native-guard"]["nativeRequest"]["params"])
        self.assertEqual(self.resources.preferences(), {
            "agent": "quiet", "agentRef": REFS[0], "agentPreview": QUIET,
        })
        self.assertIsNone(self.session.active_tools)
        self.session.save.assert_awaited_once_with(live=True)
        await self.assert_result("profiles-clear")
        self.assertEqual(self.resources.preferences(), {})
        self.assertIsNone(self.session.active_tools)
        self.session.rpc.assert_awaited_with("aelix_control", method="agents/use", params={"name": "--none"})

    async def test_invalid_guard_advertisements_never_apply(self):
        expected = CASES["native-guard"]["pinWithoutGuard"]["relayResult"]
        for number, version in enumerate((None, False, True, 2, "1", 1.0)):
            self.session.controls.capabilities["profileRefGuard"] = version
            payload = dict(CASES["profiles-use"]["post"]["payload"], requestId=f"019aa123-0001-7000-8000-{number:012x}")
            self.assertEqual(await self.call("profiles-use", payload=payload), expected)
        self.session.rpc.assert_not_awaited()
        self.session.save.assert_not_awaited()

    async def test_pages_snapshot_replay_binding_ttl_and_size(self):
        first, second = (deepcopy(CASES[f"profiles-list-page-{page}"]["result"]) for page in (1, 2))
        snapshot = {**first, "profiles": first["profiles"] + second["profiles"], "nextCursor": None}
        self.resources.catalog = AsyncMock(return_value=snapshot)
        result = await self.call("profiles-list-page-1")
        cursor = result["nextCursor"]
        self.assertTrue(cursor)
        self.assertEqual({**result, "nextCursor": first["nextCursor"]}, first)
        self.resources.catalog.return_value = {**snapshot, "profiles": []}
        for _ in range(2):
            await self.assert_result("profiles-list-page-2", payload={"cursor": cursor})
        self.resources.catalog.assert_awaited_once()
        other = session_relay.SessionRelay(self.session, "b-22")
        self.assertEqual(await other.profiles_list({"cursor": cursor}, response_id=7), CASES["relay-error-stale-cursor"]["result"])
        self.now += 60
        await self.assert_result("relay-error-stale-cursor", payload={"cursor": cursor})
        self.resources.catalog.return_value = {**snapshot, "profiles": [{"description": "한" * 90000}]}
        await self.assert_result("relay-error-large-item")

    async def test_page_invalidated_by_session_state_change(self):
        first = CASES["profiles-list-page-1"]["result"]
        self.resources.catalog = AsyncMock(return_value={
            **first, "profiles": first["profiles"] + CASES["profiles-list-page-2"]["result"]["profiles"],
        })
        cursor = (await self.call("profiles-list-page-1"))["nextCursor"]
        await self.assert_result("tools-set-active")
        await self.assert_result("relay-error-stale-cursor", payload={"cursor": cursor})

    async def test_same_request_concurrent_receipts_conflict_and_expiry(self):
        started, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def native(*args, **kwargs):
            nonlocal calls
            if kwargs["method"] == "tools/set_active":
                calls += 1
                started.set()
                await release.wait()
            return await self.native(*args, **kwargs)

        self.session.rpc.side_effect = native
        first = asyncio.create_task(self.call("tools-set-empty"))
        await started.wait()
        duplicate = asyncio.create_task(self.call("tools-set-empty"))
        try:
            await self.assert_result("lookup-pending")
            self.assertEqual(await self.call("relay-rpc-id-conflict", wire=True),
                             CASES["relay-rpc-id-conflict"]["acpError"])
            await self.assert_result("relay-error-busy")
        finally:
            release.set()
        results = await asyncio.gather(first, duplicate)
        spec = CASES["same-key-twice"]["expected"]
        self.assertEqual(calls, spec["nativeMutations"])
        self.assertEqual(results, [CASES["tools-set-empty"]["result"]] * spec["identicalResults"])
        await self.assert_result("lookup-done")
        self.now += session_relay.RECEIPT_TTL
        await self.assert_result("lookup-expired")

    async def test_receipts_for_preflight_error_and_unknown_commit(self):
        self.session.rpc.side_effect = ProtocolError("private read failure", -32602)
        with self.assertRaises(ProtocolError) as error:
            await self.call("tools-set-empty")
        self.assertEqual(error.exception.code, -32602)
        await self.assert_result("lookup-error")
        self.session.rpc.side_effect = self.native
        self.relay.receipts.clear()
        self.session.save.side_effect = OSError("database connection closed")
        for _ in range(2):
            with self.assertRaises(ProtocolError) as error:
                await self.call("tools-set-empty")
            self.assertEqual(error.exception.code, CASES["relay-rpc-outcome"]["acpError"]["error"]["code"])
        await self.assert_result("lookup-unknown")
        self.session.save.assert_awaited_once()
        await self.assert_result("relay-error-busy")

    async def test_validation_capacity_missing_and_locked(self):
        self.assertEqual(await self.call("relay-rpc-params", wire=True), CASES["relay-rpc-params"]["acpError"])
        self.assertEqual(await self.call("relay-rpc-method", wire=True), CASES["relay-rpc-method"]["acpError"])
        self.assertEqual(await self.call("tools-set-empty", wire=True, binding="b-retired"),
                         CASES["relay-rpc-binding"]["acpError"])
        for names in (None, ["read", "read"], ["read\0"]):
            with self.assertRaises(ProtocolError):
                await self.call("tools-set-empty", payload={"names": names, "requestId": CASES["lookup-done"]["post"]["payload"]["requestId"]})
        self.session.rpc.assert_not_awaited()
        with patch.object(session_relay, "MAX_RECEIPTS", 0):
            await self.assert_result("relay-error-dedup")
        await self.assert_result("relay-error-unavailable-tool")

        async def locked(*args, **kwargs):
            if kwargs["method"] == "tools/set_active":
                raise BackendError("Cannot turn on write: write is locked off for this run: fixture", "aelix_control")
            return await self.native(*args, **kwargs)

        self.session.rpc.side_effect = locked
        await self.assert_result("relay-error-locked-tool")
        self.session.save.assert_not_awaited()

    async def test_profile_read_and_native_refusal(self):
        self.resources.inspect.return_value = None
        await self.assert_result("relay-error-profile-unavailable")
        self.resources.inspect.return_value = QUIET
        self.session.rpc.side_effect = None
        self.session.rpc.return_value = CASES["relay-error-profile-refused"]["result"]
        await self.assert_result("relay-error-profile-refused")
        self.session.save.assert_not_awaited()

    async def test_unknown_native_refusal_cannot_become_tool_success(self):
        async def refused(*args, **kwargs):
            if kwargs["method"] == "tools/set_active":
                return {"accepted": False, "reason": "unrecognized", "activeTools": []}
            return await self.native(*args, **kwargs)

        self.session.rpc.side_effect = refused
        with self.assertRaises(ProtocolError) as failure:
            await self.call("tools-set-empty")
        self.assertEqual(failure.exception.code, -33100)
        self.session.save.assert_not_awaited()
        await self.assert_result("lookup-unknown")
