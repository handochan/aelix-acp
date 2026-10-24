"""Published beta.2 wheels, actual ACP subprocesses, synthetic local model only."""
import asyncio
import json
import os
import unittest

from runtime_fixture import RuntimeFixture
from support import AcpClient


@unittest.skipUnless(os.environ.get("AELIX_ACP_TEST_BETA2"), "Run scripts/test_beta2.py with the published beta.2 environment")
class Beta2RuntimeTests(RuntimeFixture):
    executable_env = "AELIX_ACP_TEST_BETA2"
    runtime_profile = None  # Exercise the actual CLI default, not a fixture override.

    async def reopen(self):
        await self.client.close()
        self.client = await AcpClient.start(self.root, self.executable, runtime_profile=None)
        return await self.client.request("session/load", {"cwd": str(self.root), "sessionId": self.session, "mcpServers": []})

    async def test_default_profile_and_commands_describe_the_published_runtime(self):
        self.assertEqual(self.client.initialized["_meta"]["aelix/runtimeProfile"], "beta2")
        self.assertEqual(self.client.initialized["agentCapabilities"]["mcpCapabilities"], {"http": False, "sse": False})
        frames = [event["params"]["update"] for event in self.client.updates]
        names = {entry["name"] for frame in frames for entry in frame.get("availableCommands", [])}
        self.assertTrue({"model", "thinking", "compact", "stats", "export", "tools", "skills", "settings", "reload", "agents", "extension"}.issubset(names))
        self.assertFalse({"mcp", "echo", "choose", "hooks", "scoped-models"} & names)
        self.assertEqual(self.created["modes"]["availableModes"][0]["id"], "default")
        self.assertEqual(len(self.created["modes"]["availableModes"]), 1)

    async def test_model_reasoning_and_four_runtime_selectors_are_applied(self):
        await self.config("thinking_level", "high")
        for key, value in (("steeringMode", "all"), ("followUpMode", "all"),
                           ("autoCompactionEnabled", "false"), ("autoRetryEnabled", "false")):
            result = await self.config(key, value)
            self.assertEqual(next(option["currentValue"] for option in result["configOptions"] if option["id"] == key), value)
        self.assertIn("model", {option["id"] for option in self.created["configOptions"]})
        self.assertEqual(self.created["models"]["currentModelId"], "fixture/one")
        self.assertTrue(self.created["models"]["availableModels"])
        await self.prompt("FIRST_MODEL_MARKER")
        await self.config("model", "fixture/two")
        self.assertEqual(await self.prompt("Respond"), {"stopReason": "end_turn"})
        self.assertIn("NATIVE_OK", self.client.text())
        self.assertEqual([r["model"] for r in self.model_requests], ["one", "two"])
        self.assertIn("FIRST_MODEL_MARKER", json.dumps(self.model_requests[-1]))

    async def test_explicit_permission_allows_one_write_and_denial_blocks_the_next(self):
        await self.prompt("WRITE_TEST")
        self.assertTrue(self.write_path.is_file())
        self.write_path.unlink()
        self.client.permission = "reject-once"
        await self.prompt("WRITE_TEST")
        self.assertFalse(self.write_path.exists())
        self.assertEqual(len([request for request in self.client.requests if request["method"] == "session/request_permission"]), 2)

    async def test_unenforceable_permission_modes_are_rejected(self):
        for mode in ("plan", "auto-accept-edits", "auto", "yolo"):
            with self.subTest(mode=mode), self.assertRaises(RuntimeError):
                await self.config("permission_mode", mode)
        self.assertEqual((await self.client.request("_aelix/capabilities", {"sessionId": self.session}))["configOptions"][0]["currentValue"], "default")

    async def test_management_and_commands_never_fall_through_to_the_model(self):
        for text in ("/hooks", "/scoped-models", "/mcp", "/echo hello"):
            with self.subTest(command=text), self.assertRaises(RuntimeError) as error:
                await self.prompt(text)
            self.assertEqual(json.loads(str(error.exception))["code"], -32004)
        with self.assertRaises(RuntimeError) as error:
            await self.manage("mcp/configure")
        self.assertEqual(json.loads(str(error.exception))["data"]["kind"], "runtime_capability_required")
        self.assertEqual(self.model_requests, [])

    async def test_unknown_commands_are_structured_errors_without_model_execution(self):
        with self.assertRaises(RuntimeError) as error:
            await self.prompt("/not-a-command")
        self.assertEqual(json.loads(str(error.exception))["data"]["kind"], "unknown_command")
        self.assertEqual(self.model_requests, [])

    async def test_image_embedded_text_and_resource_links_reach_the_native_model(self):
        image = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aXRcAAAAASUVORK5CYII="
        uri = (self.root / "context.txt").as_uri()
        result = await self.client.request("session/prompt", {"sessionId": self.session, "prompt": [
            {"type": "text", "text": "Inspect the supplied fixture"},
            {"type": "resource", "resource": {"uri": uri, "mimeType": "text/plain", "text": "EMBEDDED_FIXTURE_123"}},
            {"type": "resource_link", "uri": uri, "name": "context"},
            {"type": "image", "mimeType": "image/png", "data": image},
        ]})
        self.assertEqual(result["stopReason"], "end_turn")
        request = json.dumps(self.model_requests[-1])
        self.assertIn("EMBEDDED_FIXTURE_123", request)
        self.assertIn("data:image/png;base64,", request)

    async def test_model_command_uses_native_registry_without_calling_a_model(self):
        await self.prompt("/model")
        self.assertIn('"fixture/two"', self.client.text())
        self.assertEqual(self.model_requests, [])
        with self.assertRaises(RuntimeError) as error:
            await self.prompt("/model fixture/unknown")
        self.assertEqual(json.loads(str(error.exception))["code"], -32602)
        await self.prompt("/model fixture/two")
        self.assertEqual(self.model_requests, [])

    async def test_tool_selection_is_effective_empty_and_persistent(self):
        await self.manage("tools/set_active", {"names": ["read"]})
        await self.prompt("Only read tool")
        self.assertEqual([t["function"]["name"] for t in self.model_requests[-1]["tools"]], ["read"])
        with self.assertRaises(RuntimeError):
            await self.manage("tools/set_active", {"names": ["unknown-tool"]})
        await self.manage("tools/set_active", {"names": []})
        await self.reopen()
        await self.prompt("No tools after reopening")
        self.assertFalse(self.model_requests[-1].get("tools"))

    async def test_selected_skill_and_template_commands_expand_via_native_formatters(self):
        skill = self.root / "selected"
        skill.mkdir()
        (skill / "SKILL.md").write_text("---\nname: selected\ndescription: SELECTED_DESCRIPTION\n---\nSELECTED_BODY\n")
        ambient = self.root / "agent/skills/ambient"
        ambient.mkdir(parents=True)
        (ambient / "SKILL.md").write_text("---\nname: ambient\ndescription: AMBIENT_DESCRIPTION\n---\nAmbient\n")
        template = self.root / "review.md"
        template.write_text("---\ndescription: Review fixture\n---\nREVIEW_MARKER $1\n")
        await self.manage("resources/configure", {"inheritSkills": False, "skills": [str(skill)], "promptTemplates": [str(template)]})
        await self.prompt('/review "two words"')
        request = json.dumps(self.model_requests[-1])
        self.assertIn("REVIEW_MARKER two words", request)
        self.assertIn("SELECTED_DESCRIPTION", request)
        self.assertNotIn("AMBIENT_DESCRIPTION", request)
        await self.prompt("/skill:selected extra")
        self.assertIn("SELECTED_BODY", json.dumps(self.model_requests[-1]))
        await self.manage("resources/configure", {"inheritSkills": False, "skills": []})
        with self.assertRaises(RuntimeError):
            await self.prompt("/skill:selected")
        await self.prompt("After removing selected skill")
        self.assertNotIn("SELECTED_DESCRIPTION", json.dumps(self.model_requests[-1]["messages"][0]))

    async def test_native_profile_combination_restarts_preserving_history_and_grants(self):
        marker = self.root / "loaded.txt"
        extension = self.root / "profile_extension.py"
        extension.write_text("from pathlib import Path\ndef setup(api): Path(" + repr(str(marker)) + ").write_text('loaded')\n")
        content = ("---\nname: combo\ndescription: Combination\nprovider: fixture\nmodel: two\n"
                   "thinking: 'off'\ntools: [read]\ninherit_skills: false\ninherit_extensions: false\n"
                   "extensions: [" + json.dumps(str(extension)) + "]\nsystem_prompt: append\n---\nCOMBO_SYSTEM_MARKER\n")
        await self.prompt("BEFORE_PROFILE_MARKER")
        await self.manage("agents/save", {"name": "combo", "content": content})
        with self.assertRaises(RuntimeError):
            await self.manage("agents/save", {"name": "combo", "content": content})
        await self.manage("agents/use", {"name": "combo"})
        self.assertTrue(marker.is_file())
        await self.prompt("After profile")
        request = self.model_requests[-1]
        self.assertEqual(request["model"], "two")
        self.assertEqual([t["function"]["name"] for t in request["tools"]], ["read"])
        self.assertIn("COMBO_SYSTEM_MARKER", json.dumps(request))
        self.assertIn("BEFORE_PROFILE_MARKER", json.dumps(request))
        with self.assertRaises(RuntimeError):
            await self.manage("tools/set_active", {"names": ["bash"]})
        await self.reopen()
        await self.prompt("Profile survived ACP restart")
        self.assertEqual(self.model_requests[-1]["model"], "two")
        await self.manage("agents/use", {"name": "--none"})
        await self.manage("tools/set_active", {"names": ["bash"]})
        await self.prompt("Cleared profile")
        self.assertEqual(self.model_requests[-1]["model"], "one")

    async def test_resource_and_extension_changes_require_consent_before_effects(self):
        self.client.permission = "reject-once"
        extension = self.root / "rejected.py"
        marker = self.root / "rejected-loaded"
        extension.write_text("from pathlib import Path\ndef setup(api): Path(" + repr(str(marker)) + ").touch()\n")
        for action, params in (("resources/configure", {"inheritSkills": False}),
                               ("extensions/action", {"action": "enable", "target": str(extension)}),
                               ("agents/save", {"name": "no", "content": "---\nname: no\n---\nNo"})):
            with self.subTest(action=action), self.assertRaises(RuntimeError):
                await self.manage(action, params)
        self.assertFalse(marker.exists())
        self.assertFalse((self.root / "agent/agents/no.md").exists())
        self.assertEqual(await self.prompt("Usable after rejected changes"), {"stopReason": "end_turn"})

    async def test_untrusted_project_profile_and_changed_saved_profile_fail_closed(self):
        content = "---\nname: restricted\ndescription: Restricted fixture\ntools: []\n---\nRestricted system\n"
        project = self.root / ".aelix/agents/restricted.md"
        project.parent.mkdir(parents=True)
        project.write_text(content)
        with self.assertRaises(RuntimeError):
            await self.manage("agents/use", {"name": str(project)})
        await self.manage("agents/save", {"name": "restricted", "content": content})
        await self.manage("agents/use", {"name": "restricted"})
        await self.prompt("Restricted before source change")
        user = self.root / "agent/agents/restricted.md"
        original = user.read_bytes()
        user.write_text(content.replace("tools: []", "tools: [bash]"))
        with self.assertRaises(RuntimeError) as caught:
            await self.reopen()
        self.assertEqual(json.loads(str(caught.exception))["code"], -32004)
        user.write_bytes(original)
        await self.client.request("session/load", {"cwd": str(self.root), "sessionId": self.session, "mcpServers": []})
        await self.prompt("Restricted after file restoration")
        self.assertFalse(self.model_requests[-1].get("tools"))

    async def test_extension_enable_disable_replaces_runtime_without_dropping_history(self):
        extension = self.root / "toggle.py"
        marker = self.root / "toggle_loaded"
        extension.write_text("from pathlib import Path\ndef setup(api): Path(" + repr(str(marker)) + ").touch()\n")
        await self.prompt("BEFORE_EXTENSION_MARKER")
        result = await self.manage("extensions/action", {"action": "enable", "target": str(extension)})
        self.assertTrue(result["restarted"])
        self.assertTrue(marker.exists())
        marker.unlink()
        await self.manage("extensions/action", {"action": "disable", "target": str(extension)})
        self.assertFalse(marker.exists())
        await self.prompt("After disabling extension")
        self.assertIn("BEFORE_EXTENSION_MARKER", json.dumps(self.model_requests[-1]))

    async def test_cancelling_a_model_hook_replies_and_recovers_actual_selection(self):
        extension = self.root / "slow_model.py"
        marker = self.root / "hook_entered"
        extension.write_text("import asyncio\nfrom pathlib import Path\ndef setup(api):\n"
            " first = True\n async def selected(event, ctx):\n  nonlocal first\n"
            "  if first:\n   first = False\n   Path(" + repr(str(marker)) + ").touch()\n   await asyncio.Event().wait()\n"
            " api.on('model_select', selected)\n")
        await self.manage("extensions/action", {"action": "enable", "target": str(extension)})
        task = asyncio.create_task(self.config("model", "fixture/two"))
        try:
            async with asyncio.timeout(10):
                while not marker.exists():
                    await asyncio.sleep(.01)
            await self.client.send({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": self.session}})
            with self.assertRaises(RuntimeError) as caught:
                await asyncio.wait_for(task, 10)
            self.assertEqual(json.loads(str(caught.exception))["code"], -32003)
            await self.config("model", "fixture/one")
            await self.prompt("Usable after control cancellation")
            self.assertEqual(self.model_requests[-1]["model"], "one")
        finally:
            await asyncio.gather(task, return_exceptions=True)

    async def test_extension_controls_authentication_and_native_busy_guard(self):
        extension = self.root / "capture_endpoint.py"
        capture = self.root / "private_endpoint.json"
        extension.write_text("import os,json\nfrom pathlib import Path\ndef setup(api):\n Path(" + repr(str(capture)) +
            ").write_text(json.dumps({'file':os.environ['AELIX_ACP_CONTROLS_FILE'],'auth':json.loads(os.environ['AELIX_ACP_ENDPOINT'])['token']}))\n")
        await self.manage("extensions/action", {"action": "enable", "target": str(extension)})
        private = json.loads(capture.read_text())
        from pathlib import Path
        endpoint = json.loads(Path(private["file"]).read_text())
        async def raw(token):
            reader, writer = await asyncio.open_connection("127.0.0.1", endpoint["port"])
            try:
                writer.write((json.dumps({"id": "probe", "token": token, "method": "models/set", "params": {"model": "fixture/two"}}) + "\n").encode())
                await writer.drain()
                return json.loads(await reader.readline())
            finally:
                writer.close()
                await writer.wait_closed()
        self.assertIn("authentication", (await raw("wrong"))["error"])
        self.model_gate = asyncio.Event()
        task = asyncio.create_task(self.prompt("Hold native busy"))
        try:
            async with asyncio.timeout(10):
                while not self.model_requests:
                    await asyncio.sleep(.01)
            self.assertIn("busy", (await raw(private["auth"]))["error"])
            self.model_gate.set()
            self.model_gate = None
            await task
            await self.prompt("Still the original model")
            self.assertEqual(self.model_requests[-1]["model"], "one")
        finally:
            if self.model_gate:
                self.model_gate.set()
            await asyncio.gather(task, return_exceptions=True)

    async def test_duplicate_completed_request_does_not_repeat_model_or_tool_effects(self):
        await self.prompt("WRITE_TEST", "stable-turn")
        count = len(self.model_requests)
        await self.prompt("WRITE_TEST", "stable-turn")
        self.assertEqual(len(self.model_requests), count)
        with self.assertRaisesRegex(RuntimeError, "different prompt content"):
            await self.prompt("Changed content", "stable-turn")

    async def test_process_restart_restores_history_model_and_classic_settings(self):
        await self.config("thinking_level", "high")
        await self.config("autoRetryEnabled", "false")
        await self.prompt("Remember beta2 fixture")
        restored = await self.reopen()
        values = {option["id"]: option["currentValue"] for option in restored["configOptions"]}
        self.assertEqual(restored["models"]["currentModelId"], "fixture/one")
        self.assertEqual(values["thinking_level"], "high")
        self.assertEqual(values["autoRetryEnabled"], "false")
        self.assertIn("NATIVE_OK", self.client.text())

    async def test_cancel_during_a_model_request_leaves_the_session_usable(self):
        self.model_gate = asyncio.Event()
        task = asyncio.create_task(self.prompt("Wait for cancellation"))
        try:
            async with asyncio.timeout(10):
                while not self.model_requests:
                    await asyncio.sleep(.01)
            await self.client.send({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": self.session}})
            self.assertEqual(await asyncio.wait_for(task, 10), {"stopReason": "cancelled"})
            self.model_gate.set()
            self.model_gate = None
            self.assertEqual(await self.prompt("Respond after cancellation"), {"stopReason": "end_turn"})
        finally:
            if self.model_gate is not None:
                self.model_gate.set()
            await asyncio.gather(task, return_exceptions=True)

    async def test_client_mcp_is_rejected_before_a_candidate_is_admitted(self):
        for method in ("session/new", "session/load"):
            with self.subTest(method=method), self.assertRaises(RuntimeError) as error:
                await self.client.request(method, {"sessionId": self.session, "cwd": str(self.root),
                    "mcpServers": [{"name": "fixture", "command": "unused", "args": []}]})
            self.assertEqual(json.loads(str(error.exception))["data"]["capability"], "mcp/configure")
        sessions = await self.client.request("session/list", {"cwd": str(self.root)})
        self.assertEqual(len(sessions["sessions"]), 1)
        self.assertEqual(await self.prompt("Still usable"), {"stopReason": "end_turn"})

    async def test_control_profile_refuses_the_published_beta2_runtime(self):
        await self.client.close()
        self.client = await AcpClient.start(self.root, self.executable, runtime_profile="control-v1")
        with self.assertRaises(RuntimeError) as error:
            await self.client.request("session/new", {"cwd": str(self.root), "mcpServers": []})
        self.assertEqual(json.loads(str(error.exception))["code"], -32004)

    async def test_closed_session_can_be_reopened_and_deleted_from_the_index(self):
        await self.prompt("Persist history")
        await self.manage("sessions/close")
        self.assertEqual(len((await self.client.request("session/list", {"cwd": str(self.root)}))["sessions"]), 1)
        await self.client.request("session/load", {"cwd": str(self.root), "sessionId": self.session, "mcpServers": []})
        await self.client.request("session/delete", {"sessionId": self.session})
        self.assertEqual((await self.client.request("session/list", {"cwd": str(self.root)}))["sessions"], [])
