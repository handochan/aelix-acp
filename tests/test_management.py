import asyncio
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from aelix_acp.server import Server, Session, ProtocolError
from aelix_acp.runtime.commands import split_command
from aelix_acp.runtime.commands import execute
from aelix_acp.runtime.extensions import extension_reference, run_extension_action
from support import process_alive


class ManagementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.server = Server(SimpleNamespace(state_dir=self.temp.name, profile="coding", aelix="/fake/aelix"))
        self.session = Session(self.server, "session", self.temp.name)
        self.messages = []
        async def send(message):
            self.messages.append(message)
        self.server.send = send

    async def asyncTearDown(self):
        await asyncio.gather(*list(self.server.cleanup_tasks), return_exceptions=True)
        self.server.db.close()
        self.temp.cleanup()

    async def test_version_negotiates_to_v1_instead_of_claiming_draft_v2(self):
        result = await self.server.dispatch("initialize", {"protocolVersion": 2, "clientCapabilities": {}})
        self.assertEqual(result["protocolVersion"], 1)
        with self.assertRaisesRegex(ProtocolError, "Already initialized"):
            await self.server.dispatch("initialize", {"protocolVersion": 1})

    async def test_malformed_client_capabilities_are_rejected(self):
        with self.assertRaises(ProtocolError):
            await self.server.dispatch("initialize", {"protocolVersion": 1, "clientCapabilities": []})
        self.assertFalse(self.server.initialized)

    async def test_ui_action_is_not_sent_to_an_unadvertised_client(self):
        with self.assertRaises(ProtocolError) as result:
            await self.server.client_action(self.session, "new_session", {})
        self.assertEqual(result.exception.data["kind"], "client_action_required")
        self.assertEqual(self.messages, [])

    async def test_error_response_retains_machine_readable_details(self):
        self.server.sessions[self.session.id] = self.session
        self.session.controls.capabilities = {}
        await self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "session/prompt", "params": {
            "sessionId": self.session.id, "prompt": [{"type": "text", "text": "/new"}]}})
        error = next(message["error"] for message in self.messages if message.get("id") == 1)
        self.assertEqual(error["data"]["kind"], "client_action_required")

    async def test_rejected_extension_install_never_spawns_a_package_manager(self):
        async def deny(method, params):
            return {"outcome": {"outcome": "selected", "optionId": "reject-once"}}
        self.server.client_request = deny
        with patch("asyncio.create_subprocess_exec") as process:
            with self.assertRaisesRegex(ProtocolError, "not approved"):
                await run_extension_action(self.server, self.session, {"action": "install", "target": "fixture"})
            process.assert_not_called()

    async def test_approved_update_and_remove_pass_headless_consent_to_native_cli(self):
        import json
        script = self.root / "manager.py"
        script.write_text("import json,sys\nprint(json.dumps(sys.argv[1:]))\n")
        self.server.args.aelix = sys.executable
        self.server.args.aelix_arg = [str(script)]
        async def approve(method, params):
            return {"outcome": {"outcome": "selected", "optionId": "allow-once"}}
        self.server.client_request = approve
        for action in ("update", "remove"):
            result = await run_extension_action(self.server, self.session, {"action": action, "target": "fixture"})
            self.assertEqual(json.loads(result["text"]), ["extension", action, "fixture", "--yes"])

    async def test_cancel_stops_an_owned_extension_manager_process(self):
        script = self.root / "manager.py"
        marker = self.root / "manager.pid"
        script.write_text("import os,time\nfrom pathlib import Path\nPath('manager.pid').write_text(str(os.getpid()))\ntime.sleep(60)\n")
        self.server.args.aelix = sys.executable
        self.server.args.aelix_arg = [str(script)]
        task = asyncio.create_task(run_extension_action(self.server, self.session, "list"))
        try:
            async with asyncio.timeout(10):
                while not marker.is_file():
                    await asyncio.sleep(0.01)
            await self.session.cancel()
            with self.assertRaisesRegex(ProtocolError, "cancelled"):
                await task
            self.assertFalse(process_alive(int(marker.read_text())))
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_bundled_application_cannot_be_modified_by_extension_installs(self):
        self.server.args.aelix = "/Applications/Studio.app/Contents/Resources/bin/aelix"
        with patch("asyncio.create_subprocess_exec") as process:
            with self.assertRaisesRegex(ProtocolError, "read-only"):
                await run_extension_action(self.server, self.session, {"action": "install", "target": "fixture"})
            process.assert_not_called()

    async def test_unknown_or_flag_like_extension_actions_never_execute(self):
        for command in ["execute anything", "install --python", "remove", "list a b"]:
            with self.subTest(command=command), patch("asyncio.create_subprocess_exec") as process:
                with self.assertRaises(ProtocolError):
                    await run_extension_action(self.server, self.session, command)
                process.assert_not_called()

    async def test_compaction_and_extension_reload_share_the_configured_deadline(self):
        calls = []
        async def rpc(command, **arguments):
            calls.append((command, arguments))
            return {}
        self.session.rpc = rpc
        for timeout in (147, 0):
            self.server.args.command_timeout = timeout
            await execute(self.session, "/compact")
            await run_extension_action(self.server, self.session, "reload")
            compact = next(arguments for command, arguments in reversed(calls) if command == "compact")
            reload = next(arguments for command, arguments in reversed(calls) if command == "aelix_control" and arguments.get("method") == "resources/reload")
            self.assertEqual(compact["_timeout"], timeout or None)
            self.assertEqual(reload["_timeout"], timeout or None)

    async def test_session_pages_do_not_duplicate_equal_timestamps(self):
        self.server.db.executemany("INSERT INTO sessions VALUES(?,?,?,?,?,?)", [
            (f"id-{i:03}", str(self.root.resolve()), "coding", None, "title", 1) for i in range(205)])
        self.server.db.commit()
        identifiers = []
        params = {"cwd": self.temp.name}
        while True:
            result = await self.server.dispatch("session/list", params)
            identifiers.extend(row["sessionId"] for row in result["sessions"])
            if "nextCursor" not in result:
                break
            params["cursor"] = result["nextCursor"]
        self.assertEqual(len(identifiers), 205)
        self.assertEqual(len(set(identifiers)), 205)

    async def test_invalid_cursor_is_a_parameter_error(self):
        with self.assertRaises(ProtocolError):
            await self.server.dispatch("session/list", {"cursor": "not-base64-json"})

    async def test_delete_reserves_the_actor_until_its_records_are_removed(self):
        self.server.sessions[self.session.id] = self.session
        self.session.cwd = str(self.root.resolve())
        self.session.save()
        entered, release = asyncio.Event(), asyncio.Event()
        async def close():
            entered.set()
            await release.wait()
            self.server.sessions.pop(self.session.id, None)
        self.session.close = close
        deleting = asyncio.create_task(self.server.dispatch("session/delete", {"sessionId": self.session.id}))
        try:
            await entered.wait()
            with self.assertRaisesRegex(ProtocolError, "busy"):
                await self.server.dispatch("session/prompt", {"sessionId": self.session.id,
                    "prompt": [{"type": "text", "text": "/help"}]})
            with self.assertRaisesRegex(ProtocolError, "starting"):
                await self.server.dispatch("session/load", {"sessionId": self.session.id, "cwd": self.temp.name, "mcpServers": []})
        finally:
            release.set()
            await deleting

    async def test_client_rpc_errors_are_backend_outcomes_not_invalid_user_parameters(self):
        import json
        reader = asyncio.StreamReader()
        future = asyncio.get_running_loop().create_future()
        self.server.pending["client-request"] = future
        reader.feed_data((json.dumps({"jsonrpc": "2.0", "id": "client-request", "error": {
            "code": -32601, "message": "Unsupported client action"}}) + "\n").encode())
        reader.feed_eof()
        class Input:
            readline = staticmethod(reader.readline)
            def close(self):
                pass
        with patch("aelix_acp.server.StdinReader", Input):
            await self.server.run()
        error = future.exception()
        self.assertEqual(error.code, -32603)
        self.assertEqual(error.data, {"kind": "client_error", "code": -32601})

    async def test_concurrent_starts_reserve_the_process_limit(self):
        self.server.args.max_sessions = 1
        entered, release = asyncio.Event(), asyncio.Event()
        class Starting:
            instances = 0
            def __init__(self, server, identifier, cwd, **_options):
                Starting.instances += 1
                self.id = identifier
                self.mutation_lock = asyncio.Lock()
            async def start(self, filename):
                entered.set()
                await release.wait()
            async def commands(self):
                pass
            async def publish_recovery_warnings(self):
                pass
            async def close(self):
                pass
            def selectors(self):
                return {}
        with patch("aelix_acp.server.Session", Starting):
            first = asyncio.create_task(self.server.dispatch("session/new", {"cwd": self.temp.name, "mcpServers": []}))
            await entered.wait()
            with self.assertRaisesRegex(ProtocolError, "limit reached"):
                await self.server.dispatch("session/new", {"cwd": self.temp.name, "mcpServers": []})
            release.set()
            await first
        self.assertEqual(Starting.instances, 1)


class ExtensionReferenceTests(unittest.TestCase):
    def test_native_module_references_are_not_rewritten_as_paths(self):
        self.assertEqual(extension_reference("pack.extension:setup", "/tmp"), "pack.extension:setup")

    def test_a_project_path_cannot_silently_shadow_an_installed_module(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "pack.extension").mkdir()
            with self.assertRaisesRegex(ProtocolError, "shadows"):
                extension_reference("pack.extension", directory)

    def test_paths_with_spaces_remain_one_native_argument(self):
        with tempfile.TemporaryDirectory() as directory:
            expected = Path(directory) / "my extension.py"
            self.assertEqual(extension_reference(str(expected), directory), str(expected.resolve()))

    def test_malformed_slash_commands_never_fall_through_as_plain_prompts(self):
        for value in ["/", "/ ", "/model\nother"]:
            with self.subTest(value=value), self.assertRaises(ProtocolError):
                split_command(value)
