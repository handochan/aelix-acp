import asyncio
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from aelix_acp.server import ProtocolError, Server, Session


class AdapterTests(unittest.IsolatedAsyncioTestCase):
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
        listener = await asyncio.start_server(self.session.permission, host="127.0.0.1", port=0)
        address = listener.sockets[0].getsockname()
        await self.session.event({"type": "tool_execution_start", "tool_call_id": "t1",
                                  "tool_name": "write", "args": {"path": "output.html"}})
        reader, writer = await asyncio.open_connection(address[0], address[1])
        writer.write(json.dumps({"toolCallId": "t1", "name": "write", "args": {"path": "output.html"}}).encode() + b"\n")
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

    async def test_permission_token_and_known_tool_identity_are_required(self):
        self.session.permission_token = "correct-secret"
        self.session.controls.permission_mode = "yolo"
        await self.session.event({"type": "tool_execution_start", "toolCallId": "t1", "toolName": "write", "args": {}})
        listener = await asyncio.start_server(self.session.permission, host="127.0.0.1", port=0)
        address = listener.sockets[0].getsockname()
        try:
            for token, identifier, name in [("wrong", "t1", "write"), ("correct-secret", "unknown", "write"),
                                             ("correct-secret", "t1", "bash"), ("correct-secret", "t1", "write")]:
                reader, writer = await asyncio.open_connection(*address)
                writer.write((json.dumps({"token": token, "toolCallId": identifier, "name": name, "args": {}}) + "\n").encode())
                await writer.drain()
                if token == "correct-secret" and identifier == "unknown":
                    await asyncio.sleep(0.02)
                    await self.session.cancel()
                result = json.loads(await asyncio.wait_for(reader.readline(), 2))
                self.assertEqual(result["allow"], token == "correct-secret" and identifier == "t1" and name == "write")
                self.session.cancelled = False
                writer.close()
                await writer.wait_closed()
        finally:
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
        listener = await asyncio.start_server(self.session.permission, host="127.0.0.1", port=0)
        address = listener.sockets[0].getsockname()
        reader, writer = await asyncio.open_connection(address[0], address[1])
        writer.write(b'{"toolCallId":"t2","name":"bash","args":{"command":"no-run"}}\n')
        await writer.drain()
        await entered.wait()
        await self.session.cancel()
        self.assertEqual(json.loads(await reader.readline()), {"allow": False})
        writer.close()
        await writer.wait_closed()
        listener.close()
        await listener.wait_closed()

    async def test_authenticated_bridge_may_arrive_before_the_stdout_tool_event(self):
        self.session.permission_token = "secret"
        self.session.controls.permission_mode = "yolo"
        listener = await asyncio.start_server(self.session.permission, "127.0.0.1", 0)
        reader, writer = await asyncio.open_connection(*listener.sockets[0].getsockname())
        try:
            writer.write(b'{"token":"secret","toolCallId":"early","name":"write","args":{}}\n')
            await writer.drain()
            received = asyncio.create_task(reader.readline())
            await asyncio.sleep(0.02)
            self.assertFalse(received.done())
            await self.session.event({"type": "tool_execution_start", "toolCallId": "early", "toolName": "write", "args": {}})
            self.assertEqual(json.loads(await asyncio.wait_for(received, 2)), {"allow": True})
        finally:
            writer.close()
            await writer.wait_closed()
            listener.close()
            await listener.wait_closed()

    async def test_cancel_during_slash_preparation_never_submits_the_expanded_prompt(self):
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def prepare(session, text):
            entered.set()
            await release.wait()
            return True, "Expanded model prompt"
        async def rpc(command, **kwargs):
            calls.append(command)
            return {}
        self.session.rpc = rpc
        with patch("aelix_acp.session.execute_slash", prepare):
            task = asyncio.create_task(self.session.prompt([{"type": "text", "text": "/template"}]))
            await entered.wait()
            await self.session.cancel()
            release.set()
            self.assertEqual(await task, {"stopReason": "cancelled"})
        self.assertNotIn("prompt", calls)

    async def test_model_prompt_waits_until_native_recovery_is_idle(self):
        states = []
        async def rpc(command, **kwargs):
            if command == "get_state":
                states.append(command)
                return {"isStreaming": len(states) < 3}
            return {}
        self.session.rpc = rpc
        self.assertEqual(await self.session.prompt([{"type": "text", "text": "Respond"}]), {"stopReason": "end_turn"})
        self.assertEqual(len(states), 3)

    async def test_compaction_deadline_sends_native_abort_before_returning(self):
        calls = []
        session = self.session
        class Input:
            def write(self, raw):
                payload = json.loads(raw)
                calls.append(payload["type"])
                if payload["type"] == "abort":
                    asyncio.get_running_loop().call_soon(session.pending[payload["id"]].set_result, {})
            async def drain(self):
                pass
        self.session.proc = SimpleNamespace(stdin=Input(), returncode=None)
        with self.assertRaises(ProtocolError) as caught:
            await self.session.rpc("compact", _timeout=0.01)
        self.assertEqual(caught.exception.code, -32007)
        self.assertEqual(calls, ["compact", "abort"])

    async def test_child_eof_finishes_turn_with_error(self):
        reader = asyncio.StreamReader()
        reader.feed_eof()
        self.session.proc = SimpleNamespace(stdout=reader, returncode=0)
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

    async def test_unknown_permission_mode_is_rejected_without_backend_mutation(self):
        self.server.sessions[self.session.id] = self.session
        with self.assertRaisesRegex(ProtocolError, "Unsupported value"):
            await self.server.dispatch("session/set_mode", {"sessionId": self.session.id, "modeId": "bypass-everything"})
        self.assertEqual(self.session.controls.permission_mode, "default")

    async def test_model_selector_reports_actual_launch_model(self):
        self.server.sessions[self.session.id] = self.session
        self.session.state = {"model": {"provider": "test", "id": "model-one"}}
        models = self.session.selectors()["models"]
        self.assertEqual(models["currentModelId"], "test/model-one")
        with self.assertRaisesRegex(ProtocolError, "Unsupported value"):
            await self.server.dispatch("session/set_model", {"sessionId": self.session.id, "modelId": "other/model"})

    async def test_analytics_result_emits_a_separate_preview_link(self):
        root = Path(self.tmp.name)
        source = root/'workspace/runs/fixture.html'
        source.parent.mkdir(parents=True)
        original = '<html><head></head><body>Recorded result</body></html>'
        source.write_text(original, encoding="utf-8")
        self.session.analytics_project = root
        await self.session.event({"type":"tool_execution_end", "tool_call_id":"t3", "tool_name":"analytics_run",
            "result":{"content":[{"type":"text","text":"analytics_run: ok"}],
                      "details":{"run":{"id":"fixture","status":"ok"}}}})
        preview = root/'workspace/aion-preview/fixture.html'
        self.assertTrue(preview.is_file())
        self.assertEqual(source.read_text(encoding="utf-8"), original)
        rendered = "".join(event["params"]["update"]["content"]["text"] for event in self.events
                           if event["params"]["update"]["sessionUpdate"] == "agent_message_chunk")
        self.assertIn(str(preview.resolve()), rendered)

    async def test_missing_analytics_extension_does_not_silently_become_coding(self):
        self.server.args.profile = 'analytics'
        async def empty_commands(command, **kwargs):
            return {"commands":[]}
        self.session.rpc = empty_commands
        with self.assertRaisesRegex(ProtocolError,'Analytics extension'):
            await self.session.commands()


if __name__ == "__main__":
    unittest.main()
