"""Real Aelix + ACP + local model endpoint; no provider credentials or internet."""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from support import AcpClient
from aelix_acp.wire.errors import SESSION_LOCKED

EXECUTABLE = os.environ.get("AELIX_ACP_TEST_AELIX")


@unittest.skipUnless(EXECUTABLE, "Set AELIX_ACP_TEST_AELIX to a runtime-control-v1 Aelix executable")
class NativeRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="acp-native-", dir="/tmp" if os.name != "nt" else None)
        self.root = Path(self.temp.name).resolve()
        (self.root / "home").mkdir()
        (self.root / "agent").mkdir()
        self.model_requests = []
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
            if self.model_gate is not None:
                await self.model_gate.wait()
            messages = request.get("messages", [])
            last = messages[-1] if messages else {}
            content = str(last.get("content", ""))
            if last.get("role") == "user" and "WRITE_TEST" in content:
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

    async def prompt(self, text, key=None):
        return await self.client.request("session/prompt", {"sessionId": self.session,
            "prompt": [{"type": "text", "text": text}], **({"_meta": {"aelix/requestId": key}} if key else {})})

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

    async def test_removed_profile_recovers_with_explicit_approval_mode(self):
        profiles = self.root / "agent/agents"
        profiles.mkdir()
        profile = profiles / "reviewer.md"
        profile.write_text("---\nname: reviewer\ndescription: Review\ninherit_extensions: true\n---\nReview carefully.\n", encoding="utf-8")
        await self.manage("agents/use", {"name": "reviewer"})
        await self.config("permission_mode", "yolo")
        await self.prompt("Respond")
        await self.client.close()
        profile.unlink()
        self.client = await AcpClient.start(self.root, EXECUTABLE)
        result = await self.client.request("session/load", {"cwd": str(self.root), "sessionId": self.session, "mcpServers": []})
        self.assertEqual(result["modes"]["currentModeId"], "default")
        self.assertTrue(result["_meta"]["aelix/recoveryWarnings"])

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

    async def test_launch_extension_cannot_be_disabled_by_the_session(self):
        extension = str((self.root / "commands.py").resolve())
        with self.assertRaisesRegex(RuntimeError, "launch configuration"):
            await self.manage("extensions/action", {"action": "disable", "target": extension})
        await self.prompt("/echo still-loaded")
        self.assertIn("echo:still-loaded", self.client.text())
        loaded = {entry.get("source") or entry.get("name") for entry in (await self.manage("resources/list"))["extensions"]}
        self.assertIn(extension, loaded)

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

    async def test_saved_restrictions_survive_a_missing_profile_without_widening(self):
        await self.prompt("Respond")
        await self.client.close()
        import sqlite3
        with contextlib.closing(sqlite3.connect(self.root / "state/sessions.sqlite3")) as db, db:
            value = json.loads(db.execute("SELECT value FROM preferences WHERE session_id=?", (self.session,)).fetchone()[0])
            value.update(agent="restricted-fixture", activeTools=[], permission_mode="yolo")
            db.execute("UPDATE preferences SET value=? WHERE session_id=?", (json.dumps(value), self.session))
        self.client = await AcpClient.start(self.root, EXECUTABLE)
        result = await self.client.request("session/load", {"cwd": str(self.root), "sessionId": self.session, "mcpServers": []})
        self.assertEqual(result["modes"]["currentModeId"], "default")
        self.assertEqual(result["_meta"]["aelix/recoveryWarnings"][0]["kind"], "agent_unavailable")
        self.assertEqual((await self.manage("permissions/get"))["mode"], "default")
        self.client.permission = "reject-once"
        await self.prompt("Respond")
        self.assertFalse(self.model_requests[-1].get("tools"), self.model_requests[-1].get("tools"))

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
