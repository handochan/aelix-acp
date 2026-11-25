"""D16 interim compatibility, catalog and restore checks against the pinned native executable."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from support import AcpClient
from test_session_relay import initialize_offer, META
from test_studio_resources import CASES, QUIET

EXECUTABLE = os.environ.get("AELIX_ACP_TEST_AELIX")
OBSERVE_NATIVE = """
import json
from pathlib import Path
from aelix_acp.server import main
from aelix_acp.session import Session

rpc = Session.rpc
async def trace(session, command, **kwargs):
    name = kwargs.get("method", command)
    with Path("native-calls.jsonl").open("a", encoding="utf-8") as log:
        log.write(json.dumps(name) + "\\n")
    return await rpc(session, command, **kwargs)

Session.rpc = trace
main()
"""


@unittest.skipUnless(EXECUTABLE, "Set AELIX_ACP_TEST_AELIX to the pinned native executable")
class NativeProfileReselectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="acp-profile-", dir="/tmp" if os.name != "nt" else None)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / "home").mkdir()
        self.profile = self.root / "agent/agents/quiet.md"
        self.profile.parent.mkdir(parents=True)
        raw = CASES["profile-ref"]["vectors"][0]["rawUtf8"].encode().replace(b"\n", b"\r\n")
        self.profile.write_bytes(raw)
        identity_bytes = ("studio-profile/1\0user\0" + self.profile.as_uri() + "\0").encode() + raw
        self.ref = {**QUIET["ref"], "revision": "sha256:" + hashlib.sha256(identity_bytes).hexdigest()}
        self.client = await AcpClient.start(self.root, EXECUTABLE, entry=("-c", OBSERVE_NATIVE),
                                            capabilities=initialize_offer()["clientCapabilities"])
        self.addAsyncCleanup(self.client.close)
        self.binding = "b-21"
        self.session = (await self.client.request("session/new", self.open_params()))["sessionId"]

    def open_params(self):
        return {"cwd": str(self.root), "mcpServers": [], "_meta": {META: {"bindingId": self.binding}}}

    async def relay(self, case, **changes):
        post = CASES[case]["post"]
        return await self.client.request(post["method"], {
            "sessionId": self.session, "bindingId": self.binding, "payload": {**post["payload"], **changes},
        })

    def preferences(self, update=None):
        with closing(sqlite3.connect(self.root / "state/sessions.sqlite3")) as database, database:
            stored = json.loads(database.execute("SELECT value FROM preferences WHERE session_id=?",
                                                 (self.session,)).fetchone()[0])
            if update:
                stored.update(update)
                database.execute("UPDATE preferences SET value=? WHERE session_id=?",
                                 (json.dumps(stored), self.session))
            return stored

    def calls(self):
        return [json.loads(line) for line in (self.root / "native-calls.jsonl").read_text().splitlines()]

    async def test_native_catalog_legacy_manage_and_interim_explicit_selection(self):
        shown = await self.relay("profiles-show")
        self.assertEqual(shown, {"profile": {**QUIET, "ref": self.ref}})
        catalog = await self.relay("relay-error-large-item")
        self.assertIn({"ref": self.ref, "description": QUIET["description"]}, catalog["profiles"])
        self.assertIsNone(catalog["activeRef"])
        capabilities = await self.client.request("_aelix/capabilities", {"sessionId": self.session})
        self.assertNotIn("profileRefGuard", capabilities["runtime"])
        await self.client.request("_aelix/manage", {"sessionId": self.session, "action": "agents/use",
                                                    "arguments": {"name": "quiet"}})
        self.assertEqual(self.preferences()["agent"], "quiet")
        self.assertNotIn("agentRef", self.preferences())
        await self.client.request("session/prompt", {"sessionId": self.session,
                                                     "prompt": [{"type": "text", "text": "/agents use quiet"}]})
        self.assertNotIn("agentRef", self.preferences())
        expected = CASES["profiles-use"]["result"]
        self.assertEqual(await self.relay("profiles-use", profile=self.ref), {**expected, "activeRef": self.ref})
        self.assertEqual(self.preferences()["agentRef"], self.ref)
        self.assertEqual(self.calls().count("agents/use"), 3)
        self.assertNotIn("prompt", self.calls())

    async def test_native_empty_tools_and_legacy_load_remain_supported(self):
        self.assertEqual(await self.relay("tools-set-empty"), CASES["tools-set-empty"]["result"])
        self.assertEqual(await self.relay("lookup-done"), CASES["lookup-done"]["result"])
        await self.client.request("session/close", {"sessionId": self.session})
        self.preferences({"agent": "quiet", "permission_mode": "yolo"})
        self.binding = "b-22"
        result = await self.client.request("session/load", {**self.open_params(), "sessionId": self.session})
        self.assertEqual(result["_meta"][META], CASES["load-legacy"]["loadSuccessFragment"]["result"]["_meta"][META])
        resources = await self.client.request("_aelix/manage", {"sessionId": self.session, "action": "resources/list"})
        self.assertEqual(resources["activeTools"], [])
        stored = self.preferences()
        self.assertEqual(stored["agent"], "quiet")
        self.assertEqual(stored["activeTools"], [])
        self.assertEqual(stored["permission_mode"], "yolo")
        self.assertNotIn("agentRef", stored)
        self.assertNotIn("agentPreview", stored)
        self.assertEqual(self.calls().count("agents/use"), 1)
        self.assertEqual(await self.relay("lookup-expired"), CASES["lookup-expired"]["result"])

    async def test_native_recorded_restore_uses_interim_name_apply(self):
        await self.client.request("session/close", {"sessionId": self.session})
        self.preferences({**CASES["load-drift-open"]["stored"], "agentRef": self.ref,
                          "agentPreview": {**QUIET, "ref": self.ref}})
        self.binding = "b-22"
        result = await self.client.request("session/load", {**self.open_params(), "sessionId": self.session})
        self.assertEqual(result["_meta"][META], {"bindingId": "b-22"})
        self.assertEqual(self.preferences()["agentRef"], self.ref)
        self.assertEqual(self.preferences()["activeTools"], [])
        self.assertEqual(self.calls().count("agents/use"), 1)
        self.assertNotIn("prompt", self.calls())
        self.assertFalse(self.client.requests)
