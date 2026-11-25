"""A session whose native runtime has no current model (ACP #62)."""
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
from aelix_acp.runtime.store import load_preferences

# What native get_state reports when nothing chose a model (aelix-ai 83714045,
# no defaultProvider/defaultModel and no --model): an empty placeholder.
PLACEHOLDER = {"id": "", "name": "unknown", "provider": "", "api": "unknown", "thinkingLevelMap": None,
               "maxTokens": 0, "contextWindow": 0, "reasoning": False, "baseUrl": "", "input": []}
AVAILABLE = [
    {"id": "reasoner", "name": "Reasoner", "provider": "fixture", "api": "openai-completions", "reasoning": True,
     "thinkingLevelMap": None, "input": ["text"], "headers": {"Authorization": "secret-header"}},
    {"id": "plain/with/slash", "name": "Plain", "provider": "fixture", "api": "openai-completions",
     "reasoning": False, "input": ["text"]},
]
REASONING = ["off", "minimal", "low", "medium", "high"]
REFUSAL = {"kind": "model_selection_required", "configId": "model", "availableModels": 2, "retryable": False}


class NoCurrentModelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = Server(SimpleNamespace(state_dir=self.temp.name, profile="coding"))
        self.session = Session(self.server, "session", self.temp.name)
        self.server.sessions[self.session.id] = self.session
        self.updates, self.calls = [], []
        self.available = [dict(model) for model in AVAILABLE]
        self.state = {"model": dict(PLACEHOLDER), "thinkingLevel": "off", "isStreaming": False,
                      "steeringMode": "all", "followUpMode": "all", "messageCount": 0}

        async def send(value):
            self.updates.append(value)

        async def rpc(command, **kwargs):
            self.calls.append((command, kwargs))
            if command == "get_state":
                return dict(self.state)
            if command == "get_available_models":
                return {"models": self.available}
            if command == "aelix_control" and kwargs["method"] == "capabilities":
                reasoning = self.state["model"].get("reasoning")
                return {"version": 1, "methods": [], "thinkingLevels": REASONING if reasoning else ["off"]}
            if command == "set_model":
                model = next(m for m in self.available
                             if (m["provider"], m["id"]) == (kwargs["provider"], kwargs["modelId"]))
                self.state.update(model=dict(model), thinkingLevel="off")
            if command == "set_thinking_level":
                self.state["thinkingLevel"] = kwargs["level"]
            return {}

        self.server.send = send
        self.session.rpc = rpc
        await self.session.controls.refresh()

    async def asyncTearDown(self):
        await self.session.live_history.flush()
        self.server.db.close()
        self.temp.cleanup()

    def option(self, options, config_id):
        return next((option for option in options if option["id"] == config_id), None)

    def published(self):
        return [update["params"]["update"] for update in self.updates
                if update["params"]["update"]["sessionUpdate"] == "config_option_update"]

    def recorded(self, kind):
        return [json.loads(value) for value, in self.server.db.execute("SELECT value FROM events ORDER BY sequence")
                if json.loads(value)["sessionUpdate"] == kind]

    async def prompt(self, text):
        return await self.server.dispatch("session/prompt", {"sessionId": self.session.id,
                                                              "prompt": [{"type": "text", "text": text}]})

    async def test_available_models_are_advertised_with_an_empty_current_value(self):
        selectors = self.session.selectors()
        model = self.option(selectors["configOptions"], "model")
        self.assertEqual((model["category"], model["type"], model["currentValue"]), ("model", "select", ""))
        self.assertEqual([entry["value"] for entry in model["options"]], ["fixture/reasoner", "fixture/plain/with/slash"])
        self.assertEqual(selectors["models"], {"currentModelId": "", "availableModels": [
            {"modelId": "fixture/reasoner", "name": "Reasoner", "description": "fixture"},
            {"modelId": "fixture/plain/with/slash", "name": "Plain", "description": "fixture"}]})
        self.assertIsNone(self.option(selectors["configOptions"], "thinking_level"))
        self.assertNotIn("secret-header", json.dumps(selectors))
        # Nothing selected is not a selection to save or restore.
        self.assertNotIn("model", self.session.controls.selections())

    async def test_the_empty_current_value_cannot_be_selected(self):
        self.calls.clear()
        for value in ("", "fixture/"):
            with self.assertRaisesRegex(ProtocolError, "Unsupported value for model"):
                await self.session.controls.set("model", value)
        self.assertEqual(self.calls, [])

    async def test_no_available_models_offers_no_model_selector(self):
        self.available = []
        await self.session.controls.refresh()
        selectors = self.session.selectors()
        self.assertIsNone(self.option(selectors["configOptions"], "model"))
        self.assertNotIn("models", selectors)
        with self.assertRaises(ProtocolError) as refused:
            await self.prompt("Hello")
        self.assertEqual(refused.exception.code, -32004)
        self.assertEqual(refused.exception.data, {**REFUSAL, "availableModels": 0})
        self.assertIn("configure a provider", str(refused.exception))

    async def test_choice_is_applied_and_publishes_the_real_model_with_its_reasoning_levels(self):
        result = await self.server.dispatch("session/set_config_option", {
            "sessionId": self.session.id, "configId": "model", "value": "fixture/reasoner"})
        self.assertIn(("set_model", {"provider": "fixture", "modelId": "reasoner"}), self.calls)
        self.assertEqual(self.option(result["configOptions"], "model")["currentValue"], "fixture/reasoner")
        reasoning = self.option(result["configOptions"], "thinking_level")
        self.assertEqual((reasoning["category"], reasoning["currentValue"]), ("thought_level", "off"))
        self.assertEqual([entry["value"] for entry in reasoning["options"]], REASONING)
        self.assertEqual(self.published()[-1]["configOptions"], result["configOptions"])
        config = load_preferences(self.server.db, self.session.id)["config"]
        self.assertEqual((config["model"], config["thinking_level"]), ("fixture/reasoner", "off"))

        await self.session.controls.set("thinking_level", "high")
        self.assertIn(("set_thinking_level", {"level": "high"}), self.calls)

    async def test_legacy_model_selection_is_applied_from_no_model(self):
        self.assertEqual(await self.server.dispatch("session/set_model", {
            "sessionId": self.session.id, "modelId": "fixture/plain/with/slash"}), {})
        self.assertIn(("set_model", {"provider": "fixture", "modelId": "plain/with/slash"}), self.calls)
        self.assertEqual(self.session.selectors()["models"]["currentModelId"], "fixture/plain/with/slash")
        reasoning = self.option(self.published()[-1]["configOptions"], "thinking_level")
        self.assertEqual([entry["value"] for entry in reasoning["options"]], ["off"])

    async def test_choice_is_refused_during_a_running_turn(self):
        self.session.done = asyncio.get_running_loop().create_future()
        try:
            with self.assertRaisesRegex(ProtocolError, "running turn"):
                await self.session.controls.set("model", "fixture/reasoner")
        finally:
            self.session.done.cancel()
        self.assertNotIn("set_model", [command for command, _kwargs in self.calls])

    async def test_prompt_without_a_model_is_refused_before_native_and_before_it_is_recorded(self):
        self.calls.clear()
        with self.assertRaises(ProtocolError) as refused:
            await self.prompt("Hello")
        self.assertEqual(refused.exception.code, -32004)
        self.assertEqual(refused.exception.data, REFUSAL)
        commands = [command for command, _kwargs in self.calls]
        self.assertNotIn("prompt", commands)
        self.assertIn("get_state", commands)  # The refusal reads native again first.
        self.assertEqual(self.recorded("user_message_chunk"), [])

        await self.session.controls.set("model", "fixture/reasoner")
        self.assertEqual(await self.prompt("Hello"), {"stopReason": "end_turn"})
        self.assertIn(("prompt", {"message": "Hello"}), self.calls)

    async def test_compaction_without_a_model_is_refused(self):
        self.calls.clear()
        with self.assertRaises(ProtocolError) as refused:
            await self.prompt("/compact")
        self.assertEqual(refused.exception.data, REFUSAL)
        self.assertNotIn("compact", [command for command, _kwargs in self.calls])

    async def test_model_slash_command_selects_without_a_model(self):
        self.assertEqual(await self.prompt("/model fixture/reasoner"), {"stopReason": "end_turn"})
        self.assertIn(("set_model", {"provider": "fixture", "modelId": "reasoner"}), self.calls)
        self.assertNotIn("prompt", [command for command, _kwargs in self.calls])

    async def test_expanded_template_without_a_model_is_refused_before_native(self):
        async def prepare(_session, _text):
            return True, "Expanded model prompt"

        with patch("aelix_acp.session.execute_slash", prepare), self.assertRaises(ProtocolError) as refused:
            await self.prompt("/template")
        self.assertEqual(refused.exception.data, REFUSAL)
        self.assertNotIn("prompt", [command for command, _kwargs in self.calls])

    async def test_a_model_native_reports_on_the_second_read_is_used_and_published(self):
        # Native changed its model through a path that did not refresh the selectors.
        self.state.update(model=dict(self.available[1]))
        self.assertEqual(await self.prompt("Hello"), {"stopReason": "end_turn"})
        self.assertIn(("prompt", {"message": "Hello"}), self.calls)
        model = self.option(self.published()[-1]["configOptions"], "model")
        self.assertEqual(model["currentValue"], "fixture/plain/with/slash")

    async def test_a_state_without_a_model_field_is_no_evidence(self):
        # Older fixtures and runtimes do not report a model; native decides.
        del self.state["model"]
        self.session.state = dict(self.state)
        self.assertEqual(await self.prompt("Hello"), {"stopReason": "end_turn"})
        self.assertIn(("prompt", {"message": "Hello"}), self.calls)


# A native worker that, like aelix-ai 83714045 with no default model, starts on the
# placeholder, records set_model in its session file at once, and restores a model
# from that file only when the file also has messages and the model is available.
FAKE_NATIVE = '''import json, os, sys, uuid
from pathlib import Path
PLACEHOLDER = %(placeholder)s
AVAILABLE = %(available)s
REASONING = %(reasoning)s
Path(os.environ["AELIX_ACP_READY_FILE"]).write_text(os.environ["AELIX_ACP_NONCE"])
mode = sys.argv[sys.argv.index("--permission-mode") + 1]
if "--session" in sys.argv:
    session_file = Path(sys.argv[sys.argv.index("--session") + 1])
else:
    session_file = Path.cwd() / "native" / (uuid.uuid4().hex + ".jsonl")
entries = [json.loads(line) for line in session_file.read_text().splitlines()] if session_file.is_file() else []
messages = sum(entry["type"] == "message" for entry in entries)
model, level = dict(PLACEHOLDER), "off"
if messages:
    for entry in entries:
        if entry["type"] == "model_change":
            found = [m for m in AVAILABLE if (m["provider"], m["id"]) == (entry["provider"], entry["modelId"])]
            model = dict(found[0]) if found else dict(PLACEHOLDER)
        elif entry["type"] == "thinking_level_change":
            level = entry["level"]
def record(entry):
    session_file.parent.mkdir(exist_ok=True)
    with session_file.open("a") as stream:
        stream.write(json.dumps(entry) + "\\n")
    entries.append(entry)
for line in sys.stdin:
    request = json.loads(line)
    with open("native.requests", "a") as log:
        log.write(request["type"] + "\\n")
    kind, data, error = request["type"], {}, None
    if kind == "get_state":
        data = {"model": model, "thinkingLevel": level, "isStreaming": False, "messageCount": messages,
                "sessionFile": str(session_file), "steeringMode": "all", "followUpMode": "all"}
    elif kind == "get_available_models":
        data = {"models": AVAILABLE}
    elif kind == "get_commands":
        data = {"commands": []}
    elif kind == "get_messages":
        data = {"messages": []}
    elif kind == "aelix_control" and request["method"] == "capabilities":
        data = {"version": 1, "methods": ["permissions/get", "permissions/set"],
                "thinkingLevels": REASONING if model.get("reasoning") else ["off"]}
    elif kind == "aelix_control" and request["method"] == "permissions/set":
        mode = request["params"]["mode"]
        data = {"mode": mode}
    elif kind == "aelix_control" and request["method"] == "permissions/get":
        data = {"mode": mode}
    elif kind == "set_model":
        found = [m for m in AVAILABLE if (m["provider"], m["id"]) == (request["provider"], request["modelId"])]
        if found:
            model, level = dict(found[0]), "off"
            record({"type": "model_change", "provider": model["provider"], "modelId": model["id"]})
        else:
            error = "Model not found"
    elif kind == "set_thinking_level":
        level = request["level"]
        record({"type": "thinking_level_change", "level": level})
    elif kind == "prompt":
        if not model["provider"]:
            error = "No model selected."
        else:
            record({"type": "message", "role": "user"})
            record({"type": "message", "role": "assistant", "model": model["id"]})
            messages += 2
    # Native's key order, which ACP's history projection matches.
    response = {"type": "response", "command": kind, "success": error is None, "id": request["id"]}
    response.update({"error": error} if error else {"data": data})
    print(json.dumps(response), flush=True)
''' % {"placeholder": repr(PLACEHOLDER), "available": repr([{k: v for k, v in m.items() if k != "headers"} for m in AVAILABLE]),
       "reasoning": repr(REASONING)}


class NoCurrentModelWorkerTests(unittest.IsolatedAsyncioTestCase):
    """Load and worker restart with a real (fake native) worker process."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.native = self.root / "fake_native.py"
        self.native.write_text(FAKE_NATIVE, encoding="utf-8")
        self.cwd = str(self.root)
        self.server = None
        await self.open_server()

    async def asyncTearDown(self):
        await self.close_server()
        self.temp.cleanup()

    async def open_server(self):
        self.server = Server(SimpleNamespace(state_dir=str(self.root / "state"), profile="coding",
                                             aelix=sys.executable, aelix_arg=[str(self.native)],
                                             provider=None, model=None))

        async def send(_value):
            pass

        self.server.send = send
        await self.server.dispatch("initialize", {"protocolVersion": 1, "clientCapabilities": {}})

    async def close_server(self):
        if self.server is not None:
            for session in list(self.server.sessions.values()):
                await session.close()
            self.server.db.close()
            self.server = None

    async def reopen(self, session_id):
        """A new adapter process, as after a Studio restart."""
        await self.close_server()
        await self.open_server()
        return await self.server.dispatch("session/load", {"cwd": self.cwd, "sessionId": session_id, "mcpServers": []})

    def options(self, result):
        return {option["id"]: option["currentValue"] for option in result["configOptions"]}

    def native_requests(self):
        return (self.root / "native.requests").read_text().split()

    async def prompt(self, session_id, text="Hello"):
        return await self.server.dispatch("session/prompt", {"sessionId": session_id,
                                                              "prompt": [{"type": "text", "text": text}]})

    async def config(self, session_id, config_id, value):
        return await self.server.dispatch("session/set_config_option",
                                          {"sessionId": session_id, "configId": config_id, "value": value})

    async def new_session(self):
        created = await self.server.dispatch("session/new", {"cwd": self.cwd, "mcpServers": []})
        self.assertEqual(self.options(created)["model"], "")
        self.assertNotIn("thinking_level", self.options(created))
        return created["sessionId"]

    async def test_load_restores_a_choice_made_before_the_first_prompt(self):
        session_id = await self.new_session()
        await self.config(session_id, "model", "fixture/reasoner")
        await self.config(session_id, "thinking_level", "high")
        loaded = await self.reopen(session_id)
        # Native came back on its placeholder: its file has no messages yet.
        self.assertEqual((self.options(loaded)["model"], self.options(loaded)["thinking_level"]), ("fixture/reasoner", "high"))
        self.assertEqual(loaded["models"]["currentModelId"], "fixture/reasoner")
        self.assertNotIn("aelix/recoveryWarnings", loaded["_meta"])
        self.assertEqual(await self.prompt(session_id), {"stopReason": "end_turn"})

    async def test_worker_restart_restores_the_choice(self):
        session_id = await self.new_session()
        await self.config(session_id, "model", "fixture/plain/with/slash")
        session = self.server.sessions[session_id]
        old_worker = session.proc.pid
        await session.restart()
        self.assertNotEqual(session.proc.pid, old_worker)
        self.assertEqual(self.options(session.selectors())["model"], "fixture/plain/with/slash")
        self.assertEqual(await self.prompt(session_id), {"stopReason": "end_turn"})

    async def test_an_unselected_session_still_offers_and_requires_a_model_after_load(self):
        session_id = await self.new_session()
        loaded = await self.reopen(session_id)
        self.assertEqual(self.options(loaded)["model"], "")
        self.assertEqual(loaded["models"]["currentModelId"], "")
        self.assertNotIn("thinking_level", self.options(loaded))
        self.assertNotIn("aelix/recoveryWarnings", loaded["_meta"])
        self.assertNotIn("model", load_preferences(self.server.db, session_id)["config"])
        with self.assertRaises(ProtocolError) as refused:
            await self.prompt(session_id)
        self.assertEqual(refused.exception.data["kind"], "model_selection_required")
        self.assertNotIn("prompt", self.native_requests())

    async def test_native_history_without_a_restorable_model_takes_the_saved_choice(self):
        session_id = await self.new_session()
        await self.config(session_id, "model", "fixture/reasoner")
        self.assertEqual(await self.prompt(session_id), {"stopReason": "end_turn"})
        session_file = Path(self.server.sessions[session_id].state["sessionFile"])
        # Native's recorded model is gone: it resumes the history on its placeholder.
        session_file.write_text(session_file.read_text().replace('"provider": "fixture"', '"provider": "removed"', 1))
        loaded = await self.reopen(session_id)
        self.assertEqual(self.options(loaded)["model"], "fixture/reasoner")
        self.assertNotIn("aelix/recoveryWarnings", loaded["_meta"])

    async def test_native_history_model_still_wins_over_the_saved_choice(self):
        session_id = await self.new_session()
        await self.config(session_id, "model", "fixture/reasoner")
        self.assertEqual(await self.prompt(session_id), {"stopReason": "end_turn"})
        session_file = Path(self.server.sessions[session_id].state["sessionFile"])
        with session_file.open("a") as stream:  # Another native process switched the model.
            stream.write(json.dumps({"type": "model_change", "provider": "fixture", "modelId": "plain/with/slash"}) + "\n")
        loaded = await self.reopen(session_id)
        self.assertEqual(self.options(loaded)["model"], "fixture/plain/with/slash")


if __name__ == "__main__":
    unittest.main()
