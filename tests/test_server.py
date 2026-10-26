import asyncio
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from aelix_acp.server import ProtocolError, Server, Session


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def channel(self):
        self.session.permission_token = "fixture-session-token"
        listener = await asyncio.start_server(self.session.permission, "127.0.0.1", 0)
        port = listener.sockets[0].getsockname()[1]
        self.session.permission_endpoint = f"tcp://127.0.0.1:{port}"
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        return listener, reader, writer

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.args = SimpleNamespace(state_dir=self.tmp.name, profile="coding")
        self.server = Server(self.args)
        self.events = []

        async def capture(event):
            self.events.append(event)

        self.server.send = capture
        self.session = Session(self.server, "test-session", self.tmp.name)

    async def asyncTearDown(self):
        self.server.db.close()
        self.tmp.cleanup()

    async def test_pending_tool_is_created_once_and_does_not_run_before_permission(self):
        release = asyncio.Event()

        async def consent(method, params):
            await release.wait()
            return {"outcome": {"outcome": "selected", "optionId": "allow-once"}}

        self.server.client_request = consent
        path = Path(self.tmp.name) / "permission.sock"
        listener, reader, writer = await self.channel()
        await self.session.event({"type": "tool_execution_start", "tool_call_id": "t1",
                                  "tool_name": "write", "args": {"path": "output.html"}})
        writer.write(json.dumps({"token": self.session.permission_token, "toolCallId": "t1", "name": "write", "args": {"path": "output.html"}}).encode() + b"\n")
        await writer.drain()
        receive = asyncio.create_task(reader.readline())
        await asyncio.sleep(0.02)
        self.assertFalse(receive.done(), "Permission was granted before the client answered")
        release.set()
        self.assertEqual(json.loads(await receive), {"allow": True})
        updates = [e["params"]["update"] for e in self.events]
        self.assertEqual(sum(u["sessionUpdate"] == "tool_call" for u in updates), 1)
        self.assertEqual([u["status"] for u in updates], ["pending", "pending", "in_progress"])
        writer.close()
        await writer.wait_closed()
        listener.close()
        await listener.wait_closed()

    async def test_cancelling_pending_consent_rejects_before_abort(self):
        entered = asyncio.Event()
        never = asyncio.Event()

        async def consent(method, params):
            entered.set()
            await never.wait()

        async def abort(command, **kwargs):
            self.assertEqual(command, "abort")
            return {}

        self.server.client_request = consent
        self.session.rpc = abort
        path = Path(self.tmp.name) / "cancel.sock"
        listener, reader, writer = await self.channel()
        writer.write(json.dumps({"token": self.session.permission_token, "toolCallId": "t2", "name": "bash", "args": {"command": "no-run"}}).encode() + b"\n")
        await writer.drain()
        await entered.wait()
        await self.session.cancel()
        self.assertEqual(json.loads(await reader.readline()), {"allow": False})
        writer.close()
        await writer.wait_closed()
        listener.close()
        await listener.wait_closed()

    async def test_child_eof_finishes_turn_with_error(self):
        reader = asyncio.StreamReader()
        reader.feed_eof()
        self.session.proc = SimpleNamespace(stdout=reader)
        self.session.done = asyncio.get_running_loop().create_future()
        await self.session.read_rpc()
        self.assertTrue(self.session.done.done())
        self.assertIn("closed", self.session.failure)

    async def test_saved_analytics_session_cannot_load_as_coding(self):
        self.server.db.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?)",
                               ("analytics-id", self.tmp.name, "analytics", "/a/session.jsonl", "test", 1))
        with self.assertRaisesRegex(ProtocolError, "profile"):
            await self.server.dispatch("session/load", {"sessionId": "analytics-id", "cwd": self.tmp.name})

    async def test_unsupported_prompt_is_rejected_before_rpc(self):
        with self.assertRaisesRegex(ProtocolError, "Unsupported"):
            await self.session.prompt([{"type": "image", "data": "fake"}])

    async def test_stream_errors_are_preserved(self):
        await self.session.event({"type": "message_end", "message": {"error_message": "provider unavailable"}})
        self.assertEqual(self.session.failure, "provider unavailable")

    async def test_only_verified_permission_modes_are_exposed(self):
        self.server.sessions[self.session.id] = self.session
        for mode in ("plan", "auto-accept-edits", "auto", [], "unknown"):
            with self.assertRaisesRegex(ProtocolError, "Only Ask"):
                await self.server.dispatch("session/set_mode", {"sessionId": self.session.id, "modeId": mode})
        async def idle(command, **params):
            return {"isStreaming": False}
        self.session.rpc = idle
        self.assertEqual(await self.server.dispatch("session/set_mode", {
            "sessionId": self.session.id, "modeId": "default"}), {})

    async def test_model_selector_reports_actual_launch_model(self):
        self.server.sessions[self.session.id] = self.session
        self.session.state = {"model": {"provider": "test", "id": "model-one"}}
        models = self.session.selectors()["models"]
        self.assertEqual(models["currentModelId"], "test/model-one")
        with self.assertRaisesRegex(ProtocolError, "at launch"):
            await self.server.dispatch("session/set_model", {"sessionId": self.session.id, "modelId": "other/model"})

    async def test_analytics_result_emits_a_separate_preview_link(self):
        root = Path(self.tmp.name)
        source = root/'workspace/runs/fixture.html'
        source.parent.mkdir(parents=True)
        original = '<html><head></head><body>Recorded result</body></html>'
        source.write_text(original)
        self.session.analytics_project = root
        await self.session.event({"type":"tool_execution_end", "tool_call_id":"t3", "tool_name":"analytics_run",
            "result":{"content":[{"type":"text","text":"analytics_run: ok"}],
                      "details":{"run":{"id":"fixture","status":"ok"}}}})
        preview = root/'workspace/aion-preview/fixture.html'
        self.assertTrue(preview.is_file())
        self.assertEqual(source.read_text(), original)
        messages = [e["params"]["update"].get("content", {}) for e in self.events if e["params"]["update"]["sessionUpdate"] == "agent_message_chunk"]
        self.assertIn(str(preview.resolve()), "".join(item.get("text", "") for item in messages))

    async def test_missing_analytics_extension_does_not_silently_become_coding(self):
        self.server.args.profile = 'analytics'
        async def empty_commands(command, **kwargs):
            return {"commands":[]}
        self.session.rpc = empty_commands
        with self.assertRaisesRegex(ProtocolError,'Analytics extension'):
            await self.session.commands()


if __name__ == "__main__":
    unittest.main()
