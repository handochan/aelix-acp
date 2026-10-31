import asyncio
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from aelix_acp.server import Server, Session, ProtocolError
from aelix_acp.runtime.controls import thinking_levels
from aelix_acp.runtime.store import save_preferences


class ControlsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = Server(SimpleNamespace(state_dir=self.temp.name, profile="coding"))
        self.session = Session(self.server, "session", self.temp.name)
        self.server.sessions[self.session.id] = self.session
        self.updates, self.calls = [], []
        self.state = {"model": {"provider": "p", "id": "one/with/slashes", "reasoning": True,
                      "thinkingLevelMap": {"low": "low", "high": "high"}, "headers": {"Authorization": "secret"}},
                      "thinkingLevel": "low", "steeringMode": "all", "followUpMode": "one-at-a-time",
                      "autoCompactionEnabled": True, "autoRetryEnabled": False}
        async def send(value):
            self.updates.append(value)
        self.server.send = send
        self.native_mode = "default"
        self.refuse_permission = False
        async def rpc(command, **kwargs):
            self.calls.append((command, kwargs))
            if command == "get_state":
                return dict(self.state)
            if command == "get_available_models":
                return {"models": [self.state["model"], {"provider": "p", "id": "two", "reasoning": False}]}
            if command == "aelix_control":
                if kwargs["method"] == "permissions/set":
                    # The ACP-side policy must already be Ask when native is asked to downgrade.
                    self.policy_at_native_call = self.session.controls.permission_mode
                    if self.refuse_permission:
                        raise ProtocolError("native posture failed", -32603)
                    self.native_mode = kwargs["params"]["mode"]
                    return {"mode": self.native_mode}
                if kwargs["method"] == "permissions/get":
                    return {"mode": self.native_mode}
                return {"version": 1, "methods": ["permissions/get", "permissions/set"]}
            if command == "set_model":
                self.state["model"] = {"provider": kwargs["provider"], "id": kwargs["modelId"], "reasoning": False}
                self.state["thinkingLevel"] = "off"
            if command == "set_thinking_level":
                self.state["thinkingLevel"] = kwargs["level"]
            return {}
        self.session.rpc = rpc
        await self.session.controls.refresh()

    async def asyncTearDown(self):
        self.server.db.close()
        self.temp.cleanup()

    async def test_catalog_does_not_expose_provider_headers(self):
        selectors = self.session.selectors()
        self.assertNotIn("secret", str(selectors))
        self.assertEqual(selectors["models"]["currentModelId"], "p/one/with/slashes")

    async def test_model_selection_preserves_slashes_and_publishes_full_catalog(self):
        result = await self.server.dispatch("session/set_config_option", {"sessionId": "session", "configId": "model", "value": "p/two"})
        self.assertIn(("set_model", {"provider": "p", "modelId": "two"}), self.calls)
        self.assertEqual(next(o for o in result["configOptions"] if o["id"] == "thinking_level")["currentValue"], "off")
        self.assertEqual(self.updates[-1]["params"]["update"]["configOptions"], result["configOptions"])

    async def test_invalid_model_does_not_reach_backend(self):
        self.calls.clear()
        with self.assertRaises(ProtocolError):
            await self.session.controls.set("model", "evil/model")
        self.assertEqual(self.calls, [])

    async def test_failed_hook_after_model_mutation_refreshes_actual_selection(self):
        original = self.session.rpc
        async def fail_after_set(command, **kwargs):
            result = await original(command, **kwargs)
            if command == "set_model":
                raise ProtocolError("model hook failed", -32603)
            return result
        self.session.rpc = fail_after_set
        with self.assertRaisesRegex(ProtocolError, "hook failed"):
            await self.session.controls.set("model", "p/two")
        self.assertEqual(self.session.selectors()["models"]["currentModelId"], "p/two")

    async def test_reasoning_selection_uses_live_state(self):
        await self.session.controls.set("thinking_level", "high")
        self.assertEqual(self.session.state["thinkingLevel"], "high")
        self.assertIn(("set_thinking_level", {"level": "high"}), self.calls)

    async def test_invalid_reasoning_level_does_not_mutate_state(self):
        with self.assertRaises(ProtocolError):
            await self.session.controls.set("thinking_level", "unlimited")
        self.assertEqual(self.session.state["thinkingLevel"], "low")

    def saved_mode(self):
        row = self.server.db.execute("SELECT value FROM preferences WHERE session_id='session'").fetchone()
        return __import__("json").loads(row[0])["permission_mode"]

    async def test_only_ask_and_yolo_are_offered(self):
        selectors = self.session.selectors()
        self.assertEqual([mode["id"] for mode in selectors["modes"]["availableModes"]], ["default", "yolo"])
        option = next(o for o in selectors["configOptions"] if o["id"] == "permission_mode")
        self.assertEqual([entry["value"] for entry in option["options"]], ["default", "yolo"])
        for mode in ("plan", "auto-accept-edits", "auto"):
            with self.assertRaisesRegex(ProtocolError, "Unsupported value"):
                await self.session.controls.set("permission_mode", mode)
        self.assertNotIn("permissions/set", [kwargs.get("method") for _command, kwargs in self.calls])

    async def test_yolo_is_persisted_only_after_native_confirms(self):
        await self.session.controls.set("permission_mode", "yolo")
        self.assertEqual(self.session.selectors()["modes"]["currentModeId"], "yolo")
        self.assertEqual(self.native_mode, "yolo")
        self.assertEqual(self.saved_mode(), "yolo")
        self.assertEqual(self.session.selectors()["_meta"]["aelix/permission"]["nativeMode"], "yolo")
        self.assertEqual(self.updates[-2]["params"]["update"], {"sessionUpdate": "current_mode_update", "currentModeId": "yolo"})

    async def test_failed_upgrade_never_grants_yolo(self):
        self.refuse_permission = True
        with self.assertRaisesRegex(ProtocolError, "native posture failed"):
            await self.session.controls.set("permission_mode", "yolo")
        self.assertEqual(self.session.controls.permission_mode, "default")
        self.assertEqual(self.saved_mode(), "default")
        self.assertEqual(self.updates[-1]["params"]["update"], {"sessionUpdate": "current_mode_update", "currentModeId": "default"})

    async def test_failed_downgrade_still_revokes_client_yolo(self):
        await self.session.controls.set("permission_mode", "yolo")
        self.refuse_permission = True
        with self.assertRaisesRegex(ProtocolError, "native posture failed"):
            await self.session.controls.set("permission_mode", "default")
        self.assertEqual(self.policy_at_native_call, "default")
        self.assertEqual(self.session.controls.permission_mode, "default")
        self.assertEqual(self.saved_mode(), "default")

    async def test_unconfirmed_native_posture_is_a_failure(self):
        original = self.session.rpc
        async def wrong(command, **kwargs):
            if command == "aelix_control" and kwargs["method"] == "permissions/set":
                return {"mode": "default"}
            return await original(command, **kwargs)
        self.session.rpc = wrong
        with self.assertRaisesRegex(ProtocolError, "did not confirm"):
            await self.session.controls.set("permission_mode", "yolo")
        self.assertEqual(self.session.controls.permission_mode, "default")

    async def test_pre_mode_records_stay_ask_and_unknown_saved_modes_refuse(self):
        self.server.db.execute("CREATE TABLE permission_modes (id TEXT PRIMARY KEY, mode TEXT NOT NULL)")
        self.assertEqual(Session(self.server, "fresh", self.temp.name).launch_mode, "default")
        self.server.db.execute("INSERT INTO permission_modes VALUES('legacy','yolo')")
        self.assertEqual(Session(self.server, "legacy", self.temp.name).launch_mode, "yolo")
        for stored in ("auto", "plan"):
            with self.server.db:
                save_preferences(self.server.db, "stale", {"permission_mode": stored})
            with self.assertRaises(ProtocolError) as caught:
                Session(self.server, "stale", self.temp.name)
            self.assertEqual(caught.exception.data["kind"], "unsupported_saved_mode")
            self.assertEqual(caught.exception.data["mode"], stored)

    async def test_restored_mode_notification_follows_open_response(self):
        events = []
        async def send(value):
            events.append(value)
        self.server.send = send
        self.session.controls.permission_mode = "yolo"
        async def dispatch(method, params):
            return self.session.selectors()
        with patch.object(self.server, "dispatch", dispatch):
            await self.server.handle({"jsonrpc": "2.0", "id": "open", "method": "session/load",
                                      "params": {"sessionId": "session"}})
        self.assertEqual(events[0]["id"], "open")
        self.assertEqual(events[1]["params"]["update"], {"sessionUpdate": "current_mode_update", "currentModeId": "yolo"})

    async def test_busy_session_rejects_configuration(self):
        self.session.done = asyncio.get_running_loop().create_future()
        with self.assertRaisesRegex(ProtocolError, "running turn"):
            await self.session.controls.set("permission_mode", "yolo")
        self.assertEqual(self.session.controls.permission_mode, "default")
        self.session.done.cancel()


class ThinkingLevelTests(unittest.TestCase):
    def test_non_reasoning_model_only_offers_off(self):
        self.assertEqual(thinking_levels({"reasoning": False}, {}), ["off"])

    def test_model_thinking_map_is_respected(self):
        self.assertEqual(thinking_levels({"reasoning": True, "thinkingLevelMap": {"low": 1, "max": 2}}, {}), ["off", "low", "max"])
