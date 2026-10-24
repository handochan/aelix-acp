import asyncio
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from aelix_acp.server import Server, Session, ProtocolError
from aelix_acp.wire.errors import BackendError
from aelix_acp.runtime.controls import thinking_levels
from aelix_acp.runtime.policy import permission_decision


class ControlsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = Server(SimpleNamespace(state_dir=self.temp.name, profile="coding", runtime_profile="control-v1"))
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
        async def rpc(command, **kwargs):
            self.calls.append((command, kwargs))
            if command == "get_state":
                return dict(self.state)
            if command == "get_available_models":
                return {"models": [self.state["model"], {"provider": "p", "id": "two", "reasoning": False}]}
            if command == "aelix_control":
                return {"version": 1, "methods": ["permissions/set"],
                        "permissionModes": ["default", "plan", "auto-accept-edits", "auto", "yolo"]}
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

    async def test_permission_mode_is_persisted_and_not_only_echoed(self):
        await self.session.controls.set("permission_mode", "plan")
        self.assertEqual(self.session.selectors()["modes"]["currentModeId"], "plan")
        row = self.server.db.execute("SELECT value FROM preferences WHERE session_id='session'").fetchone()
        self.assertIn('"plan"', row[0])

    async def test_busy_session_rejects_configuration(self):
        self.session.done = asyncio.get_running_loop().create_future()
        with self.assertRaisesRegex(ProtocolError, "running turn"):
            await self.session.controls.set("permission_mode", "yolo")
        self.assertEqual(self.session.controls.permission_mode, "default")
        self.session.done.cancel()

    async def test_beta2_does_not_probe_or_advertise_native_controls(self):
        self.server.args.runtime_profile = "beta2"
        self.calls.clear()
        await self.session.controls.refresh()
        self.assertNotIn("aelix_control", [command for command, _arguments in self.calls])
        self.assertEqual(self.session.controls.permission_modes()[0][0], "default")
        self.assertEqual(len(self.session.controls.permission_modes()), 1)

    async def test_missing_registry_preserves_current_model_without_a_false_selector(self):
        original = self.session.rpc
        async def unbound_registry(command, **arguments):
            if command == "get_available_models":
                raise BackendError("no ModelRegistry configured", command)
            return await original(command, **arguments)
        self.session.rpc = unbound_registry
        await self.session.controls.refresh()
        self.assertNotIn("model", {option["id"] for option in self.session.controls.options()})
        self.assertEqual(self.session.selectors()["models"]["currentModelId"], "p/one/with/slashes")

    async def test_model_catalog_timeout_is_not_silently_treated_as_legacy_support(self):
        original = self.session.rpc
        async def timeout(command, **arguments):
            if command == "get_available_models":
                raise ProtocolError("RPC deadline", -32007)
            return await original(command, **arguments)
        self.session.rpc = timeout
        with self.assertRaises(ProtocolError) as error:
            await self.session.controls.refresh()
        self.assertEqual(error.exception.code, -32007)


class PolicyTests(unittest.TestCase):
    def test_plan_does_not_trust_an_extension_named_read(self):
        self.assertEqual(permission_decision("plan", None), "deny")
        self.assertEqual(permission_decision("plan", "read_only"), "allow")

    def test_accept_edits_does_not_implicitly_allow_shell_or_plugin_tools(self):
        for provenance in [None, "bash", "delegation"]:
            self.assertEqual(permission_decision("auto-accept-edits", provenance), "ask")
        self.assertEqual(permission_decision("auto-accept-edits", "write"), "ask")
        self.assertEqual(permission_decision("auto-accept-edits", "write", write_allowed=True), "allow")

    def test_default_and_unknown_modes_require_approval(self):
        for mode in ["default", "corrupt-stored-value"]:
            self.assertEqual(permission_decision(mode, "read_only"), "ask")

    def test_non_reasoning_model_only_offers_off(self):
        self.assertEqual(thinking_levels({"reasoning": False}, {}), ["off"])

    def test_model_thinking_map_is_respected(self):
        self.assertEqual(thinking_levels({"reasoning": True, "thinkingLevelMap": {"low": 1, "max": 2}}, {}), ["off", "low", "max"])
