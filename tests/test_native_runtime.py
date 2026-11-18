"""Real Aelix + ACP + local model endpoint; no provider credentials or internet."""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from support import AcpClient
from aelix_acp.wire.errors import SESSION_LOCKED, TURN_FAILED_AFTER_DISPATCH

EXECUTABLE = os.environ.get("AELIX_ACP_TEST_AELIX")


@unittest.skipUnless(EXECUTABLE, "Set AELIX_ACP_TEST_AELIX to a runtime-control-v1 Aelix executable")
class NativeRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="acp-native-", dir="/tmp" if os.name != "nt" else None)
        self.root = Path(self.temp.name).resolve()
        (self.root / "home").mkdir()
        (self.root / "agent").mkdir()
        self.model_requests = []
        self.model_authorizations = []
        self.write_path = self.root / "approved.txt"
        self.failures = 0
        self.model_gate = None
        self.listener = await asyncio.start_server(self.model, "127.0.0.1", 0)
        port = self.listener.sockets[0].getsockname()[1]
        models = {"providers": {"fixture": {"baseUrl": f"http://127.0.0.1:{port}/v1", "api": "openai-completions",
            "apiKey": "synthetic-fixture-key", "models": [
                {"id": "one", "name": "Fixture One", "reasoning": True, "input": ["text", "image"],
                 "contextWindow": 32768, "maxTokens": 2048},
                {"id": "two", "name": "Fixture Two", "reasoning": False, "input": ["text"],
                 "contextWindow": 32768, "maxTokens": 2048}]}}}
        (self.root / "agent/models.json").write_text(json.dumps(models))
        (self.root / "agent/settings.json").write_text(json.dumps({"defaultProvider": "fixture", "defaultModel": "one"}))
        extension = self.root / "commands.py"
        extension.write_text('''import asyncio
async def echo(args, ctx): return "echo:" + args
async def choose(args, ctx):
    selected = await ctx.ui.select("Pick", ["one", "two"])
    return "selected:" + str(selected)
async def wait(args, ctx): await asyncio.Event().wait()
def setup(api):
    api.register_command("echo", description="Echo", handler=echo)
    api.register_command("choose", description="Choose", handler=choose)
    api.register_command("wait", description="Wait", handler=wait)
''')
        self.client = await AcpClient.start(self.root, EXECUTABLE, extensions=[extension],
                                          capabilities={"_meta": {"aelix/clientActions": True}})
        created = await self.client.request("session/new", {"cwd": str(self.root), "mcpServers": []}, timeout=120)
        self.session = created["sessionId"]
        self.created = created

    async def asyncTearDown(self):
        await self.client.close()
        self.listener.close()
        await self.listener.wait_closed()
        self.temp.cleanup()

    async def model(self, reader, writer):
        try:
            header = (await reader.readuntil(b"\r\n\r\n")).decode()
            length = int(next(line.split(":", 1)[1].strip() for line in header.split("\r\n") if line.lower().startswith("content-length:")))
            request = json.loads(await reader.readexactly(length))
            self.model_requests.append(request)
            self.model_authorizations.append(next(
                (line.split(":", 1)[1].strip() for line in header.split("\r\n")
                 if line.lower().startswith("authorization:")), None))
            if self.model_gate is not None:
                await self.model_gate.wait()
            messages = request.get("messages", [])
            last = messages[-1] if messages else {}
            content = str(last.get("content", ""))
            if last.get("role") == "user" and "DELEGATE_NOW" in content:
                delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": "call_agent_" + str(len(self.model_requests)),
                    "type": "function", "function": {"name": "agent", "arguments": json.dumps({
                        "profile": "reviewer", "task": "CHILD_TASK respond briefly"})}}]}
                stop = "tool_calls"
            elif last.get("role") == "user" and "CHILD_TASK" in content:
                delta, stop = {"role": "assistant", "content": "CHILD_OK"}, "stop"
            elif last.get("role") == "user" and "WRITE_TEST" in content:
                delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": "call_" + str(len(self.model_requests)),
                    "type": "function", "function": {"name": "write", "arguments": json.dumps({
                        "path": str(self.write_path), "content": "approved write"})}}]}
                stop = "tool_calls"
            else:
                delta, stop = {"role": "assistant", "content": "NATIVE_OK"}, "stop"
            def event(delta, finish):
                return "data: " + json.dumps({"id": "fixture", "object": "chat.completion.chunk", "created": 0,
                    "model": request.get("model", "one"), "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}) + "\n\n"
            body = (event(delta, None) + event({}, stop) + "data: [DONE]\n\n").encode()
            if self.failures:
                self.failures -= 1
                body = b'data: {"error":{"message":"overloaded fixture","type":"overloaded_error"}}\n\n'
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\nContent-Length: "
                         + str(len(body)).encode() + b"\r\n\r\n" + body)
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    async def prompt(self, text, key=None, fork_head=None):
        metadata = {}
        if key:
            metadata["aelix/requestId"] = key
        if fork_head:
            metadata["aelix/forkHead"] = fork_head
        return await self.client.request("session/prompt", {"sessionId": self.session,
            "prompt": [{"type": "text", "text": text}], "_meta": metadata})

    def fork_params(self, anchor, mcp_servers=()):
        return {"cwd": str(self.root), "sessionId": self.session, "mcpServers": list(mcp_servers),
                "_meta": {"aelix/expectedHead": anchor}}

    async def config(self, key, value):
        return await self.client.request("session/set_config_option", {"sessionId": self.session, "configId": key, "value": value})

    async def manage(self, action, arguments=None):
        return await self.client.request("_aelix/manage", {"sessionId": self.session, "action": action, "arguments": arguments or {}})

    async def test_mcp_servers_are_forwarded_and_tools_can_be_selected(self):
        import sys
        script = self.root / "mcp_server.py"
        script.write_text('from mcp.server.fastmcp import FastMCP\ns=FastMCP("fixture")\n@s.tool()\ndef echo(value: str) -> str: return value\ns.run()\n')
        result = await self.manage("mcp/configure", {"servers": [{"name": "fixture", "command": sys.executable, "args": [str(script)]}]})
        self.assertTrue(result["servers"][0]["connected"])
        resources = await self.manage("resources/list")
        tool_name = next(tool["name"] for tool in resources["tools"] if tool["name"] == "acp_fixture__echo")
        await self.manage("tools/set_active", {"names": [tool_name]})
        await self.prompt("Respond")
        self.assertEqual(self.model_requests[-1]["tools"][0]["function"]["name"], tool_name)
        await self.manage("workspace/trust", {"trusted": False})
        self.assertTrue((await self.manage("mcp/status"))["servers"][0]["connected"])
        self.assertEqual((await self.manage("resources/list"))["activeTools"], [tool_name])
        self.client.dialog = lambda _request: {"value": "two"}
        await self.prompt("/choose")
        self.assertIn("selected:two", self.client.text())
        await self.manage("mcp/configure", {"servers": []})
        self.assertEqual((await self.manage("resources/list"))["activeTools"], [])
        self.assertEqual((await self.manage("mcp/status"))["servers"], [])
        await self.manage("mcp/configure", {"servers": [{"name": "fixture", "command": sys.executable, "args": [str(script)]}]})
        self.assertEqual((await self.manage("resources/list"))["activeTools"], [tool_name])

    async def test_global_profile_selection_survives_adapter_restart(self):
        profiles = self.root / "agent/agents"
        profiles.mkdir()
        (profiles / "reviewer.md").write_text("---\nname: reviewer\ndescription: Review\ninherit_extensions: true\n---\nReview carefully.\n")
        await self.manage("agents/use", {"name": "reviewer"})
        await self.prompt("Respond")
        await self.client.close()
        self.client = await AcpClient.start(self.root, EXECUTABLE)
        await self.client.request("session/load", {"cwd": str(self.root), "sessionId": self.session, "mcpServers": []}, timeout=120)
        capabilities = await self.client.request("_aelix/capabilities", {"sessionId": self.session})
        self.assertEqual(capabilities["runtime"]["activeAgent"], "reviewer")

    async def test_client_mcp_load_restores_tool_selection_and_inventory(self):
        import sys
        script = self.root / "mcp_server.py"
        script.write_text('from mcp.server.fastmcp import FastMCP\ns=FastMCP("fixture")\n@s.tool()\ndef echo(value: str) -> str: return value\ns.run()\n')
        servers = [{"name": "fixture", "command": sys.executable, "args": [str(script)]}]
        await self.manage("mcp/configure", {"servers": servers})
        profiles = self.root / "agent/agents"
        profiles.mkdir()
        (profiles / "mcp-profile.md").write_text("---\nname: mcp-profile\ndescription: MCP\ntools: [acp_fixture__echo]\n---\nUse MCP.\n", encoding="utf-8")
        await self.manage("agents/use", {"name": "mcp-profile"})
        before = {tool["name"] for tool in (await self.manage("resources/list"))["tools"]}
        await self.manage("tools/set_active", {"names": ["acp_fixture__echo"]})
        await self.client.close()
        self.client = await AcpClient.start(self.root, EXECUTABLE)
        await self.client.request("session/load", {"cwd": str(self.root), "sessionId": self.session, "mcpServers": servers}, timeout=120)
        after = await self.manage("resources/list")
        self.assertEqual({tool["name"] for tool in after["tools"]}, before)
        self.assertEqual(after["activeTools"], ["acp_fixture__echo"])

    async def test_native_subagent_runs_and_emits_progress(self):
        profiles = self.root / "agent/agents"
        profiles.mkdir()
        (profiles / "reviewer.md").write_text("---\nname: reviewer\ndescription: Review\n---\nReview carefully.\n")
        await self.manage("agents/enable", {"enabled": True})
        self.client.dialog = lambda _request: {"confirmed": True, "value": "yes"}
        result = await self.manage("agents/run", {"name": "reviewer", "task": "Respond briefly"})
        self.assertTrue(result["result"]["ok"], result)
        events = [value["params"]["event"] for value in self.client.updates if value.get("method") == "_aelix/subagent"]
        self.assertIn("subagent_start", events)
        self.assertIn("subagent_end", events)

    async def test_settings_schema_and_reload_apply_native_values(self):
        settings = await self.manage("settings/get")
        self.assertTrue(any(field["key"] == "retry_enabled" for field in settings["schema"]))
        await self.manage("settings/set", {"key": "retry_enabled", "value": False})
        await self.prompt("/reload")
        current = await self.client.request("_aelix/capabilities", {"sessionId": self.session})
        self.assertEqual(next(option for option in current["configOptions"] if option["id"] == "autoRetryEnabled")["currentValue"], "false")

    async def test_api_key_changes_never_return_or_replay_the_key(self):
        result = await self.manage("auth/set_api_key", {"provider": "another", "key": "PRIVATE_SYNTHETIC_VALUE"})
        self.assertNotIn("PRIVATE_SYNTHETIC_VALUE", json.dumps(result))
        self.assertNotIn("PRIVATE_SYNTHETIC_VALUE", self.client.text())
        await self.manage("auth/logout", {"provider": "another"})

    async def test_extension_enable_disable_changes_real_command_catalog(self):
        extension = self.root / "extra.py"
        extension.write_text('def setup(api):\n    api.register_command("extra", handler=lambda args, ctx: "EXTRA_OK", description="Extra")\n')
        await self.manage("extensions/action", {"action": "enable", "target": str(extension)})
        await self.prompt("/extra")
        self.assertIn("EXTRA_OK", self.client.text())
        await self.manage("extensions/action", {"action": "disable", "target": str(extension)})
        with self.assertRaisesRegex(RuntimeError, "Unknown slash"):
            await self.prompt("/extra")

    async def test_clone_creates_a_new_acp_identity_and_preserves_parent(self):
        await self.manage("tools/set_active", {"names": ["read"]})
        await self.prompt("Respond")
        await self.prompt("/clone")
        self.assertEqual((await self.manage("resources/list"))["activeTools"], ["read"])
        listing = await self.client.request("session/list", {"cwd": str(self.root)})
        self.assertEqual(len(listing["sessions"]), 2)
        self.assertIn(self.session, [row["sessionId"] for row in listing["sessions"]])
        await self.prompt("Respond again")
        self.assertIn("NATIVE_OK", self.client.text())

    async def test_request_key_cannot_be_reused_for_different_content(self):
        await self.prompt("Respond", "unique-key")
        count = len(self.model_requests)
        with self.assertRaisesRegex(RuntimeError, "different prompt"):
            await self.prompt("Different content", "unique-key")
        self.assertEqual(len(self.model_requests), count)

    async def test_empty_session_can_be_reopened_without_a_native_transcript(self):
        await self.config("permission_mode", "yolo")
        await self.config("model", "fixture/two")
        await self.client.close()
        self.client = await AcpClient.start(self.root, EXECUTABLE)
        result = await self.client.request("session/load", {"cwd": str(self.root), "sessionId": self.session, "mcpServers": []})
        self.assertEqual(result["modes"]["currentModeId"], "yolo")
        self.assertEqual(result["models"]["currentModelId"], "fixture/two")

    async def test_native_file_selectors_win_over_saved_adapter_preferences(self):
        await self.config("thinking_level", "low")
        await self.prompt("Seed the native session")
        await self.client.close()

        state_dir = self.root / "state/sessions.sqlite3"
        with contextlib.closing(sqlite3.connect(state_dir)) as db:
            filename, = db.execute("SELECT file FROM sessions WHERE id=?", (self.session,)).fetchone()
            saved, = db.execute("SELECT value FROM preferences WHERE session_id=?", (self.session,)).fetchone()
        self.assertEqual(json.loads(saved)["config"]["model"], "fixture/one")
        self.assertEqual(json.loads(saved)["config"]["thinking_level"], "low")

        models_path = self.root / "agent/models.json"
        models = json.loads(models_path.read_text(encoding="utf-8"))
        models["providers"]["fixture"]["models"][1]["reasoning"] = True
        models_path.write_text(json.dumps(models), encoding="utf-8")

        env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP") if key in os.environ}
        env.update(HOME=str(self.root / "home"), USERPROFILE=str(self.root / "home"),
                   AELIX_CODING_AGENT_DIR=str(self.root / "agent"),
                   AELIX_SETTINGS_PATH=str(self.root / "agent/settings.json"))
        native = await asyncio.create_subprocess_exec(
            EXECUTABLE, "--mode", "rpc", "--offline", "--no-extensions", "--session", filename,
            cwd=self.root, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
        try:
            for request in (
                {"id": "model", "type": "set_model", "provider": "fixture", "modelId": "two"},
                {"id": "level", "type": "set_thinking_level", "level": "high"},
            ):
                native.stdin.write((json.dumps(request) + "\n").encode())
                await native.stdin.drain()
                async with asyncio.timeout(30):
                    while line := await native.stdout.readline():
                        response = json.loads(line)
                        if response.get("id") == request["id"] and response.get("type") == "response":
                            self.assertTrue(response["success"], response)
                            break
                    else:
                        self.fail(f"Native RPC closed while handling {request['type']}")
        finally:
            native.stdin.close()
            try:
                await asyncio.wait_for(native.wait(), 10)
            except TimeoutError:
                native.kill()
                await native.wait()
        self.assertEqual(native.returncode, 0)

        self.client = await AcpClient.start(self.root, EXECUTABLE)
        restored = await self.client.request(
            "session/load", {"cwd": str(self.root), "sessionId": self.session, "mcpServers": []}, timeout=120)
        options = {option["id"]: option["currentValue"] for option in restored["configOptions"]}
        self.assertEqual((options["model"], options["thinking_level"]), ("fixture/two", "high"))
        self.assertEqual(await self.prompt("Use the restored model"), {"stopReason": "end_turn"})
        self.assertEqual([request["model"] for request in self.model_requests], ["one", "two"])
        with contextlib.closing(sqlite3.connect(state_dir)) as db:
            persisted, = db.execute("SELECT value FROM preferences WHERE session_id=?", (self.session,)).fetchone()
        config = json.loads(persisted)["config"]
        self.assertEqual((config["model"], config["thinking_level"]), ("fixture/two", "high"))

    async def test_launch_model_is_only_a_default_for_sessions(self):
        launch = ["--provider", "fixture", "--model", "two"]
        await self.client.close()
        self.client = await AcpClient.start(self.root, EXECUTABLE, adapter_args=launch)
        new = await self.client.request("session/new", {"cwd": str(self.root), "mcpServers": []})
        self.session = new["sessionId"]
        self.assertEqual(new["models"]["currentModelId"], "fixture/two")
        await self.config("model", "fixture/one")
        await self.config("thinking_level", "high")
        await self.prompt("Use the explicit selector")
        await self.client.close()

        self.client = await AcpClient.start(self.root, EXECUTABLE, adapter_args=launch)
        restored = await self.client.request("session/load", {
            "cwd": str(self.root), "sessionId": self.session, "mcpServers": [],
        })
        options = {option["id"]: option["currentValue"] for option in restored["configOptions"]}
        self.assertEqual((options["model"], options["thinking_level"]), ("fixture/one", "high"))
        await self.prompt("Use the resumed selectors")
        self.assertEqual([request["model"] for request in self.model_requests], ["one", "one"])

    async def test_profile_restore_keeps_native_selectors_and_enforces_tool_filter(self):
        profiles = self.root / "agent/agents"
        profiles.mkdir()
        (profiles / "restricted.md").write_text(
            "---\nname: restricted\ndescription: Restricted\nprovider: fixture\nmodel: two\n"
            "thinking: 'off'\ntools: [read]\n---\nRead only.\n", encoding="utf-8")
        await self.manage("agents/use", {"name": "restricted"})
        await self.config("model", "fixture/one")
        await self.config("thinking_level", "high")
        await self.config("permission_mode", "yolo")
        await self.prompt("Seed the restricted session")
        await self.client.close()

        state_dir = self.root / "state/sessions.sqlite3"
        with contextlib.closing(sqlite3.connect(state_dir)) as db, db:
            saved, = db.execute("SELECT value FROM preferences WHERE session_id=?", (self.session,)).fetchone()
            preferences = json.loads(saved)
            self.assertEqual(preferences["agent"], "restricted")
            preferences["config"].update(model="fixture/two", thinking_level="off")
            db.execute("UPDATE preferences SET value=? WHERE session_id=?", (json.dumps(preferences), self.session))

        self.client = await AcpClient.start(self.root, EXECUTABLE)
        restored = await self.client.request("session/load", {
            "cwd": str(self.root), "sessionId": self.session, "mcpServers": [],
        })
        options = {option["id"]: option["currentValue"] for option in restored["configOptions"]}
        self.assertEqual((options["model"], options["thinking_level"]), ("fixture/one", "high"))
        self.assertEqual(restored["modes"]["currentModeId"], "yolo")
        self.assertEqual((await self.manage("resources/list"))["activeTools"], ["read"])
        # The provider deliberately proposes a write even though it was not
        # offered this tool. Restored Yolo must not bypass the profile filter.
        await self.prompt("WRITE_TEST")
        self.assertTrue(all(request["model"] == "one" for request in self.model_requests))
        self.assertGreaterEqual(len(self.model_requests), 2)
        self.assertEqual([tool["function"]["name"] for tool in self.model_requests[-1]["tools"]], ["read"])
        self.assertFalse(self.write_path.exists())
        with contextlib.closing(sqlite3.connect(state_dir)) as db:
            saved, = db.execute("SELECT value FROM preferences WHERE session_id=?", (self.session,)).fetchone()
        config = json.loads(saved)["config"]
        self.assertEqual((config["model"], config["thinking_level"]), ("fixture/one", "high"))

    async def test_resume_does_not_rebind_a_typed_key_to_another_provider(self):
        await self.client.close()
        models_path = self.root / "agent/models.json"
        models = json.loads(models_path.read_text(encoding="utf-8"))
        models["providers"]["alternate"] = {
            **models["providers"]["fixture"], "apiKey": "synthetic-alternate-key",
            "models": [{"id": "alternate-model", "name": "Alternate", "reasoning": False,
                        "input": ["text"], "contextWindow": 32768, "maxTokens": 2048}],
        }
        models_path.write_text(json.dumps(models), encoding="utf-8")
        self.client = await AcpClient.start(self.root, EXECUTABLE)
        created = await self.client.request("session/new", {"cwd": str(self.root), "mcpServers": []})
        self.session = created["sessionId"]
        await self.config("model", "alternate/alternate-model")
        await self.prompt("Record the alternate provider in history")
        await self.client.close()
        (self.root / "agent/settings.json").write_text(json.dumps({
            "defaultProvider": "alternate", "defaultModel": "alternate-model",
        }), encoding="utf-8")

        typed_key = "synthetic-typed-launch-key"
        launch = ["--provider", "fixture", "--model", "one"]
        self.client = await AcpClient.start(
            self.root, EXECUTABLE, prefix=["--api-key", typed_key], adapter_args=launch)
        restored = await self.client.request("session/load", {
            "cwd": str(self.root), "sessionId": self.session, "mcpServers": [],
        })
        self.assertEqual(restored["models"]["currentModelId"], "fixture/one")
        self.assertNotIn(typed_key, json.dumps(restored))
        await self.prompt("Use the credential-bound launch route")
        await self.config("model", "alternate/alternate-model")
        await self.prompt("Select the other provider")
        self.assertEqual([request["model"] for request in self.model_requests], ["alternate-model", "one", "alternate-model"])
        self.assertEqual(self.model_authorizations, [
            "Bearer synthetic-alternate-key", f"Bearer {typed_key}", "Bearer synthetic-alternate-key",
        ])

    async def test_live_session_lease_blocks_other_adapters_and_close_preserves_history(self):
        other = await AcpClient.start(self.root, EXECUTABLE)
        try:
            for method, params in (("session/load", {"sessionId": self.session, "cwd": str(self.root), "mcpServers": []}),
                                   ("session/delete", {"sessionId": self.session})):
                with self.assertRaises(RuntimeError) as caught:
                    await other.request(method, params)
                error = json.loads(str(caught.exception))
                self.assertEqual((error["code"], error["data"]["kind"], error["data"]["lockedBy"]),
                                 (SESSION_LOCKED, "session_locked", "acp"))
            await self.manage("sessions/close")
            result = await other.request("session/load", {"sessionId": self.session, "cwd": str(self.root), "mcpServers": []})
            self.assertIn("configOptions", result)
        finally:
            await other.close()

    async def test_models_reasoning_and_real_local_model_turn(self):
        options = self.created["configOptions"]
        self.assertIn("fixture/two", [m["value"] for o in options if o["id"] == "model" for m in o["options"]])
        await self.config("thinking_level", "high")
        changed = await self.config("model", "fixture/two")
        self.assertEqual(next(o for o in changed["configOptions"] if o["id"] == "thinking_level")["currentValue"], "off")
        self.assertEqual(await self.prompt("Respond briefly"), {"stopReason": "end_turn"})
        self.assertIn("NATIVE_OK", self.client.text())
        self.assertEqual(self.model_requests[-1]["model"], "two")

    async def test_native_retry_settles_before_the_prompt_response(self):
        self.failures = 1
        self.assertEqual(await self.prompt("Respond after retry"), {"stopReason": "end_turn"})
        self.assertEqual(len(self.model_requests), 2)
        self.assertIn("NATIVE_OK", self.client.text())
        self.assertEqual(await self.prompt("Respond again"), {"stopReason": "end_turn"})

    async def test_native_handled_input_finishes_without_an_agent_end_event(self):
        extension = self.root / "handled.py"
        extension.write_text('from aelix_agent_core.harness.hooks import InputHandled\nasync def intercept(event, ctx):\n    if event.text == "INTERCEPT": return InputHandled()\ndef setup(api): api.on("input", intercept)\n', encoding="utf-8")
        await self.manage("extensions/action", {"action": "enable", "target": str(extension)})
        self.assertEqual(await self.prompt("INTERCEPT"), {"stopReason": "end_turn"})
        self.assertEqual(self.model_requests, [])

    async def test_cancel_crossing_before_agent_start_stops_the_later_turn_task(self):
        await self.cancel_startup_with_abort_hook(False)

    async def test_failing_abort_hook_keeps_the_cancelled_prompt_outcome(self):
        await self.cancel_startup_with_abort_hook(True)

    async def cancel_startup_with_abort_hook(self, raises):
        extension = self.root / "paused.py"
        hook = 'Path(ctx.cwd,"aborted").touch()' + ('; raise RuntimeError("failing abort hook")' if raises else '')
        extension.write_text('import asyncio\nfrom pathlib import Path\nasync def pause(event, ctx):\n    Path(ctx.cwd,"paused").touch()\n    while not Path(ctx.cwd,"resume").exists(): await asyncio.sleep(0.01)\nasync def aborted(event, ctx): ' + hook + '\ndef setup(api):\n    api.on("before_agent_start",pause)\n    api.on("abort",aborted)\n', encoding="utf-8")
        await self.manage("extensions/action", {"action": "enable", "target": str(extension)})
        self.model_gate = asyncio.Event()
        prompt = asyncio.create_task(self.prompt("Respond"))
        cancelling = None
        try:
            async with asyncio.timeout(10):
                while not (self.root / "paused").exists():
                    await asyncio.sleep(0.01)
                cancelling = asyncio.create_task(self.client.request("session/cancel", {"sessionId": self.session}))
                while not (self.root / "aborted").exists():
                    await asyncio.sleep(0.01)
            (self.root / "resume").touch()
            self.assertEqual(await asyncio.wait_for(prompt, 10), {"stopReason": "cancelled"})
            await cancelling
        finally:
            self.model_gate.set()
            (self.root / "resume").touch()
            for task in (prompt, cancelling):
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_mcp_reconfiguration_respects_profile_filters_without_gui_selection(self):
        import sys
        script = self.root / "profile_mcp.py"
        script.write_text('from mcp.server.fastmcp import FastMCP\ns=FastMCP("fixture")\n@s.tool()\ndef echo(value: str)->str:return value\ns.run()\n', encoding="utf-8")
        servers = [{"name": "fixture", "command": sys.executable, "args": [str(script)]}]
        await self.manage("agents/use", {"name": "--none"})
        await self.manage("mcp/configure", {"servers": servers})
        self.assertIn("acp_fixture__echo", (await self.manage("resources/list"))["activeTools"])
        profiles = self.root / "agent/agents"
        profiles.mkdir()
        (profiles / "all.md").write_text("---\nname: all\ndescription: All\n---\nAll tools.\n", encoding="utf-8")
        await self.manage("agents/use", {"name": "all"})
        await self.manage("mcp/configure", {"servers": []})
        resources = await self.manage("resources/list")
        baseline = resources["activeTools"]
        (profiles / "restricted.md").write_text("---\nname: restricted\ndescription: Native only\ntools: " + json.dumps(baseline) + "\n---\nNative tools only.\n", encoding="utf-8")
        await self.manage("agents/use", {"name": "restricted"})
        await self.manage("mcp/configure", {"servers": servers})
        self.assertEqual(set((await self.manage("resources/list"))["activeTools"]), set(baseline))

    async def test_edited_profile_tool_restrictions_replace_its_prior_filter(self):
        profiles = self.root / "agent/agents"
        profiles.mkdir()
        profile = profiles / "restricted.md"
        profile.write_text("---\nname: restricted\ndescription: Restricted\ntools: [read, bash]\n---\nRead or bash.\n", encoding="utf-8")
        await self.manage("agents/use", {"name": "restricted"})
        await self.prompt("Respond")
        await self.client.close()
        profile.write_text("---\nname: restricted\ndescription: Restricted\ntools: [read]\n---\nRead only.\n", encoding="utf-8")
        self.client = await AcpClient.start(self.root, EXECUTABLE)
        await self.client.request("session/load", {"cwd": str(self.root), "sessionId": self.session, "mcpServers": []})
        self.assertEqual((await self.manage("resources/list"))["activeTools"], ["read"])

    async def test_removed_model_recovers_with_the_native_fallback(self):
        await self.config("model", "fixture/two")
        await self.prompt("Respond")
        await self.client.close()
        filename = self.root / "agent/models.json"
        config = json.loads(filename.read_text(encoding="utf-8"))
        provider = config["providers"].pop("fixture")
        provider["models"] = provider["models"][:1]
        config["providers"]["replacement"] = provider
        filename.write_text(json.dumps(config), encoding="utf-8")
        (self.root / "agent/settings.json").write_text(json.dumps({"defaultProvider": "replacement", "defaultModel": "one"}), encoding="utf-8")
        self.client = await AcpClient.start(self.root, EXECUTABLE)
        result = await self.client.request("session/load", {"cwd": str(self.root), "sessionId": self.session, "mcpServers": []})
        self.assertEqual(result["models"]["currentModelId"], "replacement/one")
        self.assertTrue(result["_meta"]["aelix/recoveryWarnings"])

    async def test_removed_profile_refuses_the_load_and_never_widens_tools(self):
        profiles = self.root / "agent/agents"
        profiles.mkdir()
        profile = profiles / "restricted.md"
        profile.write_text("---\nname: restricted\ndescription: Restricted\ntools: [read]\n---\nRead only.\n", encoding="utf-8")
        await self.manage("agents/use", {"name": "restricted"})
        await self.config("permission_mode", "yolo")
        await self.prompt("Respond")
        self.assertEqual([tool["function"]["name"] for tool in self.model_requests[-1]["tools"]], ["read"])
        await self.client.close()
        profile.unlink()
        requests = len(self.model_requests)
        self.client = await AcpClient.start(self.root, EXECUTABLE)
        with self.assertRaises(RuntimeError) as caught:
            await self.client.request("session/load", {"cwd": str(self.root), "sessionId": self.session, "mcpServers": []})
        error = json.loads(str(caught.exception))
        self.assertEqual((error["code"], error["data"]), (-32004, {"kind": "profile_unavailable", "profile": "restricted"}))
        self.assertIn("Restore or recreate", error["message"])
        # What a host replays from its saved preference must not reach a session.
        with self.assertRaises(RuntimeError):
            await self.client.request("session/set_mode", {"sessionId": self.session, "modeId": "yolo"})
        with self.assertRaisesRegex(RuntimeError, "Unknown session"):
            await self.config("model", "fixture/two")
        with self.assertRaisesRegex(RuntimeError, "Unknown session"):
            await self.manage("agents/use", {"name": "--none"})
        with self.assertRaises(RuntimeError):
            await self.prompt("WRITE_TEST")
        self.assertFalse(self.write_path.exists())
        self.assertEqual(len(self.model_requests), requests)
        profile.write_text("---\nname: restricted\ndescription: Restricted\ntools: [read]\n---\nRead only.\n", encoding="utf-8")
        result = await self.client.request("session/load", {"cwd": str(self.root), "sessionId": self.session, "mcpServers": []})
        self.assertEqual(result["modes"]["currentModeId"], "yolo")

    async def test_approved_write_is_real_and_denied_write_is_absent(self):
        await self.prompt("WRITE_TEST")
        self.assertEqual((self.root / "approved.txt").read_text(), "approved write")
        (self.root / "approved.txt").unlink()
        self.client.permission = "reject-once"
        await self.prompt("WRITE_TEST")
        self.assertFalse((self.root / "approved.txt").exists())

    async def test_yolo_skips_client_approval_and_ask_restores_it(self):
        await self.config("permission_mode", "yolo")
        self.assertEqual((await self.manage("permissions/get"))["mode"], "yolo")
        await self.prompt("WRITE_TEST")
        self.assertTrue((self.root / "approved.txt").exists())
        self.assertFalse(any(r["method"] == "session/request_permission" for r in self.client.requests))
        (self.root / "approved.txt").unlink()
        await self.config("permission_mode", "default")
        self.assertEqual((await self.manage("permissions/get"))["mode"], "default")
        self.client.permission = "reject-once"
        await self.prompt("WRITE_TEST")
        self.assertFalse((self.root / "approved.txt").exists())
        self.assertTrue(any(r["method"] == "session/request_permission" for r in self.client.requests))
        for mode in ("plan", "auto-accept-edits", "auto"):
            with self.assertRaisesRegex(RuntimeError, "Unsupported value"):
                await self.config("permission_mode", mode)

    async def test_delegated_child_progress_rides_the_parent_tool_call(self):
        profiles = self.root / "agent/agents"
        profiles.mkdir()
        (profiles / "reviewer.md").write_text("---\nname: reviewer\ndescription: Review\ntools: [read]\n---\nReview.\n", encoding="utf-8")
        await self.manage("agents/enable", {"enabled": True})
        self.assertEqual(await self.prompt("DELEGATE_NOW"), {"stopReason": "end_turn"})
        updates = [event["params"]["update"] for event in self.client.updates if event.get("method") == "session/update"]
        parent = next(update["toolCallId"] for update in updates
                      if update.get("sessionUpdate") == "tool_call" and update.get("title") == "agent")
        frames = [update for update in updates if update.get("toolCallId") == parent and "_meta" in update]
        self.assertTrue(frames, "No parent tool_call_update carried child progress")
        self.assertTrue(all(update["sessionUpdate"] == "tool_call_update" for update in frames))
        children = [frame["_meta"]["aelix/subagents"]["children"] for frame in frames]
        self.assertEqual({child["profile"] for group in children for child in group}, {"reviewer"})
        self.assertIn("subagent_start", {child["event"] for group in children for child in group})
        final = next(update for update in reversed(updates) if update.get("toolCallId") == parent and update.get("status") == "completed")
        self.assertEqual(final["_meta"]["aelix/subagents"]["children"][0]["state"], "done")
        # Standard ACP only: the unhandled extension notification is not used for a parented child.
        self.assertFalse([event for event in self.client.updates if event.get("method") == "_aelix/subagent"])

    async def test_launch_extension_cannot_be_disabled_by_the_session(self):
        extension = self.root / "commands.py"
        os.link(extension, self.root / "linked.py")
        variants = [str(extension.resolve()), str(self.root / "linked.py")]
        if os.path.exists(self.root / "COMMANDS.py"):  # Only a case-insensitive filesystem has this name.
            variants.append(str(self.root / "COMMANDS.py"))
        for target in variants:
            for action in ("disable", "enable"):
                with self.subTest(target=target, action=action):
                    with self.assertRaises(RuntimeError) as caught:
                        await self.manage("extensions/action", {"action": action, "target": target})
                    error = json.loads(str(caught.exception))
                    self.assertEqual((error["code"], error["data"]["kind"]), (-32004, "extension_owned_by_launch"))
        await self.prompt("/echo still-loaded")
        self.assertIn("echo:still-loaded", self.client.text())
        loaded = {entry.get("source") or entry.get("name") for entry in (await self.manage("resources/list"))["extensions"]}
        self.assertIn(str(extension.resolve()), loaded)

    async def test_disabled_extension_is_absent_from_native_inventory(self):
        extension = self.root / "extra.py"
        extension.write_text('def setup(api):\n    api.register_command("extra", handler=lambda args, ctx: "EXTRA_OK", description="Extra")\n')
        enabled = await self.manage("extensions/action", {"action": "enable", "target": str(extension)})
        self.assertEqual((enabled["ready"], enabled["loaded"]), (True, True))
        loaded = {entry.get("source") or entry.get("name") for entry in (await self.manage("resources/list"))["extensions"]}
        self.assertIn(str(extension.resolve()), loaded)
        disabled = await self.manage("extensions/action", {"action": "disable", "target": str(extension)})
        self.assertEqual(disabled["enabledExtensions"], [])
        loaded = {entry.get("source") or entry.get("name") for entry in (await self.manage("resources/list"))["extensions"]}
        self.assertNotIn(str(extension.resolve()), loaded)
        with self.assertRaisesRegex(RuntimeError, "Unknown slash"):
            await self.prompt("/extra")

    async def test_seeded_missing_profile_with_saved_yolo_refuses_instead_of_widening(self):
        await self.prompt("Respond")
        await self.client.close()
        import sqlite3
        with contextlib.closing(sqlite3.connect(self.root / "state/sessions.sqlite3")) as db, db:
            value = json.loads(db.execute("SELECT value FROM preferences WHERE session_id=?", (self.session,)).fetchone()[0])
            value.update(agent="restricted-fixture", activeTools=[], permission_mode="yolo")
            db.execute("UPDATE preferences SET value=? WHERE session_id=?", (json.dumps(value), self.session))
        requests = len(self.model_requests)
        self.client = await AcpClient.start(self.root, EXECUTABLE)
        with self.assertRaises(RuntimeError) as caught:
            await self.client.request("session/load", {"cwd": str(self.root), "sessionId": self.session, "mcpServers": []})
        self.assertEqual(json.loads(str(caught.exception))["data"], {"kind": "profile_unavailable", "profile": "restricted-fixture"})
        with self.assertRaises(RuntimeError):
            await self.prompt("Respond")
        self.assertEqual(len(self.model_requests), requests)

    async def test_native_lock_holder_refuses_load_with_a_structured_error(self):
        import sqlite3
        import subprocess
        await self.prompt("Respond")
        await self.manage("sessions/close")
        with contextlib.closing(sqlite3.connect(self.root / "state/sessions.sqlite3")) as db:
            native_file = db.execute("SELECT file FROM sessions WHERE id=?", (self.session,)).fetchone()[0]
        env = {key: os.environ[key] for key in ["PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"] if key in os.environ}
        env.update(HOME=str(self.root / "home"), USERPROFILE=str(self.root / "home"),
                   AELIX_CODING_AGENT_DIR=str(self.root / "agent"), AELIX_SETTINGS_PATH=str(self.root / "agent/settings.json"))
        holder = subprocess.Popen([EXECUTABLE, "--mode", "rpc", "--offline", "--no-extensions", "--session", native_file],
                                  cwd=self.root, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            holder.stdin.write(b'{"id":"ready","type":"get_state"}\n')
            holder.stdin.flush()
            self.assertIn(b'"ready"', await asyncio.to_thread(holder.stdout.readline))
            with self.assertRaises(RuntimeError) as caught:
                await self.client.request("session/load", {"cwd": str(self.root), "sessionId": self.session, "mcpServers": []}, timeout=60)
            error = json.loads(str(caught.exception))
            self.assertEqual((error["code"], error["data"]["lockedBy"], error["data"]["holder"], error["data"]["retryable"]),
                             (SESSION_LOCKED, "native", None, False))
        finally:
            holder.stdin.close()
            holder.wait(10)
        result = await self.client.request("session/load", {"cwd": str(self.root), "sessionId": self.session, "mcpServers": []}, timeout=60)
        self.assertIn("configOptions", result)

    async def test_standard_head_fork_opens_child_in_a_second_adapter(self):
        import sqlite3

        def session_file(identifier):
            with contextlib.closing(sqlite3.connect(self.root / "state/sessions.sqlite3")) as db:
                filename = db.execute("SELECT file FROM sessions WHERE id=?", (identifier,)).fetchone()[0]
            return Path(filename)

        await self.prompt("FIRST_PARENT_TURN", fork_head="first")
        self.model_gate = asyncio.Event()
        second = asyncio.create_task(self.prompt("SECOND_PARENT_TURN", fork_head="second"))
        fork_client = await AcpClient.start(self.root, EXECUTABLE)
        self.addAsyncCleanup(fork_client.close)
        params = self.fork_params("second")
        try:
            async with asyncio.timeout(30):
                while len(self.model_requests) < 2:
                    await asyncio.sleep(0.01)
            with self.assertRaisesRegex(RuntimeError, "-32000"):
                await fork_client.request("session/fork", params)
        finally:
            self.model_gate.set()
            await second

        try:
            parent = session_file(self.session)
            parent_bytes = parent.read_bytes()
            with self.assertRaisesRegex(RuntimeError, "fork_head_changed"):
                await fork_client.request("session/fork", self.fork_params("first"))
            self.assertEqual(set(parent.parent.glob("*.jsonl")), {parent})
            result = await fork_client.request("session/fork", params)
            fork_id = result["sessionId"]
            child = session_file(fork_id)
            parent_header = json.loads(parent_bytes.splitlines()[0])
            child_header = json.loads(child.read_text(encoding="utf-8").splitlines()[0])
            self.assertNotEqual(fork_id, self.session)
            self.assertNotEqual(child_header["id"], parent_header["id"])
            self.assertEqual(Path(child_header["parentSession"]), parent)
            self.assertEqual(parent.read_bytes(), parent_bytes)
            self.assertEqual(fork_client.text(), "")
            prompt = {"sessionId": fork_id, "prompt": [{"type": "text", "text": "CHILD_NEXT"}]}
            await fork_client.request("session/prompt", prompt)
            self.assertIn("SECOND_PARENT_TURN", json.dumps(self.model_requests[-1]["messages"]))
        finally:
            await fork_client.close()
        reopened = await AcpClient.start(self.root, EXECUTABLE)
        try:
            await reopened.request("session/load", {**params, "sessionId": fork_id})
            self.assertIn("SECOND_PARENT_TURN", reopened.text())
            self.assertIn("CHILD_NEXT", reopened.text())
            await reopened.request("session/prompt", {**prompt, "prompt": [{"type": "text", "text": "AFTER_RESTART"}]})
            self.assertIn("SECOND_PARENT_TURN", json.dumps(self.model_requests[-1]["messages"]))
            self.assertEqual(parent.read_bytes(), parent_bytes)
        finally:
            await reopened.close()

    async def test_standard_head_fork_refuses_an_older_live_adapter_lease(self):
        from aelix_acp.runtime.leases import acquire

        await self.prompt("PARENT_TURN", fork_head="parent")
        await self.client.close()
        self.client = await AcpClient.start(self.root, EXECUTABLE)
        params = self.fork_params("parent")
        with acquire(self.root / "state", self.session):
            with self.assertRaisesRegex(RuntimeError, "-33001"):
                await self.client.request("session/fork", params)
        result = await self.client.request("session/fork", params)
        self.assertNotEqual(result["sessionId"], self.session)

    def native_session_file(self, session_id):
        import sqlite3

        with contextlib.closing(sqlite3.connect(self.root / "state/sessions.sqlite3")) as db:
            return Path(db.execute("SELECT file FROM sessions WHERE id=?", (session_id,)).fetchone()[0])

    async def test_standard_head_fork_replays_text_from_two_large_images_after_restart(self):
        import base64
        import sqlite3
        import struct
        import zlib

        def chunk(kind, payload):
            return (struct.pack(">I", len(payload)) + kind + payload +
                    struct.pack(">I", zlib.crc32(kind + payload)))

        width, height = 1536, 1472
        png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) +
               chunk(b"IDAT", zlib.compress(bytes((1 + width * 3) * height), level=0)) + chunk(b"IEND", b""))
        image = base64.b64encode(png).decode()
        self.assertLess(len(png), 8 * 1024 * 1024)
        self.assertGreater(len(image) * 2, 16 * 1024 * 1024)
        await self.config("autoCompactionEnabled", "false")
        for label in ("IMAGE_ONE", "IMAGE_TWO"):
            await self.client.request("session/prompt", {"sessionId": self.session, "prompt": [
                {"type": "text", "text": label}, {"type": "image", "mimeType": "image/png", "data": image},
            ], "_meta": {"aelix/forkHead": label}}, timeout=90)

        parent = self.native_session_file(self.session)
        parent_bytes = parent.read_bytes()
        params = self.fork_params("IMAGE_TWO")
        fork = await self.client.request("session/fork", params, timeout=90)
        child = self.native_session_file(fork["sessionId"])
        self.assertNotEqual(child, parent)
        await self.client.close()
        self.client = await AcpClient.start(self.root, EXECUTABLE)
        await self.client.request("session/load", {**params, "sessionId": fork["sessionId"]}, timeout=90)
        self.assertIn("IMAGE_ONE", self.client.text())
        self.assertIn("IMAGE_TWO", self.client.text())
        await self.client.request("session/prompt", {"sessionId": fork["sessionId"], "prompt": [
            {"type": "text", "text": "CHILD_AFTER_IMAGES"},
        ]}, timeout=90)
        content_blocks = (block for message in self.model_requests[-1]["messages"]
                          if isinstance(message.get("content"), list) for block in message["content"])
        self.assertEqual(sum(block.get("type") == "image_url" for block in content_blocks), 2)
        self.assertEqual(parent.read_bytes(), parent_bytes)
        await self.client.close()
        # Exercise a completed projection with zero text rows. It must not
        # fall back to the oversized aggregate native messages response.
        with contextlib.closing(sqlite3.connect(self.root / "state/sessions.sqlite3")) as db, db:
            db.execute("DELETE FROM events WHERE session_id=?", (fork["sessionId"],))
        self.client = await AcpClient.start(self.root, EXECUTABLE)
        await self.client.request("session/load", {**params, "sessionId": fork["sessionId"]}, timeout=90)
        self.assertEqual(self.client.text(), "")

    async def test_standard_head_fork_rejects_missing_profile_on_every_open(self):
        import sqlite3

        await self.prompt("PARENT_TURN", fork_head="parent")
        folder = self.native_session_file(self.session).parent
        originals = set(folder.glob("*.jsonl*")) - set(folder.glob("*.lock"))
        with contextlib.closing(sqlite3.connect(self.root / "state/sessions.sqlite3")) as db, db:
            saved = json.loads(db.execute("SELECT value FROM preferences WHERE session_id=?", (self.session,)).fetchone()[0])
            saved["agent"] = "unavailable-fork-profile"
            db.execute("UPDATE preferences SET value=? WHERE session_id=?", (json.dumps(saved), self.session))
        params = self.fork_params("parent")
        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError, "profile_unavailable"):
                await self.client.request("session/fork", params)
            self.assertEqual(set(folder.glob("*.jsonl*")) - set(folder.glob("*.lock")), originals)
        result = await self.client.request("session/list", {"cwd": str(self.root)})
        self.assertEqual([row["sessionId"] for row in result["sessions"]], [self.session])

    async def test_standard_head_fork_removes_a_draft_after_worker_startup_fails(self):
        await self.prompt("PARENT_TURN", fork_head="parent")
        parent = self.native_session_file(self.session)
        original = parent.read_bytes()
        params = self.fork_params("parent", [
            {"name": "missing", "command": str(self.root / "missing-mcp-command"), "args": [], "env": []},
        ])
        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError, "MCP"):
                await self.client.request("session/fork", params, timeout=90)
            self.assertEqual(set(parent.parent.glob("*.jsonl")), {parent})
            self.assertEqual(list(parent.parent.glob("*.jsonl.tmp")), [])
        self.assertEqual(parent.read_bytes(), original)
        fork = await self.client.request("session/fork", {**params, "mcpServers": []})
        self.assertTrue(self.native_session_file(fork["sessionId"]).is_file())

    async def test_standard_head_fork_rejects_missing_reference_and_changed_native_snapshot(self):
        await self.prompt("PARENT_TURN", fork_head="parent")
        parent = self.native_session_file(self.session)
        await self.client.close()
        self.client = await AcpClient.start(self.root, EXECUTABLE)
        params = self.fork_params("parent")
        with self.assertRaisesRegex(RuntimeError, "fork_head_changed"):
            await self.client.request("session/fork", {**params, "_meta": {}})
        with parent.open("ab") as stream:
            stream.write(b'\n')
        with self.assertRaisesRegex(RuntimeError, "fork_head_changed"):
            await self.client.request("session/fork", params)
        self.assertEqual(set(parent.parent.glob("*.jsonl")), {parent})

    async def test_failure_after_native_dispatch_is_not_retryable(self):
        await self.config("autoRetryEnabled", "false")
        self.failures = 1
        with self.assertRaises(RuntimeError) as caught:
            await self.prompt("Respond")
        error = json.loads(str(caught.exception))
        self.assertEqual(error["code"], TURN_FAILED_AFTER_DISPATCH)
        self.assertEqual(error["data"], {"kind": "turn_failed_after_dispatch", "retryable": False})
        self.assertIn("overloaded fixture", error["message"])
        self.assertEqual(len(self.model_requests), 1)

    async def test_failure_before_native_dispatch_keeps_its_specific_code(self):
        with self.assertRaises(RuntimeError) as caught:
            await self.client.request("session/prompt", {"sessionId": self.session, "prompt": [{"type": "audio", "data": ""}]})
        self.assertEqual(json.loads(str(caught.exception))["code"], -32602)
        self.assertEqual(self.model_requests, [])

    async def test_extension_commands_and_dialogs_do_not_call_the_model(self):
        await self.prompt("/echo  raw")
        self.assertIn("echo: raw", self.client.text())
        self.client.dialog = lambda _request: {"value": "two"}
        await self.prompt("/choose")
        self.assertIn("selected:two", self.client.text())
        self.assertEqual(self.model_requests, [])

    async def test_unknown_slash_is_an_error_without_model_fallback(self):
        with self.assertRaisesRegex(RuntimeError, "Unknown slash"):
            await self.prompt("/unknown-command")
        self.assertEqual(self.model_requests, [])

    async def test_command_cancel_returns_without_waiting_for_the_dialog_deadline(self):
        task = asyncio.create_task(self.prompt("/wait"))
        await asyncio.sleep(0.1)
        await self.client.request("session/cancel", {"sessionId": self.session})
        self.assertEqual(await asyncio.wait_for(task, 3), {"stopReason": "cancelled"})

    async def test_duplicate_completed_turn_does_not_repeat_model_or_tools(self):
        await self.prompt("WRITE_TEST", "stable-request")
        count = len(self.model_requests)
        await self.prompt("WRITE_TEST", "stable-request")
        self.assertEqual(len(self.model_requests), count)

    async def test_process_restart_restores_history_and_model_selection(self):
        await self.config("model", "fixture/two")
        await self.prompt("Respond")
        await self.prompt("/help")
        await self.client.close()
        self.client = await AcpClient.start(self.root, EXECUTABLE)
        result = await self.client.request("session/load", {"cwd": str(self.root), "sessionId": self.session, "mcpServers": []})
        self.assertEqual(result["models"]["currentModelId"], "fixture/two")
        self.assertIn("NATIVE_OK", self.client.text())
        self.assertIn("/help", self.client.text())
