import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from aelix_acp.server import Server, Session


class StudioExtensionManagementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        manager = root / "manager.py"
        manager.write_text("import json, sys\nprint(json.dumps(sys.argv[1:]))\n", encoding="utf-8")
        self.server = Server(SimpleNamespace(state_dir=temp.name, profile="coding", aelix=sys.executable,
                                             aelix_arg=[str(manager)]))
        self.addCleanup(self.server.db.close)
        self.session = Session(self.server, "session", temp.name)
        self.server.sessions[self.session.id] = self.session
        self.messages = []

        async def send(message):
            self.messages.append(message)

        self.server.send = send
        self.server.client_request = AsyncMock(return_value={
            "outcome": {"outcome": "selected", "optionId": "allow-once"},
        })

    async def request(self, method, **params):
        self.messages.clear()
        await self.server.handle({"jsonrpc": "2.0", "id": 1, "method": method,
                                  "params": {"sessionId": self.session.id, **params}})
        return next(message for message in self.messages if message.get("id") == 1)

    async def slash(self, command):
        return await self.request("session/prompt", prompt=[{"type": "text", "text": "/extension " + command}])

    def assert_studio_refusal(self, response, action):
        self.assertEqual(response.get("error"), {
            "code": -32004,
            "message": "Use the Extensions screen in Aelix Studio.",
            "data": {"kind": "studio_extension_management_required", "action": action},
        })
        self.assertFalse(self.session.dispatched)

    async def test_studio_refuses_writes_before_approval_or_launch(self):
        with patch.dict(os.environ, {"AELIX_ACP_STUDIO_MANAGED": "1"}), patch(
                "aelix_acp.runtime.extensions.asyncio.create_subprocess_exec") as spawn:
            for manager in (None, sys.executable):
                self.server.args.extension_manager = manager
                for command in ("install fixture", "install", "update fixture", "update", "remove fixture"):
                    with self.subTest(manager=manager, command=command):
                        self.assert_studio_refusal(await self.slash(command), command.split()[0])
            self.server.client_request.assert_not_called()
            spawn.assert_not_called()

    async def test_studio_refuses_package_writes_via_management_api(self):
        with patch.dict(os.environ, {"AELIX_ACP_STUDIO_MANAGED": "1"}), patch(
                "aelix_acp.runtime.extensions.asyncio.create_subprocess_exec") as spawn:
            for action in ("install", "update", "remove"):
                with self.subTest(action=action):
                    response = await self.request("_aelix/manage", action="extensions/action",
                                                  arguments={"action": action, "target": "fixture"})
                    self.assert_studio_refusal(response, action)
            self.server.client_request.assert_not_called()
            spawn.assert_not_called()

    async def assert_manager_argv(self, command, expected, requires_reload):
        response = await self.slash(command)
        self.assertEqual(response.get("result"), {"stopReason": "end_turn"}, response)
        update = next(message["params"]["update"] for message in self.messages
                      if message.get("method") == "session/update")
        result = json.loads(update["content"]["text"])
        self.assertEqual(result["exitCode"], 0)
        self.assertEqual(json.loads(result["text"]), expected)
        self.assertIs(result["requiresReload"], requires_reload)

    async def test_studio_keeps_read_actions(self):
        with patch.dict(os.environ, {"AELIX_ACP_STUDIO_MANAGED": "1"}):
            for action in ("list", "verify", "discover"):
                with self.subTest(action=action):
                    await self.assert_manager_argv(action, ["extension", action], False)
        self.server.client_request.assert_not_called()

    async def test_without_studio_marker_writes_still_require_approval_and_run(self):
        with patch.dict(os.environ):
            os.environ.pop("AELIX_ACP_STUDIO_MANAGED", None)
            for action in ("install", "update", "remove"):
                with self.subTest(action=action):
                    await self.assert_manager_argv(f"{action} fixture", ["extension", action, "fixture", "--yes"], True)
            os.environ["AELIX_ACP_STUDIO_MANAGED"] = "0"
            await self.assert_manager_argv("update", ["extension", "update", "--yes"], True)
        self.assertEqual([call.args[0] for call in self.server.client_request.await_args_list],
                         ["session/request_permission"] * 4)
