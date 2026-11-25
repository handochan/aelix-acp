"""D16 project selections against the pinned native worker."""
import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

from support import AcpClient
from test_session_relay import META, initialize_offer, request_id

EXECUTABLE = os.environ.get("AELIX_ACP_TEST_AELIX")
SHORT_APPROVAL = """
from aelix_acp.runtime import profile_reselection
from aelix_acp.server import main
profile_reselection.APPROVAL_SECONDS = 2
main()
"""


@unittest.skipUnless(EXECUTABLE, "Set AELIX_ACP_TEST_AELIX to the pinned native executable")
class NativeStudioSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="acp-selection-", dir="/tmp" if os.name != "nt" else None)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.agent = self.root / "agent"
        self.agent.mkdir()
        (self.root / "home").mkdir()
        self.project = self.root / ".aelix"
        self.global_settings = self.agent / "settings.json"
        self.global_settings.write_text('{"defaultTools":["-read"]}\n', encoding="utf-8")
        self.binding = "selection-first"

    async def start(self, *, expiry=False, trusted=False):
        kwargs = {"entry": ("-c", SHORT_APPROVAL)} if expiry else {}
        self.client = await AcpClient.start(
            self.root, EXECUTABLE, capabilities=initialize_offer()["clientCapabilities"],
            prefix=("--approve",) if trusted else (), **kwargs)
        self.addAsyncCleanup(self.client.close)
        created = await self.client.request("session/new", self.open_params(), timeout=120)
        self.session_id = created["sessionId"]

    def open_params(self):
        return {"cwd": str(self.root), "mcpServers": [], "_meta": {META: {"bindingId": self.binding}}}

    async def restart_worker(self):
        await self.client.request("session/close", {"sessionId": self.session_id})
        self.binding = "selection-restarted"
        await self.client.request("session/load", {**self.open_params(), "sessionId": self.session_id}, timeout=120)

    async def relay(self, suffix, payload):
        return await self.client.request("_aelix/session/" + suffix, {
            "sessionId": self.session_id, "bindingId": self.binding, "payload": payload,
        }, timeout=30)

    async def set_selection(self, kind, key, on, number, **kwargs):
        return await self.relay("selections/set", {"scope": "project", "kind": kind,
                                  "changes": [{"key": key, "on": on}], "requestId": request_id(number), **kwargs})

    async def listing(self, kind):
        return await self.relay("selections/list", {"kind": kind, "limit": 100})

    def permission_cards(self):
        return [request for request in self.client.requests if request["method"] == "session/request_permission"]

    def saved_preferences(self):
        with closing(sqlite3.connect(self.root / "state/sessions.sqlite3")) as db:
            row = db.execute("SELECT value FROM preferences WHERE session_id=?", (self.session_id,)).fetchone()
            return json.loads(row[0])

    async def test_refuse_then_approve_first_save_and_preserve_empty_override_across_restart(self):
        global_bytes = self.global_settings.read_bytes()
        await self.start(trusted=True)
        self.assertFalse((await self.listing("tool"))["scopes"]["project"]["exists"])
        await self.relay("tools/set-active", {"names": [], "requestId": request_id(1)})
        with self.assertRaisesRegex(RuntimeError, "-33102"):
            await self.set_selection("tool", "read", True, 2, scope="global")
        self.client.permission = "reject-once"
        refusal = await self.set_selection("tool", "read", True, 3)
        self.assertEqual(refusal, {"accepted": False, "reason": "consent-required", "cause": "declined",
                                   "scope": "project", "written": []})
        self.assertFalse((self.project / "settings.json").exists())
        self.client.permission = "allow-once"
        result = await self.set_selection("tool", "read", True, 4)
        self.assertTrue(result["accepted"], result)
        self.assertTrue(result["apply"]["applied"], result)
        self.assertEqual(result["written"], [".aelix/settings.json"])
        self.assertEqual(result["changed"], [{"key": "read", "saved": True}])
        self.assertEqual(len(self.permission_cards()), 2)
        self.assertEqual(self.global_settings.read_bytes(), global_bytes)
        self.assertEqual(self.saved_preferences()["activeTools"], [])
        for restart in (False, True):
            if restart:
                await self.restart_worker()
            listing = await self.listing("tool")
            selected = next(row for row in listing["items"] if row["key"] == "read")
            self.assertEqual((selected["saved"], selected["decidedBy"], selected["live"]), (True, "project", False))
            self.assertEqual((listing["activeTools"], listing["toolOverride"]), ([], []))

    async def test_real_native_confirmation_expires_without_project_file(self):
        global_bytes = self.global_settings.read_bytes()
        await self.start(expiry=True)
        seen = asyncio.Event()

        async def unanswered(_):
            seen.set()
            await asyncio.Future()

        self.client.permission = unanswered
        outcome = await self.set_selection("tool", "read", True, 5)
        self.assertTrue(seen.is_set())
        self.assertEqual(outcome, {"accepted": False, "reason": "consent-required", "cause": "unanswered",
                                   "scope": "project", "written": []})
        self.assertFalse((self.project / "settings.json").exists())
        self.assertEqual(self.global_settings.read_bytes(), global_bytes)
        self.assertEqual(len(self.permission_cards()), 1)

    async def test_skill_save_is_applied_without_restarting(self):
        skill = self.agent / "skills/review/SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: review\ndescription: Review changes\n---\nReview changes carefully.\n", encoding="utf-8")
        await self.start()
        before = await self.listing("skill")
        self.assertTrue(next(row for row in before["items"] if row["key"] == "review")["loaded"])
        result = await self.set_selection("skill", "review", False, 6)
        self.assertTrue(result["accepted"], result)
        self.assertEqual(result["effect"], "next_request")
        self.assertEqual(result["apply"]["turnedOff"], ["review"])
        self.assertTrue(result["apply"]["applied"])
        after = next(row for row in (await self.listing("skill"))["items"] if row["key"] == "review")
        self.assertEqual((after["saved"], after["applied"], after["live"], after["pending"]),
                         (False, False, None, False))

    async def test_mcp_project_file_takes_effect_on_worker_restart(self):
        script = self.root / "docs_server.py"
        script.write_text(
            'from mcp.server.fastmcp import FastMCP\ns = FastMCP("docs")\n@s.tool()\n'
            'def echo(value: str) -> str: return value\ns.run()\n', encoding="utf-8")
        entry = {"command": sys.executable, "args": [str(script)], "enabled": True}
        global_file = self.agent / "mcp.json"
        global_file.write_text(json.dumps({"mcpServers": {"global-docs": entry}}), encoding="utf-8")
        before_global = global_file.read_bytes()
        self.project.mkdir()
        config = self.project / "mcp.json"
        config.write_text(json.dumps({"mcpServers": {"local-docs": entry}}), encoding="utf-8")
        await self.start(trusted=True)
        before = await self.listing("mcp")
        self.assertEqual([row["key"] for row in before["items"]], ["local-docs"])
        self.assertTrue(before["items"][0]["live"])
        result = await self.set_selection("mcp", "local-docs", False, 7)
        self.assertEqual(result, {"accepted": True, "kind": "mcp", "scope": "project", "effect": "restart",
                                  "written": [".aelix/mcp.json"], "changed": [{"key": "local-docs", "saved": False}],
                                  "mismatches": [], "apply": None})
        saved = (await self.listing("mcp"))["items"][0]
        self.assertEqual((saved["saved"], saved["applied"], saved["live"], saved["pending"]),
                         (False, True, True, True))
        self.assertFalse(json.loads(config.read_bytes())["mcpServers"]["local-docs"]["enabled"])
        self.assertEqual(global_file.read_bytes(), before_global)
        self.assertFalse(self.permission_cards())
        await self.restart_worker()
        applied = (await self.listing("mcp"))["items"][0]
        self.assertEqual((applied["saved"], applied["applied"], applied["live"], applied["pending"]),
                         (False, False, False, False))
