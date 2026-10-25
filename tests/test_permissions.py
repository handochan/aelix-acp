"""Mode changes must not widen authority after failed/cancelled restart."""
import asyncio
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from aelix_acp.server import ProtocolError, Server, Session


class PermissionTransitionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.server = Server(SimpleNamespace(state_dir=self.temp.name, profile="coding"))
        self.events = []
        async def send(event): self.events.append(event)
        self.server.send = send
        self.session = Session(self.server, "id", str(self.root))
        self.file = self.root / "native.jsonl"
        self.file.write_text("{}\n")
        self.session.state = {"sessionFile": str(self.file)}
        self.session.save()
        self.server.sessions["id"] = self.session
        async def idle(command, **params): return {"isStreaming": False}
        self.session.rpc = idle

    async def asyncTearDown(self):
        for session in self.server.sessions.values(): await session.close()
        self.server.db.close()
        self.temp.cleanup()

    async def test_pre_mode_session_defaults_to_ask_and_unknown_saved_mode_refuses(self):
        self.server.db.execute("DELETE FROM permission_modes")
        self.assertEqual(self.server.stored_mode("id"), "default")
        self.server.store_mode("id", "auto")
        with self.assertRaisesRegex(ProtocolError, "unsupported"):
            self.server.stored_mode("id")

    async def test_restored_mode_notification_follows_open_response(self):
        async def dispatch(method, params): return self.session.selectors()
        with patch.object(self.server, "dispatch", dispatch):
            await self.server.handle({"jsonrpc": "2.0", "id": "open", "method": "session/load", "params": {"sessionId": "id"}})
        self.assertEqual(self.events[0]["id"], "open")
        self.assertEqual(self.events[1]["params"]["update"], {"sessionUpdate": "current_mode_update", "currentModeId": "default"})

    async def test_yolo_persists_only_after_successful_commands(self):
        server = self.server
        native = str(self.file)
        async def start(candidate, session_file=None):
            self.assertEqual(server.stored_mode("id"), "default")
            candidate.state = {"sessionFile": session_file}
        async def commands(candidate):
            self.assertEqual(server.stored_mode("id"), "default")
        with patch.object(Session, "start", start), patch.object(Session, "commands", commands):
            self.assertEqual(await server.set_mode({"sessionId": "id", "modeId": "yolo"}), {})
        self.assertEqual(server.stored_mode("id"), "yolo")
        candidate = server.sessions["id"]
        self.assertEqual(candidate.state["sessionFile"], native)
        self.assertEqual(candidate.selectors()["modes"]["currentModeId"], "yolo")

    async def test_failed_downgrade_stops_worker_and_revokes_saved_yolo(self):
        self.session.permission_mode = "yolo"
        self.server.store_mode("id", "yolo")
        directory = self.root / "old-socket-dir"
        directory.mkdir()
        self.session.socket_dir = str(directory)
        async def failed(candidate, session_file=None):
            self.assertEqual(self.server.stored_mode("id"), "default")
            raise RuntimeError("synthetic startup failure")
        with patch.object(Session, "start", failed):
            with self.assertRaisesRegex(ProtocolError, "worker stopped"):
                await self.server.set_mode({"sessionId": "id", "modeId": "default"})
        self.assertEqual(self.server.stored_mode("id"), "default")
        self.assertNotIn("id", self.server.sessions)
        self.assertFalse(directory.exists())

    async def test_missing_transcript_downgrade_stops_yolo(self):
        self.session.permission_mode = "yolo"
        self.server.store_mode("id", "yolo")
        self.file.unlink()
        with self.assertRaisesRegex(ProtocolError, "worker stopped"):
            await self.server.set_mode({"sessionId": "id", "modeId": "default"})
        self.assertEqual(self.server.stored_mode("id"), "default")
        self.assertNotIn("id", self.server.sessions)

    async def test_failed_upgrade_never_restores_yolo(self):
        async def failed(candidate, session_file=None): raise RuntimeError("synthetic")
        with patch.object(Session, "start", failed):
            with self.assertRaises(ProtocolError):
                await self.server.set_mode({"sessionId": "id", "modeId": "yolo"})
        self.assertEqual(self.server.stored_mode("id"), "default")
        self.assertNotIn("id", self.server.sessions)

    async def test_same_mode_and_load_refused_during_prompt(self):
        self.session.done = asyncio.get_running_loop().create_future()
        for method, params in [("session/set_mode", {"modeId": "default"}),
                               ("session/load", {"cwd": str(self.root)})]:
            with self.assertRaisesRegex(ProtocolError, "busy"):
                await self.server.dispatch(method, {"sessionId": "id", **params})
        self.assertIs(self.server.sessions["id"], self.session)
        self.assertEqual(self.events, [])
        self.session.done.cancel()

    async def test_native_streaming_after_agent_end_is_still_busy(self):
        async def streaming(command, **params): return {"isStreaming": True}
        self.session.rpc = streaming
        with self.assertRaisesRegex(ProtocolError, "still streaming"):
            await self.server.set_mode({"sessionId": "id", "modeId": "yolo"})
        self.assertEqual(self.server.stored_mode("id"), "default")

    async def test_prompt_finalization_cannot_overwrite_mode_revocation(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def rpc(command, **params):
            if command == "prompt":
                self.session.done.set_result(None)
                return {}
            entered.set()
            await release.wait()
            return {"sessionFile": str(self.file), "isStreaming": False}
        self.session.rpc = rpc
        prompt = asyncio.create_task(self.session.prompt([{"type": "text", "text": "synthetic"}]))
        await entered.wait()
        with self.assertRaisesRegex(ProtocolError, "busy"):
            await self.server.set_mode({"sessionId": "id", "modeId": "yolo"})
        release.set()
        await prompt
        self.assertFalse(self.session.prompt_active)

    async def test_cancelled_transition_blocks_prompt_load_and_other_mode_until_closed(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def waiting(candidate, session_file=None):
            candidate.state = {"sessionFile": session_file}
            entered.set()
            await release.wait()
        with patch.object(Session, "start", waiting):
            task = asyncio.create_task(self.server.set_mode({"sessionId": "id", "modeId": "yolo"}))
            await entered.wait()
            for method, params in [("session/prompt", {"prompt": [{"type": "text", "text": "test"}]}),
                                   ("session/load", {"cwd": str(self.root)}),
                                   ("session/set_mode", {"modeId": "default"})]:
                with self.assertRaises(ProtocolError):
                    await self.server.dispatch(method, {"sessionId": "id", **params})
            await self.server.dispatch("session/cancel", {"sessionId": "id"})
            with self.assertRaisesRegex(ProtocolError, "cancelled"):
                await task
        self.assertEqual(self.server.stored_mode("id"), "default")
        self.assertNotIn("id", self.server.sessions)
        self.assertNotIn("id", self.server.mutations)

    async def test_repeated_cancel_cannot_interrupt_transition_cleanup(self):
        entered, cleanup_entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        close = Session.close
        async def start(candidate, session_file=None):
            entered.set()
            await asyncio.Event().wait()
        async def slow_close(candidate):
            if candidate is not self.session:
                cleanup_entered.set()
                await release.wait()
            await close(candidate)
        with patch.object(Session, "start", start), patch.object(Session, "close", slow_close):
            transition = asyncio.create_task(self.server.set_mode({"sessionId": "id", "modeId": "yolo"}))
            await entered.wait()
            first = asyncio.create_task(self.server.dispatch("session/cancel", {"sessionId": "id"}))
            await cleanup_entered.wait()
            second = asyncio.create_task(self.server.dispatch("session/cancel", {"sessionId": "id"}))
            await asyncio.sleep(0)
            self.assertIn("id", self.server.mutations)
            self.assertFalse(first.done() or second.done())
            release.set()
            await asyncio.gather(first, second)
            with self.assertRaisesRegex(ProtocolError, "cancelled"):
                await transition
        self.assertNotIn("id", self.server.mutations)

    async def test_cancel_during_load_old_worker_shutdown_finishes_cleanup_without_start(self):
        entered, release = asyncio.Event(), asyncio.Event()
        close = Session.close
        directory = self.root / "old-load-socket"
        directory.mkdir()
        self.session.socket_dir = str(directory)
        async def slow_close(session):
            if session is self.session:
                entered.set()
                await release.wait()
            await close(session)
        async def unexpected_start(session, session_file=None):
            self.fail("Cancelled load must not start a replacement worker")
        with patch.object(Session, "close", slow_close), patch.object(Session, "start", unexpected_start):
            load = asyncio.create_task(self.server.dispatch("session/load", {"sessionId": "id", "cwd": str(self.root)}))
            await entered.wait()
            cancel = asyncio.create_task(self.server.dispatch("session/cancel", {"sessionId": "id"}))
            await asyncio.sleep(0)
            self.assertIn("id", self.server.mutations)
            release.set()
            await cancel
            with self.assertRaisesRegex(ProtocolError, "cancelled"):
                await load
        self.assertFalse(directory.exists())
        self.assertNotIn("id", self.server.sessions)
        self.assertNotIn("id", self.server.mutations)
