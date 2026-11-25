"""Real Aelix + ACP + local model endpoint; no provider credentials or internet."""
import asyncio
import contextlib
import json
import os
import signal
from pathlib import Path
import sqlite3
import sys
import tempfile
import textwrap
import unittest

from support import AcpClient, process_alive
from aelix_acp.runtime.leases import acquire as acquire_lease
from aelix_acp.wire.errors import ProtocolError, SESSION_LOCKED, TURN_FAILED_AFTER_DISPATCH

EXECUTABLE = os.environ.get("AELIX_ACP_TEST_AELIX")
SELECTOR_PROFILE = (
    "---\nname: restricted\ndescription: Restricted\nprovider: fixture\nmodel: two\n"
    "thinking: 'off'\ntools: [read]\n---\nRead only.\n"
)
REJECT_RESTORED_MODEL = """
def setup(api):
    async def reject(event, ctx):
        if event.model.id == "one" and getattr(event.previous_model, "id", None) == "two":
            raise RuntimeError("fixture refused selector restoration")
    api.on("model_select", reject)
"""


PUBLICATION_PROBE = """import asyncio
import json
from pathlib import Path
import sys
from unittest.mock import patch

from aelix_acp.server import main
from aelix_acp.session import Session
from aelix_acp.runtime import store

root = Path.cwd()
original_save = Session.save
original_write = store._write_store


async def save(session, *args, **kwargs):
    armed = root / "publication-armed"
    if kwargs.get("live") and armed.exists():
        mode = armed.read_text(encoding="utf-8")
        caller = sys._getframe(1).f_code.co_name
        if caller == mode:
            armed.unlink()
            candidate = root / "publication-candidate.tmp"
            candidate.write_text(json.dumps([session.id, session.proc.pid]), encoding="utf-8")
            candidate.replace(root / "publication-candidate")
            async with asyncio.timeout(15):
                while not (root / "publication-enter").exists():
                    await asyncio.sleep(0.01)
            (root / "publication-writing").touch()
    return await original_save(session, *args, **kwargs)


def write(directory, operation, args, check_commit):
    if (check_commit is not None and operation is store.save_session
            and args[1].get("extensionOverrides") and (root / "publication-pause-commit").exists()):
        import time
        original_check = check_commit

        def check_commit():
            original_check()
            (root / "publication-admitted").touch()
            deadline = time.monotonic() + 15
            while not (root / "publication-commit").exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError("Publication test did not release commit")
                time.sleep(0.01)
    return original_write(directory, operation, args, check_commit)


with patch.object(Session, "save", save), patch.object(store, "_write_store", write):
    main()
"""


@unittest.skipUnless(EXECUTABLE, "Set AELIX_ACP_TEST_AELIX to a runtime-control-v1 Aelix executable")
class NativeRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="acp-native-", dir="/tmp" if os.name != "nt" else None)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "home").mkdir()
        (self.root / "agent").mkdir()
        self.model_requests = []
        self.model_authorizations = []
        self.write_path = self.root / "approved.txt"
        self.failures = 0
        self.model_gate = None
        self.prompt_tokens = 23
        self.listener = await asyncio.start_server(self.model, "127.0.0.1", 0)
        port = self.listener.sockets[0].getsockname()[1]
        models = {"providers": {"fixture": {"baseUrl": f"http://127.0.0.1:{port}/v1", "api": "openai-completions",
            "apiKey": "synthetic-fixture-key", "models": [
                {"id": "one", "name": "Fixture One", "reasoning": True, "input": ["text", "image"],
                 "contextWindow": 32768, "maxTokens": 2048},
                {"id": "two", "name": "Fixture Two", "reasoning": False, "input": ["text"],
                 "contextWindow": 32768, "maxTokens": 2048}]}}}
        (self.root / "agent/models.json").write_text(json.dumps(models))
        (self.root / "agent/settings.json").write_text(json.dumps({"defaultProvider": "fixture", "defaultModel": "one",
                                                                  "compaction": {"keepRecentTokens": 0}}))
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
                    "model": request.get("model", "one"), "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                    **({"usage": {"prompt_tokens": self.prompt_tokens, "completion_tokens": 3,
                                  "total_tokens": self.prompt_tokens + 3}} if finish else {})}) + "\n\n"
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

    def publication_probe(self):
        script = self.root / "publication-probe.py"
        script.write_text(PUBLICATION_PROBE, encoding="utf-8")
        return (str(script),)

    async def publication_marker(self, name):
        path = self.root / f"publication-{name}"
        async with asyncio.timeout(15):
            while not path.exists():
                await asyncio.sleep(0.01)
        return path

    @contextlib.asynccontextmanager
    async def hold_final_save(self, caller, action):
        (self.root / "publication-armed").write_text(caller, encoding="utf-8")
        request = asyncio.create_task(action)

        async def settle_request():
            (self.root / "publication-enter").touch()
            (self.root / "publication-commit").touch()
            await asyncio.gather(request, return_exceptions=True)

        self.addAsyncCleanup(settle_request)
        path = await self.publication_marker("candidate")
        session_id, pid = json.loads(path.read_text(encoding="utf-8"))
        with contextlib.closing(sqlite3.connect(self.root / "state/sessions.sqlite3", timeout=0)) as db:
            async with asyncio.timeout(8):
                while True:
                    try:
                        db.execute("BEGIN IMMEDIATE")
                        break
                    except sqlite3.OperationalError as error:
                        if error.sqlite_errorcode & 255 != sqlite3.SQLITE_BUSY:
                            raise
                        await asyncio.sleep(0.01)
            (self.root / "publication-enter").touch()
            try:
                await self.publication_marker("writing")
                yield request, db, session_id, pid
            finally:
                db.rollback()

    async def terminate_publication_worker(self, pid):
        if os.name == "nt":
            # The PID is the adapter-owned launch gate. End its native child as well.
            taskkill = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "taskkill.exe"
            killer = await asyncio.create_subprocess_exec(str(taskkill), "/PID", str(pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            self.assertEqual(await asyncio.wait_for(killer.wait(), 5), 0)
        else:
            os.kill(pid, signal.SIGKILL)
        async with asyncio.timeout(8):
            while process_alive(pid):
                await asyncio.sleep(0.01)

    async def test_worker_exit_during_final_save_keeps_lease_and_refuses_registration(self):
        for mode in ("restart", "load", "fork", "new"):
            with self.subTest(mode=mode):
                await self.client.close()
                for marker in self.root.glob("publication-*"):
                    marker.unlink()
                self.client = await AcpClient.start(self.root, EXECUTABLE, entry=self.publication_probe())
                if mode != "new":
                    self.session = await self.open_managed(self.client)
                if mode == "fork":
                    await self.prompt("Fork parent", fork_head="publication")
                    action = self.client.request("session/fork", self.fork_params("publication"))
                    caller = "fork_head"
                elif mode == "load":
                    await self.client.request("session/close", {"sessionId": self.session})
                    action = self.client.request("session/load", {"sessionId": self.session, "cwd": str(self.root), "mcpServers": []})
                    caller = "dispatch_request"
                elif mode == "new":
                    action = self.client.request("session/new", {"cwd": str(self.root), "mcpServers": []})
                    caller = "start"
                else:
                    action = self.manage("workspace/trust", {"trusted": False})
                    caller = "restart"
                async with self.hold_final_save(caller, action) as (request, db, session_id, pid):
                    target = None
                    if mode == "fork":
                        (target,) = db.execute("SELECT file FROM pending_forks WHERE session_id=?", (session_id,)).fetchone()
                    await self.terminate_publication_worker(pid)
                    await self.client.request("session/list", {}, timeout=8)
                    with self.assertRaises(ProtocolError) as busy:
                        with acquire_lease(str(self.root / "state"), session_id):
                            pass
                    self.assertEqual(busy.exception.code, SESSION_LOCKED)
                    self.assertFalse(request.done())
                with self.assertRaises(RuntimeError) as error:
                    await request
                self.assertEqual(json.loads(str(error.exception))["data"]["kind"], "session_worker_unavailable")
                with acquire_lease(str(self.root / "state"), session_id):
                    pass
                if mode in {"fork", "new"}:
                    with contextlib.closing(sqlite3.connect(self.root / "state/sessions.sqlite3")) as db:
                        for table, key in [("sessions", "id"), ("pending_forks", "session_id")]:
                            self.assertIsNone(db.execute(f"SELECT 1 FROM {table} WHERE {key}=?", (session_id,)).fetchone())
                if target is not None:
                    self.assertFalse(Path(target).exists())

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

    async def no_default_model_session(self):
        """A new adapter and session with a configured provider but no default model (ACP #62)."""
        await self.client.close()
        (self.root / "agent/settings.json").write_text(json.dumps({"compaction": {"keepRecentTokens": 0}}))
        self.client = await AcpClient.start(self.root, EXECUTABLE)
        created = await self.client.request("session/new", {"cwd": str(self.root), "mcpServers": []}, timeout=120)
        self.session = created["sessionId"]
        return created

    def published_options(self):
        return [event["params"]["update"]["configOptions"] for event in self.client.updates
                if event.get("method") == "session/update"
                and event["params"]["update"]["sessionUpdate"] == "config_option_update"]

    async def test_no_default_model_advertises_available_models_and_applies_the_choice(self):
        created = await self.no_default_model_session()
        options = {option["id"]: option for option in created["configOptions"]}
        self.assertEqual((options["model"]["category"], options["model"]["currentValue"]), ("model", ""))
        self.assertEqual([entry["value"] for entry in options["model"]["options"]], ["fixture/one", "fixture/two"])
        self.assertEqual(created["models"]["currentModelId"], "")
        self.assertNotIn("thinking_level", options)
        self.assertNotIn("synthetic-fixture-key", json.dumps(created))

        with self.assertRaises(RuntimeError) as refused:
            await self.prompt("Respond")
        error = json.loads(str(refused.exception))
        self.assertEqual(error["code"], -32004)
        self.assertEqual(error["data"], {"kind": "model_selection_required", "configId": "model",
                                         "availableModels": 2, "retryable": False})
        self.assertEqual(self.model_requests, [])

        result = await self.config("model", "fixture/one")
        options = {option["id"]: option for option in result["configOptions"]}
        self.assertEqual(options["model"]["currentValue"], "fixture/one")
        self.assertEqual((options["thinking_level"]["category"], options["thinking_level"]["currentValue"]),
                         ("thought_level", "off"))
        self.assertEqual([entry["value"] for entry in options["thinking_level"]["options"]],
                         ["off", "minimal", "low", "medium", "high"])
        self.assertEqual(self.published_options()[-1], result["configOptions"])
        await self.config("thinking_level", "high")
        self.assertEqual(await self.prompt("Respond"), {"stopReason": "end_turn"})
        self.assertEqual([request["model"] for request in self.model_requests], ["one"])

    async def test_no_default_model_choice_survives_load_worker_restart_and_fork(self):
        await self.no_default_model_session()
        await self.config("model", "fixture/two")
        await self.client.close()

        # Native resumes a file with no messages on its placeholder; ACP restores the choice.
        self.client = await AcpClient.start(self.root, EXECUTABLE)
        loaded = await self.client.request(
            "session/load", {"cwd": str(self.root), "sessionId": self.session, "mcpServers": []}, timeout=120)
        self.assertEqual(loaded["models"]["currentModelId"], "fixture/two")
        self.assertNotIn("aelix/recoveryWarnings", loaded["_meta"])

        await self.manage("agents/enable", {"enabled": True})  # Replaces the native worker.
        runtime = await self.client.request("_aelix/capabilities", {"sessionId": self.session})
        self.assertEqual({option["id"]: option["currentValue"] for option in runtime["configOptions"]}["model"],
                         "fixture/two")

        self.assertEqual(await self.prompt("NO_DEFAULT_PARENT", fork_head="no-default"), {"stopReason": "end_turn"})
        forked = await self.client.request("session/fork", self.fork_params("no-default"), timeout=120)
        self.assertEqual(forked["models"]["currentModelId"], "fixture/two")
        await self.client.request("session/prompt", {"sessionId": forked["sessionId"],
                                                     "prompt": [{"type": "text", "text": "Respond"}]})
        self.assertEqual([request["model"] for request in self.model_requests], ["two", "two"])

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

    async def test_route_free_startup_profile_keeps_session_selectors(self):
        profiles = self.root / "agent/agents"
        profiles.mkdir()
        profile = profiles / "read-only.md"
        profile.write_text("---\nname: read-only\ndescription: Read only\ntools: [read]\n---\nRead only.\n",
                           encoding="utf-8")
        launch = ["--provider", "fixture", "--model", "two"]
        for prefix in (["--agent", "read-only"], ["--agent-file", str(profile)]):
            with self.subTest(prefix=prefix[0]):
                await self.client.close()
                self.client = await AcpClient.start(self.root, EXECUTABLE, prefix=prefix, adapter_args=launch)
                created = await self.client.request("session/new", {"cwd": str(self.root), "mcpServers": []})
                self.session = created["sessionId"]
                self.assertEqual(created["models"]["currentModelId"], "fixture/two")
                await self.config("model", "fixture/one")
                await self.config("thinking_level", "high")
                await self.prompt("Seed with the selected model")
                await self.client.close()

                self.client = await AcpClient.start(self.root, EXECUTABLE, prefix=prefix, adapter_args=launch)
                restored = await self.client.request("session/load", {
                    "cwd": str(self.root), "sessionId": self.session, "mcpServers": [],
                })
                options = {option["id"]: option["currentValue"] for option in restored["configOptions"]}
                self.assertEqual((options["model"], options["thinking_level"]), ("fixture/one", "high"))
                self.assertEqual((await self.manage("resources/list"))["activeTools"], ["read"])
                await self.prompt("Use the resumed model")
                self.assertEqual(self.model_requests[-1]["model"], "one")

    async def test_explicit_native_route_is_preserved_on_resume(self):
        await self.prompt("Record the first model")
        await self.client.close()
        self.client = await AcpClient.start(
            self.root, EXECUTABLE, prefix=["--provider", "fixture", "--model", "two"])
        restored = await self.client.request("session/load", {
            "cwd": str(self.root), "sessionId": self.session, "mcpServers": [],
        })
        self.assertEqual(restored["models"]["currentModelId"], "fixture/two")
        await self.prompt("Use the explicit native route")
        self.assertEqual([request["model"] for request in self.model_requests], ["one", "two"])

    async def test_rejected_model_restore_leaves_native_history_intact(self):
        await self.check_rejected_selector_restore("set_model", REJECT_RESTORED_MODEL)

    async def test_rejected_thinking_restore_leaves_native_history_intact(self):
        hook = """
def setup(api):
    async def reject(event, ctx):
        if event.level == "high" and event.previous_level == "off":
            raise RuntimeError("fixture refused selector restoration")
    api.on("thinking_level_select", reject)
"""
        await self.check_rejected_selector_restore("set_thinking_level", hook)

    async def check_rejected_selector_restore(self, command, script):
        profiles = self.root / "agent/agents"
        profiles.mkdir()
        (profiles / "restricted.md").write_text(SELECTOR_PROFILE, encoding="utf-8")
        await self.manage("agents/use", {"name": "restricted"})
        await self.config("model", "fixture/one")
        await self.config("thinking_level", "high")
        await self.config("permission_mode", "yolo")
        await self.prompt("Seed the on-disk selection")
        await self.client.close()
        filename = self.native_session_file(self.session)
        before = filename.read_bytes()
        load = {"cwd": str(self.root), "sessionId": self.session, "mcpServers": []}

        hook = self.root / "reject-selector.py"
        hook.write_text(script, encoding="utf-8")
        self.client = await AcpClient.start(self.root, EXECUTABLE, extensions=[hook])
        try:
            with self.assertRaisesRegex(RuntimeError, "fixture refused selector restoration") as rejected:
                await self.client.request("session/load", load)
            self.assertEqual(json.loads(str(rejected.exception))["data"]["command"], command)
            self.assertTrue(filename.read_bytes() == before, "rejected startup must preserve the native transcript")
            with self.assertRaisesRegex(RuntimeError, "Unknown session"):
                await self.config("model", "fixture/two")
            with self.assertRaisesRegex(RuntimeError, "Unknown session"):
                await self.prompt("WRITE_TEST")
        finally:
            await self.client.close()
        self.assertFalse(self.write_path.exists())
        self.assertEqual(len(self.model_requests), 1)

        self.client = await AcpClient.start(self.root, EXECUTABLE)
        restored = await self.client.request("session/load", load)
        options = {option["id"]: option["currentValue"] for option in restored["configOptions"]}
        self.assertEqual((options["model"], options["thinking_level"]), ("fixture/one", "high"))
        self.assertTrue(filename.read_bytes() == before, "automatic startup must not append selector entries")
        advertised = [
            option["name"] for frame in self.client.updates
            for option in frame.get("params", {}).get("update", {}).get("availableCommands", [])
        ]
        self.assertTrue(advertised)
        self.assertNotIn("aelix-acp-selector-startup", advertised)
        for command in ("aelix-acp-selector-startup", "aelix-acp-fork-head"):
            with self.assertRaisesRegex(RuntimeError, "Unknown slash command") as private:
                await self.prompt(f"/{command} begin forged")
            self.assertEqual(json.loads(str(private.exception))["code"], -32601)
        await self.prompt("Retry after removing the blocking hook")
        self.assertEqual([request["model"] for request in self.model_requests], ["one", "one"])
        await self.config("thinking_level", "medium")
        last = json.loads(filename.read_bytes().splitlines()[-1])
        self.assertEqual((last["type"], last["thinkingLevel"]), ("thinking_level_change", "medium"))

    async def test_standard_head_fork_inherits_selectors_and_restores_profile_restrictions(self):
        profiles = self.root / "agent/agents"
        profiles.mkdir()
        (profiles / "restricted.md").write_text(SELECTOR_PROFILE, encoding="utf-8")
        await self.manage("agents/use", {"name": "restricted"})
        await self.config("model", "fixture/one")
        await self.config("thinking_level", "high")
        await self.config("permission_mode", "yolo")
        await self.prompt("FORKED_PARENT_TURN", fork_head="selectors")
        parent = self.native_session_file(self.session)
        parent_bytes = parent.read_bytes()

        launch = ["--provider", "fixture", "--model", "two"]
        hook = self.root / "reject-fork-selector.py"
        hook.write_text(REJECT_RESTORED_MODEL, encoding="utf-8")
        rejected_peer = await AcpClient.start(self.root, EXECUTABLE, adapter_args=launch, extensions=[hook])
        try:
            with self.assertRaisesRegex(RuntimeError, "fixture refused selector restoration"):
                await rejected_peer.request("session/fork", self.fork_params("selectors"))
            self.assertEqual(set(parent.parent.glob("*.jsonl")), {parent})
            visible = await rejected_peer.request("session/list", {"cwd": str(self.root)})
            self.assertEqual([row["sessionId"] for row in visible["sessions"]], [self.session])
        finally:
            await rejected_peer.close()

        peer = await AcpClient.start(self.root, EXECUTABLE, adapter_args=launch)
        try:
            forked = await peer.request("session/fork", self.fork_params("selectors"))
            child = forked["sessionId"]
            options = {option["id"]: option["currentValue"] for option in forked["configOptions"]}
            self.assertEqual((options["model"], options["thinking_level"]), ("fixture/one", "high"))
            self.assertEqual(forked["modes"]["currentModeId"], "yolo")
        finally:
            await peer.close()

        peer = await AcpClient.start(self.root, EXECUTABLE, adapter_args=launch)
        try:
            reopened = await peer.request("session/load", {
                "cwd": str(self.root), "sessionId": child, "mcpServers": [],
            })
            options = {option["id"]: option["currentValue"] for option in reopened["configOptions"]}
            self.assertEqual((options["model"], options["thinking_level"]), ("fixture/one", "high"))
            await peer.request("session/prompt", {"sessionId": child, "prompt": [{"type": "text", "text": "WRITE_TEST"}]})
            request = self.model_requests[-1]
            self.assertEqual(request["model"], "one")
            self.assertIn("FORKED_PARENT_TURN", json.dumps(request["messages"]))
            self.assertEqual([tool["function"]["name"] for tool in request["tools"]], ["read"])
            self.assertFalse(self.write_path.exists())
            self.assertTrue(parent.read_bytes() == parent_bytes)
        finally:
            await peer.close()

    async def test_profile_restore_keeps_native_selectors_and_enforces_tool_filter(self):
        profiles = self.root / "agent/agents"
        profiles.mkdir()
        (profiles / "restricted.md").write_text(SELECTOR_PROFILE, encoding="utf-8")
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
        profiles = self.root / "agent/agents"
        profiles.mkdir()
        (profiles / "read-only.md").write_text(
            "---\nname: read-only\ndescription: Read only\ntools: [read]\n---\nRead only.\n", encoding="utf-8")
        self.client = await AcpClient.start(
            self.root, EXECUTABLE, prefix=["--agent", "read-only", "--api-key", typed_key], adapter_args=launch)
        restored = await self.client.request("session/load", {
            "cwd": str(self.root), "sessionId": self.session, "mcpServers": [],
        })
        self.assertEqual(restored["models"]["currentModelId"], "fixture/one")
        self.assertNotIn(typed_key, json.dumps(restored))
        self.assertEqual((await self.manage("resources/list"))["activeTools"], ["read"])
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

    def context_updates(self, kind):
        return [message["params"]["update"] for message in self.client.updates
                if message.get("method") == "session/update" and message["params"]["update"]["sessionUpdate"] == kind]

    async def test_manual_compaction_updates_context_and_preserves_native_history(self):
        for text in ("Remember the first detail.", "Remember the next detail.", "Remember the latest detail."):
            await self.prompt(text)
        known = self.context_updates("usage_update")[-1]
        self.assertEqual((known["used"], known["size"]), (26, 32768))
        self.assertNotIn("cost", known)

        before = len(self.model_requests)
        self.assertEqual(await self.prompt("/compact Preserve only decisions"), {"stopReason": "end_turn"})
        progress = self.context_updates("tool_call")[-1]
        result = self.context_updates("tool_call_update")[-1]
        self.assertEqual(result["toolCallId"], progress["toolCallId"])
        self.assertEqual(result["_meta"]["aelix/compaction"]["state"], "completed")
        self.assertGreater(result["_meta"]["aelix/compaction"]["tokensBefore"], 0)
        summarized_request = json.dumps(self.model_requests[before:])
        self.assertIn("Preserve only decisions", summarized_request)
        self.assertNotIn("/compact", summarized_request)
        self.assertEqual(self.context_updates("usage_update")[-1]["_meta"], {"aelix/contextUnknown": True})

        await self.client.close()
        self.client = await AcpClient.start(self.root, EXECUTABLE)
        await self.client.request("session/load", {"cwd": str(self.root), "sessionId": self.session, "mcpServers": []})
        self.assertIn("Remember the first detail.", self.client.text())
        self.assertEqual(self.context_updates("usage_update")[-1]["_meta"], {"aelix/contextUnknown": True})
        await self.prompt("Continue after compaction")
        self.assertEqual(self.context_updates("usage_update")[-1], known)

    async def test_compaction_with_no_history_reports_no_work_without_a_model_request(self):
        self.assertEqual(await self.prompt("/compact"), {"stopReason": "end_turn"})
        self.assertEqual(self.model_requests, [])
        self.assertIn("Nothing to compact", self.client.text())
        progress = self.context_updates("tool_call_update")[-1]
        self.assertEqual(progress["_meta"]["aelix/compaction"], {"reason": "manual", "state": "failed"})

    async def test_compaction_deadline_reports_unknown_and_reopens_the_same_session_without_replay(self):
        import sqlite3
        await self.config("permission_mode", "yolo")
        await self.prompt("Prior context must remain")
        await self.prompt("More prior context")
        await self.client.close()
        self.client = await AcpClient.start(self.root, EXECUTABLE, adapter_args=["--command-timeout", "1"])
        await self.client.request("session/load", {"cwd": str(self.root), "sessionId": self.session, "mcpServers": []})
        def session_file():
            with contextlib.closing(sqlite3.connect(self.root / "state/sessions.sqlite3")) as db:
                return db.execute("SELECT file FROM sessions WHERE id=?", (self.session,)).fetchone()[0]
        saved_file = session_file()
        self.assertTrue(Path(saved_file).is_file())
        self.model_gate = asyncio.Event()
        try:
            with self.assertRaisesRegex(RuntimeError, "compaction outcome is unknown") as caught:
                await self.prompt("/compact", "deadline-compact")
        finally:
            self.model_gate.set()
        self.assertEqual(json.loads(str(caught.exception))["code"], TURN_FAILED_AFTER_DISPATCH)
        self.assertEqual(self.context_updates("tool_call_update")[-1]["_meta"]["aelix/compaction"]["state"], "unknown")
        count = len(self.model_requests)
        with self.assertRaisesRegex(RuntimeError, "unresolved outcome"):
            await self.prompt("/compact", "deadline-compact")

        profiles = self.root / "agent/agents"
        profiles.mkdir(exist_ok=True)
        (profiles / "current-owner.md").write_text(
            "---\nname: current-owner\ndescription: Updated by the current owner\ntools: [read]\n---\nKeep the session."
        )
        contender = await AcpClient.start(self.root, EXECUTABLE)
        resume_prompt = "Continue this same session"
        resume_key = "retry-after-owner-releases"
        try:
            await contender.request("session/load", {"cwd": str(self.root), "sessionId": self.session, "mcpServers": []})
            await contender.request("_aelix/manage", {"sessionId": self.session, "action": "agents/use",
                                                     "arguments": {"name": "current-owner"}})
            await contender.request("session/set_config_option", {"sessionId": self.session,
                "configId": "permission_mode", "value": "default"})
            with self.assertRaises(RuntimeError) as locked:
                await self.prompt(resume_prompt, resume_key)
            self.assertEqual(json.loads(str(locked.exception))["code"], SESSION_LOCKED)
            with self.assertRaisesRegex(RuntimeError, "unresolved outcome"):
                await self.prompt("/compact", "deadline-compact")
            with contextlib.closing(sqlite3.connect(self.root / "state/sessions.sqlite3")) as db:
                saved = json.loads(db.execute("SELECT value FROM preferences WHERE session_id=?", (self.session,)).fetchone()[0])
            self.assertEqual((saved["permission_mode"], saved["agent"]), ("default", "current-owner"))
        finally:
            await contender.close()

        self.assertEqual(await self.prompt(resume_prompt, resume_key), {"stopReason": "end_turn"})
        self.assertEqual(await self.prompt(resume_prompt, resume_key), {"stopReason": "end_turn"})
        self.assertEqual(session_file(), saved_file)
        self.assertEqual((await self.manage("permissions/get"))["mode"], "default")
        self.assertEqual((await self.manage("resources/list"))["activeTools"], ["read"])
        self.assertEqual(len(self.model_requests), count + 1)
        self.assertIn("Prior context must remain", json.dumps(self.model_requests[-1]))

    async def test_threshold_compaction_finishes_before_the_deduplicated_turn_returns(self):
        await self.prompt("Earlier context")
        await self.prompt("Recent context")
        self.prompt_tokens = 18000
        self.assertEqual(await self.prompt("Trigger threshold compaction", "threshold-turn"), {"stopReason": "end_turn"})
        self.assertEqual(self.context_updates("tool_call")[-1]["_meta"]["aelix/compaction"],
                         {"reason": "threshold", "state": "running"})
        completed = self.context_updates("tool_call_update")[-1]["_meta"]["aelix/compaction"]
        self.assertEqual((completed["reason"], completed["state"]), ("threshold", "completed"))
        requests = len(self.model_requests)
        await self.prompt("Trigger threshold compaction", "threshold-turn")
        self.assertEqual(len(self.model_requests), requests)

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
        meta = "aelix/transcript"
        self.client = await AcpClient.start(self.root, EXECUTABLE, capabilities={"_meta": {meta: {"version": 1}}})
        result = await self.client.request("session/load", {"cwd": str(self.root), "sessionId": self.session, "mcpServers": []})
        self.assertEqual(result["models"]["currentModelId"], "replacement/one")
        self.assertTrue(result["_meta"]["aelix/recoveryWarnings"])
        marker = result["_meta"].get(meta)
        if marker is not None or os.environ.get("AELIX_ACP_REQUIRE_TRANSCRIPT") == "1":
            self.assertIsNotNone(marker)
            updates = [event["params"]["update"] for event in self.client.updates if event["method"] == "session/update"]
            display = [update for update in updates if meta in update.get("_meta", {})]
            warnings = [update for update in display if update.get("content", {}).get("text", "").startswith("Session recovery: ")]
            self.assertTrue(warnings)
            self.assertGreater(marker["totalFrames"], marker["totalMessages"])
            self.assertEqual(marker["totalFrames"], len(display))

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
        await self.assert_write_approval()

    @unittest.skipUnless(os.name == "posix", "POSIX Unix approval socket")
    async def test_long_tmpdir_uses_fallback_socket_for_approval_and_close(self):
        tmpdir = self.root / ("t" * (139 - len(os.fsencode(self.root))))
        tmpdir.mkdir()
        self.assertEqual(len(os.fsencode(tmpdir)), 140)
        extension = self.root / "socket_probe.py"
        extension.write_text(textwrap.dedent('''\
            import os
            from pathlib import Path

            def setup(api):
                Path("approval-socket.txt").write_text(os.environ["AELIX_ACP_SOCKET"], encoding="utf-8")
            '''), encoding="utf-8")

        await self.client.close()
        self.client = await AcpClient.start(self.root, EXECUTABLE, extensions=[extension],
                                            extra_env={"TMPDIR": str(tmpdir)})
        created = await self.client.request("session/new", {"cwd": str(self.root)}, timeout=120)
        self.session = created["sessionId"]
        socket_path = Path((self.root / "approval-socket.txt").read_text(encoding="utf-8"))
        self.assertEqual(socket_path.parent.parent, Path("/tmp"))
        self.assertTrue(socket_path.is_socket())

        await self.assert_write_approval()
        self.assertEqual(await self.client.request("session/close", {"sessionId": self.session}), {})
        self.assertFalse(socket_path.parent.exists())

    async def assert_write_approval(self):
        await self.prompt("WRITE_TEST")
        self.assertEqual(self.write_path.read_text(encoding="utf-8"), "approved write")
        self.write_path.unlink()
        self.client.permission = "reject-once"
        await self.prompt("WRITE_TEST")
        self.assertFalse(self.write_path.exists())
        self.assertEqual(sum(req["method"] == "session/request_permission" for req in self.client.requests), 2)

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

    async def test_standard_close_releases_the_native_writer_after_the_active_turn(self):
        self.model_gate = asyncio.Event()
        turn = asyncio.create_task(self.prompt("Persist this turn before closing"))
        try:
            async with asyncio.timeout(30):
                while not self.model_requests:
                    await asyncio.sleep(0.01)
            with self.assertRaises(RuntimeError) as busy:
                await self.client.request("session/close", {"sessionId": self.session})
            self.assertEqual(json.loads(str(busy.exception))["code"], -32000)
        finally:
            self.model_gate.set()
            await turn

        with contextlib.closing(sqlite3.connect(self.root / "state/sessions.sqlite3")) as db:
            native_file = db.execute("SELECT file FROM sessions WHERE id=?", (self.session,)).fetchone()[0]
        saved = Path(native_file).read_bytes()
        self.assertEqual(await self.client.request("session/close", {"sessionId": self.session}), {})
        env = {key: os.environ[key] for key in ["PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"] if key in os.environ}
        env.update(HOME=str(self.root / "home"), USERPROFILE=str(self.root / "home"),
                   AELIX_CODING_AGENT_DIR=str(self.root / "agent"), AELIX_SETTINGS_PATH=str(self.root / "agent/settings.json"))
        cli = await asyncio.create_subprocess_exec(
            EXECUTABLE, "--mode", "rpc", "--offline", "--no-extensions", "--session", native_file,
            cwd=self.root, env=env, stdin=-1, stdout=-1, stderr=-1,
        )
        try:
            cli.stdin.write(b'{"id":"ready","type":"get_state"}\n')
            await cli.stdin.drain()
            response = json.loads(await asyncio.wait_for(cli.stdout.readline(), 10))
            self.assertEqual((response["id"], response["success"]), ("ready", True))
            self.assertEqual(Path(native_file).read_bytes(), saved)
        finally:
            cli.stdin.close()
            try:
                await asyncio.wait_for(cli.wait(), 10)
            except TimeoutError:
                cli.kill()
                await cli.wait()
        result = await self.client.request("session/load", {"cwd": str(self.root), "sessionId": self.session})
        self.assertIn("configOptions", result)

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

        reopened = await AcpClient.start(self.root, EXECUTABLE)
        self.addAsyncCleanup(reopened.close)
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
            child_bytes = child.read_bytes()
            # A second ACP transport can see the shared index but cannot close
            # the fork worker it does not own, even with the valid child ID.
            with self.assertRaisesRegex(RuntimeError, "Unknown session"):
                await self.client.request("session/close", {"sessionId": fork_id})
            # The contender already has a live transport. Its first load must
            # succeed immediately after the ACK while the owner process lives.
            with self.assertRaisesRegex(RuntimeError, "-33001"):
                await reopened.request("session/load", {**params, "sessionId": fork_id})
            self.assertEqual(await fork_client.request("session/close", {"sessionId": fork_id}), {})
            self.assertIsNone(fork_client.process.returncode)
            self.assertEqual(child.read_bytes(), child_bytes)
            self.assertEqual(parent.read_bytes(), parent_bytes)
            await reopened.request("session/load", {**params, "sessionId": fork_id})
            self.assertIn("SECOND_PARENT_TURN", reopened.text())
            self.assertIn("CHILD_NEXT", reopened.text())
            resumed = {**prompt, "prompt": [{"type": "text", "text": "AFTER_CLOSE_ACK"}]}
            self.assertEqual(await reopened.request("session/prompt", resumed), {"stopReason": "end_turn"})
            self.assertIsNone(fork_client.process.returncode)
            self.assertIn("SECOND_PARENT_TURN", json.dumps(self.model_requests[-1]["messages"]))
            self.assertIn("AFTER_CLOSE_ACK", child.read_text(encoding="utf-8"))
            self.assertEqual(parent.read_bytes(), parent_bytes)
        finally:
            await reopened.close()
            await fork_client.close()

    async def test_standard_head_fork_preserves_compaction_and_recovers_context_on_the_child(self):
        await self.config("thinking_level", "high")
        for text in ("Earlier parent context", "A second parent turn", "The latest parent detail"):
            await self.prompt(text)
        compacted = await self.prompt("/compact Keep the decisions", fork_head="compacted-head")
        self.assertEqual(compacted, {"stopReason": "end_turn"})
        self.assertEqual(self.context_updates("tool_call_update")[-1]["_meta"]["aelix/compaction"]["state"], "completed")

        parent = self.native_session_file(self.session)
        original = parent.read_bytes()
        await self.client.close()
        # Simulate an older ACP cache that disagrees with the compacted native history.
        with contextlib.closing(sqlite3.connect(self.root / "state/sessions.sqlite3")) as db:
            db.execute("UPDATE preferences SET value=json_set(value,'$.config.model',?,'$.config.thinking_level',?) "
                       "WHERE session_id=?", ("fixture/two", "low", self.session))
            db.commit()

        self.client = await AcpClient.start(self.root, EXECUTABLE)
        resumed = await self.client.request("session/load", {
            "sessionId": self.session, "cwd": str(self.root), "mcpServers": [],
        })
        options = {option["id"]: option["currentValue"] for option in resumed["configOptions"]}
        self.assertEqual((options["model"], options["thinking_level"]), ("fixture/one", "high"))
        fork = await self.client.request("session/fork", self.fork_params("compacted-head"))
        child_id = fork["sessionId"]
        self.assertNotEqual(child_id, self.session)
        child = self.native_session_file(child_id)
        entries = [json.loads(line) for line in child.read_bytes().splitlines()]
        self.assertEqual(Path(entries[0]["parentSession"]), parent)
        self.assertTrue(any(entry["type"] == "compaction" for entry in entries))

        await self.client.request("_aelix/manage", {"sessionId": child_id, "action": "sessions/close"})
        self.client.updates.clear()
        reopened = await self.client.request("session/load", {
            "sessionId": child_id, "cwd": str(self.root), "mcpServers": [],
        })
        options = {option["id"]: option["currentValue"] for option in reopened["configOptions"]}
        self.assertEqual((options["model"], options["thinking_level"]), ("fixture/one", "high"))
        self.assertEqual(self.context_updates("usage_update")[-1]["_meta"], {"aelix/contextUnknown": True})

        result = await self.client.request("session/prompt", {
            "sessionId": child_id, "prompt": [{"type": "text", "text": "Continue from the compacted fork"}],
        })
        self.assertEqual(result, {"stopReason": "end_turn"})
        self.assertEqual(self.model_requests[-1]["model"], "one")
        self.assertEqual(self.context_updates("usage_update")[-1],
                         {"sessionUpdate": "usage_update", "used": 26, "size": 32768})
        self.assertEqual(parent.read_bytes(), original)

    async def test_compaction_stays_completed_when_its_fork_head_cannot_be_saved(self):
        import sqlite3

        for text in ("Earlier context", "More context"):
            await self.prompt(text)
        await self.prompt("Latest context", fork_head="before-compaction")
        with contextlib.closing(sqlite3.connect(self.root / "state/sessions.sqlite3")) as db:
            db.execute("""CREATE TRIGGER fail_fork_head_save BEFORE INSERT ON preferences
                WHEN json_extract(NEW.value, '$.forkHead.id') = 'unsaved-head'
                BEGIN SELECT json_extract('fixture malformed JSON', '$'); END""")
            db.commit()
            result = await self.prompt("/compact", key="compact-once", fork_head="unsaved-head")
            self.assertEqual(result, {"stopReason": "end_turn"})
            self.assertEqual(self.context_updates("tool_call_update")[-1]["_meta"]["aelix/compaction"]["state"],
                             "completed")
            self.assertNotIn("Request failed:", self.client.text())
            self.assertEqual(db.execute("SELECT state FROM turns WHERE session_id=? AND request_key=?",
                                        (self.session, "compact-once")).fetchone(), ("completed",))
            db.execute("DROP TRIGGER fail_fork_head_save")
            db.commit()

        calls = len(self.model_requests)
        self.assertEqual(await self.prompt("/compact", key="compact-once", fork_head="unsaved-head"), result)
        self.assertEqual(len(self.model_requests), calls)
        for anchor in ("unsaved-head", "before-compaction"):
            with self.subTest(anchor=anchor), self.assertRaisesRegex(RuntimeError, "fork_head_changed"):
                await self.client.request("session/fork", self.fork_params(anchor))

        await self.prompt("Continue after the compacted turn", fork_head="current-head")
        fork = await self.client.request("session/fork", self.fork_params("current-head"))
        self.assertNotEqual(fork["sessionId"], self.session)

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

    async def test_generic_reopened_worker_preserves_native_stderr_passthrough(self):
        await self.prompt("seed saved history")
        await self.client.close()
        extension = self.root / "runtime_stderr.py"
        extension.write_text(textwrap.dedent("""
            from pathlib import Path
            import sys

            def setup(api):
                async def log(_event, ctx):
                    print(f"warning from {Path(ctx.cwd) / 'ABS_PATH_SENTINEL'}", file=sys.stderr, flush=True)
                    print("warning from RELATIVE_PATH_SENTINEL.jsonl.", file=sys.stderr, flush=True)
                    print("Could not read RELATIVE_WITHOUT_SUFFIX_SENTINEL", file=sys.stderr, flush=True)
                    print(".-" * 8192, file=sys.stderr, flush=True)
                    print("Warning: ACP_REOPEN_RUNTIME_DIAGNOSTIC", file=sys.stderr, flush=True)
                api.on("before_agent_start", log)
        """), encoding="utf-8")
        self.client = await AcpClient.start(self.root, EXECUTABLE, extensions=[extension])
        await self.client.request("session/load", {"sessionId": self.session, "cwd": str(self.root), "mcpServers": []})
        self.assertEqual((await self.prompt("exercise reopened runtime"))["stopReason"], "end_turn")
        async with asyncio.timeout(5):
            while b"ACP_REOPEN_RUNTIME_DIAGNOSTIC" not in self.client.stderr:
                await asyncio.sleep(0.01)
        await self.client.close()
        self.assertIn(b"Warning: ACP_REOPEN_RUNTIME_DIAGNOSTIC", self.client.stderr)
        self.assertIn(os.fsencode(self.root / "ABS_PATH_SENTINEL"), self.client.stderr)
        self.assertIn(b"RELATIVE_PATH_SENTINEL.jsonl.", self.client.stderr)
        self.assertIn(b"Could not read RELATIVE_WITHOUT_SUFFIX_SENTINEL", self.client.stderr)
        prefix = f"[aelix:{self.session[:8]}] ".encode()
        self.assertIn(b".-" * 8192, self.client.stderr.replace(prefix, b""))

    async def test_cli_appends_native_turns_with_stable_live_and_replay_ids(self):
        meta = "aelix/transcript"
        offer = {"_meta": {meta: {"version": 1}}}
        await self.client.close()
        extensions = [self.root / "commands.py"]
        self.client = await AcpClient.start(self.root, EXECUTABLE, capabilities=offer, extensions=extensions)
        load = {"cwd": str(self.root), "sessionId": self.session, "mcpServers": []}
        initial = await self.client.request("session/load", load)
        if meta not in initial["_meta"]:
            if os.environ.get("AELIX_ACP_REQUIRE_TRANSCRIPT") == "1":
                self.fail("The transcript candidate must advertise transcript/list")
            self.skipTest("This native runtime does not advertise transcript/list")
        self.assertEqual(initial["_meta"][meta]["totalMessages"], 0)
        self.assertEqual(initial["_meta"][meta]["totalFrames"], 0)

        await self.config("permission_mode", "yolo")
        self.client.updates.clear()
        self.assertEqual(await self.prompt("Studio first turn", key="studio-first"), {"stopReason": "end_turn"})
        live = self.client.updates.copy()
        settlements = [event["params"] for event in live if event["method"] == "_aelix/transcript/settled"]
        self.assertEqual([(item["role"], item["status"], item["sequence"]) for item in settlements],
                         [("user", "stored", 1), ("assistant", "stored", 2)])
        deltas = [event["params"]["update"] for event in live if event["method"] == "session/update"]
        assistant = next(update for update in deltas if update["sessionUpdate"] == "agent_message_chunk")
        coordinates = assistant["_meta"][meta]
        self.assertNotIn("entryId", coordinates)
        self.assertEqual((coordinates["bindingId"], coordinates["streamId"]),
                         (settlements[1]["bindingId"], settlements[1]["streamId"]))
        self.assertEqual(sum(update["sessionUpdate"] == "user_message_chunk" for update in deltas), 1)
        await self.client.close()

        # Model a pre-upgrade ACP event table. The original display is still
        # present, but its messages have no native coordinates.
        with contextlib.closing(sqlite3.connect(self.root / "state/sessions.sqlite3")) as db, db:
            session_file, = db.execute("SELECT file FROM sessions WHERE id=?", (self.session,)).fetchone()
            for seq, value in db.execute("SELECT sequence,value FROM events WHERE session_id=?", (self.session,)):
                update = json.loads(value)
                if meta in update.get("_meta", {}):
                    update["_meta"].pop(meta)
                    db.execute("UPDATE events SET value=? WHERE sequence=?", (json.dumps(update), seq))

        env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP") if key in os.environ}
        env.update(HOME=str(self.root / "home"), USERPROFILE=str(self.root / "home"),
                   AELIX_CODING_AGENT_DIR=str(self.root / "agent"),
                   AELIX_SETTINGS_PATH=str(self.root / "agent/settings.json"))
        cli = await asyncio.create_subprocess_exec(
            EXECUTABLE, "--mode", "rpc", "--offline", "--no-extensions", "--session", session_file,
            cwd=self.root, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
        appended = []
        try:
            for request_id in ("cli-1", "cli-2"):
                request = {"id": request_id, "type": "prompt", "message": "Identical CLI input"}
                cli.stdin.write((json.dumps(request) + "\n").encode())
                await cli.stdin.drain()
                async with asyncio.timeout(30):
                    while line := await cli.stdout.readline():
                        event = json.loads(line)
                        if event.get("id") == request_id:
                            self.assertTrue(event["success"], event)
                        if event["type"] == "message_end":
                            appended.append(event["transcript"])
                        if event["type"] == "agent_end":
                            break
                    else:
                        self.fail("CLI exited before settling the appended prompt")
            self.assertEqual(len(appended), 4)
            self.client = await AcpClient.start(self.root, EXECUTABLE, capabilities=offer, extensions=extensions)
            with self.assertRaises(RuntimeError) as caught:
                await self.client.request("session/load", load)
            self.assertEqual(json.loads(str(caught.exception))["code"], SESSION_LOCKED)
        finally:
            cli.stdin.close()
            try:
                await asyncio.wait_for(cli.wait(), 10)
            except TimeoutError:
                cli.kill()
                await cli.wait()
        self.assertEqual(cli.returncode, 0)
        self.assertEqual(len(self.model_requests), 3)

        replays = []
        for _ in range(2):
            self.client.updates.clear()
            result = await self.client.request("session/load", load)
            marker = result["_meta"][meta]
            self.assertEqual(marker["totalMessages"], 6)
            self.assertFalse(marker["recovered"])
            self.assertEqual(result["modes"]["currentModeId"], "yolo")
            updates = [event["params"]["update"] for event in self.client.updates if event["method"] == "session/update"]
            replay = [(update["_meta"][meta], update["content"]["text"]) for update in updates
                      if update["sessionUpdate"] in {"agent_message_chunk", "user_message_chunk"}]
            self.assertEqual(marker["totalFrames"], len(replay))
            self.assertEqual([text for _, text in replay],
                             ["Studio first turn", "NATIVE_OK", "Identical CLI input", "NATIVE_OK",
                              "Identical CLI input", "NATIVE_OK"])
            self.assertEqual([item["sequence"] for item, _ in replay], list(range(1, 7)))
            self.assertEqual(len({item["entryId"] for item, _ in replay}), 6)
            for expected, (item, _) in zip(settlements + appended, replay, strict=True):
                self.assertEqual(item["source"], "replay")
                self.assertEqual(item["part"], 0)
                self.assertEqual(item["nativeSessionId"], marker["nativeSessionId"])
                for key in ("nativeSessionId", "entryId", "parentEntryId", "sequence"):
                    self.assertEqual(item[key], expected[key])
            replays.append((marker, replay))
        self.assertEqual(replays[0], replays[1])
        self.assertEqual(await self.prompt("/help"), {"stopReason": "end_turn"})
        self.assertEqual(await self.prompt("/echo local-only"), {"stopReason": "end_turn"})
        adapter_replays = []
        for _ in range(2):
            self.client.updates.clear()
            result = await self.client.request("session/load", load)
            marker = result["_meta"][meta]
            self.assertEqual({key: value for key, value in marker.items() if key != "totalFrames"},
                             {key: value for key, value in replays[0][0].items() if key != "totalFrames"})
            updates = [event["params"]["update"] for event in self.client.updates if event["method"] == "session/update"]
            display = [update for update in updates if update.get("_meta", {}).get(meta, {}).get("source") == "adapter"]
            self.assertEqual(len(display), 4)
            self.assertEqual(marker["totalFrames"], replays[0][0]["totalFrames"] + len(display))
            self.assertEqual(display[0]["content"]["text"], "/help")
            self.assertEqual([update["content"]["text"] for update in display[2:]], ["/echo local-only", "echo:local-only"])
            coords = [update["_meta"][meta] for update in display]
            self.assertEqual(len({event["bindingId"] for event in coords}), 1)
            self.assertEqual([event["sequence"] for event in coords], list(range(1, 5)))
            self.assertEqual(len({event["eventId"] for event in coords}), 4)
            adapter_replays.append(display)
        self.assertEqual(adapter_replays[0], adapter_replays[1])
        self.assertEqual(len(self.model_requests), 3, "load cannot resend a prompt")

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

    def managed_pack(self, name, *, lazy=False, mcp_marker=None, broken=False):
        installed = self.root / "installed"
        installed.mkdir(exist_ok=True)
        module = name.replace("-", "_")
        marker = self.root / (name + ".setup")
        if broken:
            setup = "raise RuntimeError('synthetic setup failure')"
        else:
            setup = f"""Path({str(marker)!r}).touch()
    api.register_command("{name}", description="Managed command", handler=lambda args, ctx: "{name} works")
    api.on("session_shutdown", lambda event, ctx: None)"""
        (installed / f"{module}.py").write_text(f"from pathlib import Path\ndef setup(api):\n    {setup}\n", encoding="utf-8")
        wrapper = self.root / "packs" / name
        pkg = wrapper / name
        pkg.mkdir(parents=True)
        activation = f'on_command = ["{name}"]' if lazy else "on_startup_finished = true"
        manifest = textwrap.dedent(f"""
            [plugin]
            id = "{name}"
            name = "{name}"
            version = "0.1.0"
            description = "Managed test plugin"
            authors = ["Fixture <fixture@example.com>"]
            repository = "https://example.com/fixture"
            license = "MIT"

            [plugin.api]
            level = 1
            min_level = 1

            [plugin.entry]
            python = "{module}:setup"

            [activation]
            {activation}

            [contributes]
            commands = [{{ id = "{name}", description = "Managed command" }}]
        """)
        if mcp_marker:
            server = self.root / "managed_mcp.py"
            server.write_text(textwrap.dedent(f"""
                from pathlib import Path
                from mcp.server.fastmcp import FastMCP
                Path({str(mcp_marker)!r}).touch()
                service = FastMCP("managed-fixture")
                @service.tool()
                def managed_echo(value: str) -> str:
                    return value
                service.run()
            """), encoding="utf-8")
            manifest += (
                f'mcp_servers = [{{name = "managed-fixture", transport = "stdio", command = {json.dumps(sys.executable)}, '
                f'args = [{json.dumps(str(server))}]}}]\n\n[capabilities]\nshell_exec = true\n'
            )
        (pkg / "aelix-plugin.toml").write_text(manifest, encoding="utf-8")
        return {"id": name, "defaultEnabled": not broken, "reference": str(wrapper), "entryPoints": [f"{module}:setup"]}

    def managed_module(self, name, source, *, default_enabled=False):
        installed = self.root / "installed"
        installed.mkdir(exist_ok=True)
        (installed / f"{name}.py").write_text(source, encoding="utf-8")
        return {"id": name, "defaultEnabled": default_enabled, "reference": f"{name}:setup"}

    async def managed_adapter(self, generation, *entries, operator_extensions=(), studio_managed=False,
                              trust_project=False, launch_setup="", entry=("-m", "aelix_acp.server"), native_python=sys.executable):
        installed = self.root / "installed"
        installed.mkdir(exist_ok=True)
        snapshot = self.root / f"{generation}.json"
        snapshot.write_text(json.dumps({"version": 1, "generation": generation,
                                        "sitePackages": str(installed), "extensions": entries}), encoding="utf-8")
        launcher = self.root / "managed-launch.py"
        launcher.write_text(
            f"import sys\nsys.path.append({str(installed)!r})\n"
            f"{launch_setup}\n"
            "from aelix_coding_agent.cli.entry import main_sync\nmain_sync()\n", encoding="utf-8"
        )
        adapter = await AcpClient.start(self.root, native_python, prefix=(str(launcher),), extensions=operator_extensions,
                                        entry=entry,
                                        adapter_args=("--managed-extensions", str(snapshot),
                                                      *(["--trust-project"] if trust_project else [])),
                                        extra_env={"AELIX_ACP_STUDIO_MANAGED": "1"} if studio_managed else None)
        self.addAsyncCleanup(adapter.close)
        return adapter

    async def open_managed(self, adapter, session_id=None):
        params = {"cwd": str(self.root), "mcpServers": []}
        if session_id:
            await adapter.request("session/load", dict(params, sessionId=session_id))
            return session_id
        return (await adapter.request("session/new", params))["sessionId"]

    async def managed_request(self, adapter, session_id, action="list", **arguments):
        return await adapter.request("_aelix/manage", {"sessionId": session_id,
            "action": f"extensions/overrides/{action}", "arguments": arguments})

    async def managed_states(self, adapter, session_id):
        result = await self.managed_request(adapter, session_id)
        return {row["extensionId"]: row["state"] for row in result["extensions"]}

    async def test_studio_allows_managed_overrides_and_blocks_package_installs_in_the_same_session(self):
        entry = dict(self.managed_pack("studio-pack"), defaultEnabled=False)
        adapter = await self.managed_adapter("studio-g1", entry, studio_managed=True)
        session = await self.open_managed(adapter)
        self.assertEqual(await self.managed_states(adapter, session), {"studio-pack": "disabled"})
        self.assertFalse((self.root / "studio-pack.setup").exists())

        await self.managed_request(adapter, session, "set", extensionId=entry["id"], value="on")
        self.assertTrue((self.root / "studio-pack.setup").exists())
        self.assertEqual(await self.managed_states(adapter, session), {"studio-pack": "active"})
        self.assertEqual([request["method"] for request in adapter.requests], ["session/request_permission"])

        adapter.permission = "reject-once"
        with self.assertRaises(RuntimeError) as caught:
            await adapter.request("session/prompt", {"sessionId": session, "prompt": [
                {"type": "text", "text": "/extension install ./absent-fixture.whl"},
            ]})
        error = json.loads(str(caught.exception))
        self.assertEqual(error["code"], -32004)
        self.assertEqual(error["data"], {"kind": "studio_extension_management_required", "action": "install"})
        self.assertEqual([request["method"] for request in adapter.requests], ["session/request_permission"])
        self.assertEqual(await self.managed_states(adapter, session), {"studio-pack": "active"})

    async def test_managed_extensions_recover_across_workers_adapters_and_generations(self):
        mcp = self.root / "managed.mcp"
        one = self.managed_pack("managed-one", mcp_marker=mcp)
        two = self.managed_pack("managed-two")
        first = await self.managed_adapter("g1", one, two)
        second = await self.managed_adapter("g1", one, two)
        a, b = await self.open_managed(first), await self.open_managed(second)
        active = {"managed-one": "active", "managed-two": "active"}
        self.assertEqual(await self.managed_states(first, a), active)
        self.assertTrue(mcp.exists())
        inventory = await first.request("_aelix/manage", {"sessionId": a, "action": "resources/list"})
        mcp_tool = next(tool["name"] for tool in inventory["tools"] if tool["name"].endswith("__managed_echo"))
        await first.request("session/prompt", {"sessionId": a, "prompt": [{"type": "text", "text": "Remember me"}]})
        mcp.unlink()
        (self.root / "managed-one.setup").unlink()
        (self.root / "managed-two.setup").unlink()

        await self.managed_request(first, a, "set", extensionId=one["id"], value="off")
        self.assertEqual(await self.managed_states(first, a), dict(active, **{"managed-one": "disabled"}))
        self.assertFalse(mcp.exists())
        self.assertFalse((self.root / "managed-one.setup").exists())
        self.assertTrue((self.root / "managed-two.setup").exists())
        inventory = await first.request("_aelix/manage", {"sessionId": a, "action": "resources/list"})
        hooks = {Path(row["extension"]).name for row in inventory["hooks"]}
        self.assertNotIn(mcp_tool, {tool["name"] for tool in inventory["tools"]})
        self.assertNotIn("managed-one", hooks)
        self.assertIn("managed-two", hooks)
        commands = [event["params"]["update"]["availableCommands"] for event in first.updates
                    if event.get("method") == "session/update"
                    and event["params"]["update"]["sessionUpdate"] == "available_commands_update"][-1]
        self.assertNotIn("managed-one", {command["name"] for command in commands})
        self.assertIn("managed-two", {command["name"] for command in commands})
        self.assertEqual(await self.managed_states(second, b), active)
        self.assertEqual(await self.managed_states(second, await self.open_managed(second)), active)

        shared_packs = str(self.root / "packs")
        with self.assertRaises(RuntimeError) as overlap:
            await first.request("_aelix/manage", {"sessionId": a, "action": "extensions/action",
                                                    "arguments": {"action": "enable", "target": shared_packs}})
        self.assertEqual(json.loads(str(overlap.exception))["data"]["kind"], "managed_extension_conflict")
        current = self.root / "g1.json"
        current.write_text('{"version":2}', encoding="utf-8")
        await first.request("session/close", {"sessionId": a})
        await self.open_managed(first, a)
        self.assertEqual(await self.managed_states(first, a), dict(active, **{"managed-one": "disabled"}))
        await first.close()
        third = await self.managed_adapter("g2", dict(one, defaultEnabled=False), two)
        await self.open_managed(third, a)
        self.assertIn("Remember me", third.text())
        self.assertEqual((await self.managed_request(third, a))["generation"], "g2")
        await self.managed_request(third, a, "set", extensionId=one["id"], value="on")
        self.assertEqual(await self.managed_states(third, a), active)
        new_session = await self.open_managed(third)
        self.assertEqual((await self.managed_states(third, new_session))["managed-one"], "disabled")
        await third.close()

        removed = await self.managed_adapter("g3", two)
        await self.open_managed(removed, a)
        self.assertEqual(await self.managed_states(removed, a), dict(active, **{"managed-one": "unavailable"}))
        restored = await self.managed_adapter("g4", dict(one, defaultEnabled=False), two)
        await removed.close()
        await self.open_managed(restored, a)
        self.assertEqual(await self.managed_states(restored, a), active)
        await self.managed_request(restored, a, "set", extensionId=one["id"], value="inherit")
        self.assertEqual(await self.managed_states(restored, a), dict(active, **{"managed-one": "disabled"}))

    async def test_managed_apply_failure_recovers_and_defers_lazy_packs(self):
        lazy = self.managed_pack("lazy-pack", lazy=True)
        broken = self.managed_pack("broken-pack", broken=True)
        adapter = await self.managed_adapter("g1", lazy, broken)
        session = await self.open_managed(adapter)
        self.assertFalse((self.root / "lazy-pack.setup").exists())
        self.assertEqual(await self.managed_states(adapter, session), {"lazy-pack": "deferred", "broken-pack": "disabled"})
        adapter.permission = "reject-once"
        with self.assertRaises(RuntimeError) as rejected:
            await self.managed_request(adapter, session, "set", extensionId=lazy["id"], value="off")
        self.assertEqual(json.loads(str(rejected.exception))["code"], -32003)
        adapter.permission = "allow-once"
        with self.assertRaises(RuntimeError) as failed:
            await self.managed_request(adapter, session, "set", extensionId=broken["id"], value="on")
        data = json.loads(str(failed.exception))["data"]
        self.assertEqual(data["kind"], "managed_extension_change_failed")
        self.assertEqual(data["reason"], "load_failed")
        self.assertTrue(data["recovered"])
        self.assertEqual(await self.managed_states(adapter, session), {"lazy-pack": "deferred", "broken-pack": "disabled"})
        with contextlib.closing(sqlite3.connect(self.root / "state/sessions.sqlite3")) as db:
            saved = json.loads(db.execute("SELECT value FROM preferences WHERE session_id=?", (session,)).fetchone()[0])
        self.assertNotIn("extensionOverrides", saved)
        await adapter.request("session/prompt", {"sessionId": session, "prompt": [{"type": "text", "text": "/lazy-pack"}]})
        self.assertTrue((self.root / "lazy-pack.setup").exists())
        self.assertEqual((await self.managed_states(adapter, session))["lazy-pack"], "active")
        await adapter.request("session/prompt", {"sessionId": session,
            "prompt": [{"type": "text", "text": "/extension disable lazy-pack"}]})
        self.assertEqual((await self.managed_states(adapter, session))["lazy-pack"], "disabled")

    async def publication_managed_worker(self):
        extension = self.managed_module("publication_change", "def setup(api): pass\n")
        python = Path(EXECUTABLE).with_name("python.exe" if os.name == "nt" else "python")
        client = await self.managed_adapter("publication", extension, entry=self.publication_probe(),
                                            native_python=str(python) if python.is_file() else sys.executable)
        return client, await self.open_managed(client), extension

    async def test_stop_before_final_extension_commit_restores_the_saved_worker(self):
        client, session_id, extension = await self.publication_managed_worker()
        change = self.managed_request(client, session_id, "set", extensionId=extension["id"], value="on")
        async with self.hold_final_save("restart", change) as (request, _, candidate, _):
            self.assertEqual(candidate, session_id)
            self.assertEqual(await client.request("session/cancel", {"sessionId": session_id}, timeout=8), {})
            self.assertFalse(request.done())
        with self.assertRaises(RuntimeError) as error:
            await request
        payload = json.loads(str(error.exception))
        self.assertEqual((payload["code"], payload["data"]["reason"], payload["data"]["recovered"]),
                         (-32003, "cancelled", True))
        self.assertEqual(await self.managed_states(client, session_id), {extension["id"]: "disabled"})
        with contextlib.closing(sqlite3.connect(self.root / "state/sessions.sqlite3")) as db:
            (value,) = db.execute("SELECT value FROM preferences WHERE session_id=?", (session_id,)).fetchone()
        self.assertNotIn("extensionOverrides", json.loads(value))

    async def test_stop_after_extension_commit_admission_preserves_success(self):
        client, session_id, extension = await self.publication_managed_worker()
        (self.root / "publication-pause-commit").touch()
        change = self.managed_request(client, session_id, "set", extensionId=extension["id"], value="on")
        async with self.hold_final_save("restart", change) as (request, _, _, _):
            self.assertFalse(request.done())
        try:
            await self.publication_marker("admitted")
            self.assertEqual(await client.request("session/cancel", {"sessionId": session_id}, timeout=8), {})
            self.assertFalse(request.done())
        finally:
            (self.root / "publication-commit").touch()
        result = await request
        self.assertEqual(result["extensions"][0]["state"], "active")
        self.assertEqual(await self.managed_states(client, session_id), {extension["id"]: "active"})
        with contextlib.closing(sqlite3.connect(self.root / "state/sessions.sqlite3")) as db:
            (value,) = db.execute("SELECT value FROM preferences WHERE session_id=?", (session_id,)).fetchone()
        self.assertEqual(json.loads(value)["extensionOverrides"], {extension["id"]: "on"})

    async def test_worker_exit_after_commit_admission_refuses_registration(self):
        client, session_id, extension = await self.publication_managed_worker()
        (self.root / "publication-pause-commit").touch()
        change = self.managed_request(client, session_id, "set", extensionId=extension["id"], value="on")
        async with self.hold_final_save("restart", change) as (request, _, _, pid):
            self.assertFalse(request.done())
        try:
            await self.publication_marker("admitted")
            await self.terminate_publication_worker(pid)
        finally:
            (self.root / "publication-commit").touch()
        with self.assertRaises(RuntimeError) as error:
            await request
        payload = json.loads(str(error.exception))
        self.assertEqual((payload["code"], payload["data"]["reason"], payload["data"]["recovered"]),
                         (-32004, "session_worker_closed_after_commit", True))
        self.assertEqual(await self.managed_states(client, session_id), {extension["id"]: "active"})

    async def test_cancel_managed_module_replacement_restores_previous_worker(self):
        installed = self.root / "installed"
        installed.mkdir()
        marker = self.root / "candidate-started"
        (installed / "managed_slow.py").write_text(textwrap.dedent(f"""
            import asyncio
            from pathlib import Path
            async def setup(api):
                Path({str(marker)!r}).touch()
                await asyncio.sleep(60)
        """), encoding="utf-8")
        entry = {"id": "demo:slow", "defaultEnabled": False, "reference": "managed_slow:setup"}
        adapter = await self.managed_adapter("g1", entry)
        session = await self.open_managed(adapter)
        change = asyncio.create_task(self.managed_request(adapter, session, "set", extensionId=entry["id"], value="on"))
        try:
            async with asyncio.timeout(15):
                while not marker.exists():
                    await asyncio.sleep(0.02)
            await adapter.request("session/cancel", {"sessionId": session})
            with self.assertRaises(RuntimeError) as cancelled:
                await change
            err = json.loads(str(cancelled.exception))
            self.assertEqual(err["code"], -32003)
            self.assertEqual(err["data"]["reason"], "cancelled")
            self.assertTrue(err["data"]["recovered"])
            self.assertEqual(await self.managed_states(adapter, session), {"demo:slow": "disabled"})
        finally:
            change.cancel()
            await asyncio.gather(change, return_exceptions=True)


    async def test_managed_module_uses_generation_in_trusted_workspace(self):
        marker = self.root / "generation-factory"
        entry = self.managed_module("generation_test", textwrap.dedent(f"""
            from pathlib import Path
            def setup(api):
                Path({str(marker)!r}).touch()
        """), default_enabled=True)
        (self.root / "generation_test.py").touch()
        if os.name != "nt":
            # A benign workspace directory can have the reference's literal
            # name. It contains no extension code.
            (self.root / entry["reference"]).mkdir()
        adapter = await self.managed_adapter("g1", entry, trust_project=True)
        session = await self.open_managed(adapter)
        self.assertTrue(marker.exists())
        self.assertEqual(await self.managed_states(adapter, session), {"generation_test": "active"})
        resources = await adapter.request("_aelix/manage", {"sessionId": session, "action": "resources/list"})
        self.assertTrue(resources["projectTrusted"])
        marker.unlink()
        await adapter.request("_aelix/manage", {"sessionId": session, "action": "resources/reload"})
        self.assertTrue(marker.exists())
        self.assertEqual(await self.managed_states(adapter, session), {"generation_test": "active"})

    async def test_managed_recovery_closes_worker_if_previous_extension_cannot_register(self):
        stop_registration = self.root / "fail-previous-registration"
        previous = self.managed_module("previous_ext", textwrap.dedent(f"""
            from pathlib import Path
            def setup(api):
                if Path({str(stop_registration)!r}).exists():
                    raise RuntimeError("synthetic registration failure")
        """))
        bad = self.managed_module("bad_ext", 'def setup(api): raise RuntimeError("synthetic change failure")\n')
        adapter = await self.managed_adapter("g1", previous, bad)
        session = await self.open_managed(adapter)
        history = "Remember settings through a failed recovery"
        await adapter.request("session/prompt", {"sessionId": session, "prompt": [{"type": "text", "text": history}]})
        await self.managed_request(adapter, session, "set", extensionId=previous["id"], value="on")
        self.assertEqual((await self.managed_states(adapter, session))[previous["id"]], "active")
        stop_registration.touch()
        with self.assertRaises(RuntimeError) as caught:
            await self.managed_request(adapter, session, "set", extensionId=bad["id"], value="on")
        error = json.loads(str(caught.exception))
        self.assertEqual(error["code"], -32004)
        self.assertEqual(error["data"]["kind"], "managed_extension_change_failed")
        self.assertFalse(error["data"]["recovered"])
        with self.assertRaisesRegex(RuntimeError, "Unknown session"):
            await self.managed_request(adapter, session)
        with contextlib.closing(sqlite3.connect(self.root / "state/sessions.sqlite3")) as db:
            preferences = json.loads(db.execute("SELECT value FROM preferences WHERE session_id=?", (session,)).fetchone()[0])
            self.assertEqual(preferences["extensionOverrides"], {previous["id"]: "on"})
        stop_registration.unlink()
        await self.open_managed(adapter, session)
        self.assertEqual(await self.managed_states(adapter, session), {previous["id"]: "active", bad["id"]: "disabled"})
        self.assertIn(history, adapter.text())

    async def test_managed_mode_refuses_unlisted_enable_from_slash_and_api(self):
        adapter = await self.managed_adapter("g1")
        session = await self.open_managed(adapter)
        unlisted = self.root / "unlisted.py"
        unlisted.touch()
        adapter.permission = "reject-once"
        for method, params in (
            ("session/prompt", {"prompt": [{"type": "text", "text": "/extension enable ./unlisted.py"}]}),
            ("_aelix/manage", {"action": "extensions/action", "arguments": {"action": "enable", "target": str(unlisted)}}),
        ):
            with self.subTest(method=method), self.assertRaises(RuntimeError) as caught:
                await adapter.request(method, dict(params, sessionId=session))
            error = json.loads(str(caught.exception))
            self.assertEqual(error["code"], -32004)
            self.assertEqual(error["data"]["kind"], "managed_extension_unavailable")
        self.assertEqual(adapter.requests, [])
        self.assertEqual(await self.managed_states(adapter, session), {})

    async def test_managed_change_requires_other_selected_extensions_to_register(self):
        fail_once = self.root / "fail-next-registration"
        candidate_started = self.root / "candidate-registered"
        previous = self.managed_module("previous_selected", textwrap.dedent(f"""
            from pathlib import Path
            def setup(api):
                trigger = Path({str(fail_once)!r})
                if trigger.exists():
                    trigger.unlink()
                    raise RuntimeError("synthetic one-time registration failure")
        """))
        candidate = self.managed_module("next_selected", textwrap.dedent(f"""
            from pathlib import Path
            def setup(api):
                Path({str(candidate_started)!r}).touch()
        """))
        adapter = await self.managed_adapter("g1", previous, candidate)
        session_id = await self.open_managed(adapter)
        await self.managed_request(adapter, session_id, "set", extensionId=previous["id"], value="on")
        self.assertEqual(await self.managed_states(adapter, session_id), {
            previous["id"]: "active", candidate["id"]: "disabled",
        })

        fail_once.touch()
        with self.assertRaises(RuntimeError) as caught:
            await self.managed_request(adapter, session_id, "set", extensionId=candidate["id"], value="on")
        error = json.loads(str(caught.exception))
        self.assertEqual(error["code"], -32004)
        self.assertEqual(error["data"]["kind"], "managed_extension_change_failed")
        self.assertEqual(error["data"]["reason"], "load_failed")
        self.assertEqual(error["data"]["extensionId"], candidate["id"])
        self.assertEqual(error["data"]["failedExtensionId"], previous["id"])
        self.assertTrue(error["data"]["recovered"])
        self.assertTrue(candidate_started.exists())
        self.assertFalse(fail_once.exists())
        self.assertEqual(await self.managed_states(adapter, session_id), {
            previous["id"]: "active", candidate["id"]: "disabled",
        })
        with contextlib.closing(sqlite3.connect(self.root / "state/sessions.sqlite3")) as db:
            preferences = json.loads(db.execute("SELECT value FROM preferences WHERE session_id=?", (session_id,)).fetchone()[0])
        self.assertEqual(preferences["extensionOverrides"], {previous["id"]: "on"})

    async def test_managed_lazy_factory_lookup_uses_generation_import_guard(self):
        marker = self.root / "lazy-generation-factory-registered"
        entry = self.managed_module("acp_lazy_managed", textwrap.dedent("""
            def __getattr__(name):
                if name == "setup":
                    from acp_lazy_factory_fixture import register
                    return register
                raise AttributeError(name)
        """), default_enabled=True)
        (self.root / "installed" / "acp_lazy_factory_fixture.py").write_text(textwrap.dedent(f"""
            from pathlib import Path
            def register(api):
                Path({str(marker)!r}).touch()
        """), encoding="utf-8")
        # A harmless file under the workspace's ordinary import path should
        # not be considered when the installed module looks up its factory.
        (self.root / "acp_lazy_factory_fixture.py").touch()
        adapter = await self.managed_adapter("g1", entry, trust_project=True)
        session_id = await self.open_managed(adapter)
        self.assertTrue(marker.exists())
        self.assertEqual(await self.managed_states(adapter, session_id), {entry["id"]: "active"})

    async def test_managed_factory_rejects_cached_and_workspace_subdirectory_origins(self):
        workspace_imports = self.root / "workspace-imports"
        workspace_imports.mkdir()
        foreign = workspace_imports / "outside_fixture.py"
        foreign.write_text("def register(api): pass\n", encoding="utf-8")
        package = self.root / "installed" / "acp_cached_fixture"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text(textwrap.dedent("""
            def __getattr__(name):
                if name == "setup":
                    from .supplier import register
                    return register
                raise AttributeError(name)
        """), encoding="utf-8")
        on_path = self.managed_module("acp_workspace_fixture", textwrap.dedent("""
            def __getattr__(name):
                if name == "setup":
                    from outside_fixture import register
                    return register
                raise AttributeError(name)
        """), default_enabled=True)
        cached = {"id": "cached", "defaultEnabled": True, "reference": "acp_cached_fixture:setup"}
        setup = textwrap.dedent(f"""
            from importlib.util import module_from_spec, spec_from_file_location
            sys.path.insert(0, {str(workspace_imports)!r})
            name = "acp_cached_fixture.supplier"
            spec = spec_from_file_location(name, {str(foreign)!r})
            fixture = module_from_spec(spec)
            spec.loader.exec_module(fixture)
            sys.modules[name] = fixture
        """)
        adapter = await self.managed_adapter("g1", cached, on_path, trust_project=True, launch_setup=setup)
        session = await self.open_managed(adapter)
        rows = (await self.managed_request(adapter, session))["extensions"]
        self.assertEqual({row["extensionId"]: (row["state"], row["reason"]) for row in rows},
                         {cached["id"]: ("error", "load_failed"), on_path["id"]: ("error", "load_failed")})

    async def test_managed_factory_allows_generation_namespace_and_normal_dependencies(self):
        package = self.root / "installed" / "acp_namespace_fixture"
        package.mkdir(parents=True)
        marker = self.root / "namespace-factory-ran"
        (package / "entry.py").write_text(textwrap.dedent("""
            def __getattr__(name):
                if name == "setup":
                    from .supplier import register
                    return register
                raise AttributeError(name)
        """), encoding="utf-8")
        (package / "supplier.py").write_text(textwrap.dedent(f"""
            from pathlib import Path
            from . import sibling

            def register(api):
                Path({str(marker)!r}).write_text(sibling.content(), encoding="utf-8")
        """), encoding="utf-8")
        (package / "sibling.py").write_text(textwrap.dedent("""
            from json import dumps

            def content():
                return dumps({"generation": True})
        """), encoding="utf-8")
        entry = {"id": "namespace", "defaultEnabled": True, "reference": "acp_namespace_fixture.entry:setup"}
        adapter = await self.managed_adapter("g1", entry)
        session = await self.open_managed(adapter)
        self.assertEqual(await self.managed_states(adapter, session), {"namespace": "active"})
        self.assertEqual(json.loads(marker.read_text(encoding="utf-8")), {"generation": True})

    async def test_managed_class_factory_keeps_working_when_enabling_callable_instance(self):
        class_marker = self.root / "class-factory-ran"
        instance_marker = self.root / "instance-factory-ran"
        default_enabled = self.managed_module("acp_factory_class", textwrap.dedent(f"""
            from pathlib import Path

            class Registration:
                def __init__(self, api):
                    Path({str(class_marker)!r}).touch()

            setup = Registration
        """), default_enabled=True)
        disabled = self.managed_module("acp_factory_instance", textwrap.dedent(f"""
            from pathlib import Path

            class Registration:
                def __call__(self, api):
                    Path({str(instance_marker)!r}).touch()

            setup = Registration()
        """))
        adapter = await self.managed_adapter("g1", default_enabled, disabled)
        session = await self.open_managed(adapter)
        self.assertEqual(await self.managed_states(adapter, session), {
            default_enabled["id"]: "active", disabled["id"]: "disabled",
        })
        self.assertTrue(class_marker.exists())
        self.assertFalse(instance_marker.exists())
        class_marker.unlink()
        await self.managed_request(adapter, session, "set", extensionId=disabled["id"], value="on")
        self.assertEqual(await self.managed_states(adapter, session), {
            default_enabled["id"]: "active", disabled["id"]: "active",
        })
        self.assertTrue(class_marker.exists())
        self.assertTrue(instance_marker.exists())
