"""Real Aelix + ACP + local model endpoint: delegated children over ACP (aelix-ai #612).

A scripted chat-completions server plays the parent (its requests offer the
`agent` tool) and each child (theirs do not; children run `--no-agents`). No
provider credentials or internet.
"""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from support import AcpClient
from aelix_acp.session import REABORT_SECONDS
from aelix_acp.wire.errors import TURN_FAILED_AFTER_DISPATCH

EXECUTABLE = os.environ.get("AELIX_ACP_TEST_AELIX")
PROFILE = "---\nname: worker\ndescription: Work\ntools: [read, write]\n---\nDo the task.\n"
NESTING = {"aelix/clientActions": True, "aelix/nestedToolCalls": True, "aelix/notices": True}
# Counts native aborts and keeps a cancelled turn streaming for a while after
# one, as a slow `turn_end` listener would (aelix-ai #800 K5).
SLOW_ABORT = '''import asyncio
import time
from pathlib import Path
async def aborted(event, ctx):
    with open(Path(ctx.cwd, "aborts.txt"), "a") as log:
        log.write(f"abort {time.monotonic()}\\n")
async def slow(event, ctx):
    if Path(ctx.cwd, "aborts.txt").exists():
        await asyncio.sleep(1.0)
def setup(api):
    api.on("abort", aborted)
    api.on("turn_end", slow)
'''


def text_of(message):
    content = message.get("content")
    if isinstance(content, str):
        return content
    return json.dumps(content)


@unittest.skipUnless(EXECUTABLE, "Set AELIX_ACP_TEST_AELIX to a runtime-control-v1 Aelix executable")
class NativeChildrenTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="acp-children-", dir="/tmp" if os.name != "nt" else None)
        self.root = Path(self.temp.name).resolve()
        for directory in ("home", "agent/agents"):
            (self.root / directory).mkdir(parents=True)
        (self.root / "agent/agents/worker.md").write_text(PROFILE, encoding="utf-8")
        self.parent_requests, self.child_requests = [], []
        self.child_gate, self.parent_gate = asyncio.Event(), asyncio.Event()
        self.child_waiting = asyncio.Event()
        self.calls = 0
        self.listener = await asyncio.start_server(self.model, "127.0.0.1", 0)
        port = self.listener.sockets[0].getsockname()[1]
        (self.root / "agent/models.json").write_text(json.dumps({"providers": {"fixture": {
            "baseUrl": f"http://127.0.0.1:{port}/v1", "api": "openai-completions", "apiKey": "synthetic-fixture-key",
            "models": [{"id": "one", "name": "Fixture One", "reasoning": False, "input": ["text"],
                        "contextWindow": 32768, "maxTokens": 2048}]}}}))
        (self.root / "agent/settings.json").write_text(json.dumps({"defaultProvider": "fixture", "defaultModel": "one"}))
        self.client = None
        self.clients = []

    async def asyncTearDown(self):
        self.child_gate.set()
        self.parent_gate.set()
        for client in self.clients:
            await client.close()
        self.listener.close()
        await self.listener.wait_closed()
        self.temp.cleanup()

    async def open(self, mode="yolo", capabilities=NESTING, extensions=()):
        self.client = await AcpClient.start(self.root, EXECUTABLE, extensions=extensions,
                                            capabilities={"_meta": dict(capabilities)},
                                            adapter_args=["--enable-agents", "--permission-mode", mode])
        self.clients.append(self.client)
        self.session = (await self.client.request("session/new", {"cwd": str(self.root), "mcpServers": []},
                                                  timeout=120))["sessionId"]

    # === the scripted model ===

    def call(self, name, arguments):
        self.calls += 1
        return {"role": "assistant", "tool_calls": [{"index": 0, "id": f"call_{name}_{self.calls}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)}}]}, "tool_calls"

    async def parent_turn(self, request):
        self.parent_requests.append(request)
        last = request["messages"][-1]
        text = text_of(last)
        if last.get("role") == "tool":
            return {"role": "assistant", "content": "PARENT_DONE"}, "stop"
        for marker, arguments in (
                ("SPAWN_HOLD", {"profile": "worker", "action": "spawn", "task": "CHILD_HOLD"}),
                ("SPAWN_WRITE", {"profile": "worker", "action": "spawn", "task": "CHILD_WRITE child.txt"}),
                ("DELEGATE_HOLD", {"profile": "worker", "task": "CHILD_HOLD"}),
                ("DELEGATE_WRITE", {"profile": "worker", "task": "CHILD_WRITE child.txt"})):
            if marker in text:
                return self.call("agent", arguments)
        if "PARENT_WRITE" in text:
            return self.call("write", {"path": str(self.root / "parent.txt"), "content": "parent"})
        if "PARENT_HOLD" in text:
            await self.parent_gate.wait()
        if "CHILD_DONE" in text:  # A job notice placed by a notice turn.
            return self.call("write", {"path": str(self.root / "notice.txt"), "content": "notice"})
        return {"role": "assistant", "content": "PARENT_OK"}, "stop"

    async def child_turn(self, request):
        self.child_requests.append(request)
        messages = request["messages"]
        task = next((text_of(m) for m in messages if m.get("role") == "user"), "")
        if messages[-1].get("role") == "tool":
            return {"role": "assistant", "content": "CHILD_DONE wrote"}, "stop"
        if "CHILD_WRITE" in task:
            return self.call("write", {"path": "child.txt", "content": "child"})
        if "CHILD_HOLD" in task and not any(m.get("role") == "assistant" for m in messages):
            self.child_waiting.set()
            await self.child_gate.wait()
            return {"role": "assistant", "content": "CHILD_DONE first"}, "stop"
        return {"role": "assistant", "content": "CHILD_DONE again"}, "stop"

    async def model(self, reader, writer):
        try:
            header = (await reader.readuntil(b"\r\n\r\n")).decode()
            length = int(next(line.split(":", 1)[1] for line in header.split("\r\n")
                              if line.lower().startswith("content-length:")))
            request = json.loads(await reader.readexactly(length))
            tools = {tool.get("function", {}).get("name") for tool in request.get("tools") or []}
            delta, stop = await (self.parent_turn(request) if "agent" in tools else self.child_turn(request))

            def event(delta, finish):
                return "data: " + json.dumps({"id": "fixture", "object": "chat.completion.chunk", "created": 0,
                    "model": "one", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                    **({"usage": {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12}} if finish else {})}) + "\n\n"
            body = (event(delta, None) + event({}, stop) + "data: [DONE]\n\n").encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\nContent-Length: "
                         + str(len(body)).encode() + b"\r\n\r\n" + body)
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    # === helpers ===

    async def prompt(self, text):
        return await self.client.request("session/prompt", {"sessionId": self.session,
                                                             "prompt": [{"type": "text", "text": text}]}, timeout=120)

    async def manage(self, action, arguments=None, timeout=60):
        return await self.client.request("_aelix/manage", {"sessionId": self.session, "action": action,
                                                           "arguments": arguments or {}}, timeout=timeout)

    async def error_of(self, awaitable):
        try:
            await awaitable
        except RuntimeError as failure:
            return json.loads(str(failure))
        self.fail("No error")

    async def held_child(self):
        await asyncio.wait_for(self.child_waiting.wait(), 60)
        rows = (await self.manage("children/list"))["children"]
        return next(row["id"] for row in rows if row["state"] in ("starting", "running"))

    def updates(self, client=None):
        return [event["params"]["update"] for event in (client or self.client).updates
                if event.get("method") == "session/update"]

    def card(self, call_id, client=None):
        return [update for update in self.updates(client) if update.get("toolCallId") == call_id]

    def agent_call(self):
        return next(update["toolCallId"] for update in self.updates()
                    if update.get("sessionUpdate") == "tool_call" and update.get("title") == "agent")

    async def until(self, predicate, timeout=60):
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(0.02)

    def child_ended(self, card):
        return any("end" in (u.get("_meta") or {}).get("aelix/child", {}) for u in self.card(card))

    def last_child(self, card):
        frames = [u for u in self.card(card) if "aelix/subagents" in (u.get("_meta") or {})]
        return frames[-1]["_meta"]["aelix/subagents"]["children"][-1]

    # === during a turn ===

    async def test_steer_and_wrap_up_reach_a_child_during_session_prompt(self):
        await self.open()
        prompt = asyncio.create_task(self.prompt("DELEGATE_HOLD"))
        child = await self.held_child()
        steer = await self.manage("children/steer", {"id": child, "text": "STEER_ONE"})
        self.assertEqual((steer["verb"], steer["outcome"]), ("steer", "queued"), steer)
        wrap = await self.manage("children/wrap_up", {"id": child})
        self.assertEqual((wrap["verb"], wrap["outcome"]), ("wrap_up", "queued"), wrap)
        self.child_gate.set()
        self.assertEqual(await prompt, {"stopReason": "end_turn"})
        self.assertIn("STEER_ONE", json.dumps(self.child_requests[-1]["messages"]))
        entry = self.last_child(self.agent_call())
        self.assertEqual((entry["id"], entry["wrappingUp"], entry["end"]), (child, True, "wrapped_up"))

    async def test_steer_wrap_up_and_stop_during_agents_run_on_a_top_level_card(self):
        await self.open()
        run = asyncio.create_task(self.manage("agents/run", {"name": "worker", "task": "CHILD_HOLD"}, timeout=120))
        child = await self.held_child()
        steer = await self.manage("children/steer", {"id": child, "text": "STEER_TWO"})
        self.assertEqual(steer["outcome"], "queued", steer)
        wrap = await self.manage("children/wrap_up", {"id": child})
        self.assertEqual((wrap["verb"], wrap["outcome"]), ("wrap_up", "queued"), wrap)
        stopped = await self.manage("children/stop", {"id": child})
        self.assertIn(stopped["state"], ("stopped", "running"), stopped)
        result = await run
        self.assertFalse(result["result"]["ok"], result)
        card = self.card(f"aelix-child:{child}")
        self.assertEqual((card[0]["sessionUpdate"], card[0]["title"], card[0]["kind"]),
                         ("tool_call", "agent worker", "other"))
        self.assertEqual((card[-1]["status"], card[-1]["_meta"]["aelix/child"]["end"]), ("failed", "stopped"))

    # === the fence ===

    async def test_a_restart_refuses_children_actions_before_anything_is_written(self):
        extension = self.root / "plain.py"
        extension.write_text("def setup(api):\n    pass\n", encoding="utf-8")
        await self.open()
        await self.manage("extensions/action", {"action": "enable", "target": str(extension)})
        restarts = (("workspace/trust", {"trusted": True}), ("extensions/action", {"action": "disable", "target": str(extension)}),
                    ("/trust on", None))
        for action, arguments in restarts:
            answers = []
            change = asyncio.create_task(self.prompt(action) if arguments is None else self.manage(action, arguments))
            while not change.done():
                try:
                    answers.append(await self.manage("children/steer", {"id": "sub-none", "text": "x"}))
                except RuntimeError as failure:
                    answers.append(json.loads(str(failure)))
                await asyncio.sleep(0.01)
            await change
            changing = [a for a in answers if a.get("message") == "the session is changing"]
            self.assertTrue(changing, (action, answers[-3:]))
            self.assertEqual({(a["code"], a["data"]["kind"]) for a in changing}, {(-32000, "session_changing")})

    @unittest.skipIf(sys.platform == "win32", "SIGSTOP holds the child's inbox acknowledgement")
    async def test_a_steer_in_flight_when_a_restart_begins_answers_delivery_unknown(self):
        await self.open()
        self.assertEqual(await self.prompt("SPAWN_HOLD"), {"stopReason": "end_turn"})
        child = await self.held_child()
        pids = [int(line.split(None, 1)[0]) for line in subprocess.run(
            ["ps", "-eo", "pid=,command="], capture_output=True, encoding="utf-8", errors="replace").stdout.splitlines()
            if "--command-inbox" in line and str(self.root) in line]
        self.assertEqual(len(pids), 1, pids)
        os.kill(pids[0], signal.SIGSTOP)
        try:
            steer = asyncio.create_task(self.manage("children/steer", {"id": child, "text": "LATE"}))
            await asyncio.sleep(0.5)
            self.assertFalse(steer.done())
            trust = asyncio.create_task(self.manage("workspace/trust", {"trusted": True}, timeout=120))
            error = await self.error_of(asyncio.wait_for(steer, 30))
            self.assertEqual((error["message"], error["code"], error["data"]["cause"]),
                             ("the session changed while this request ran; its delivery is unknown",
                              TURN_FAILED_AFTER_DISPATCH, {"kind": "session_changed", "command": "children/steer"}))
        finally:
            os.kill(pids[0], signal.SIGCONT)
        self.assertEqual(await trust, {"trusted": True})

    # === events ===

    async def test_a_spawned_childs_calls_nest_under_its_call_after_that_call_ended(self):
        await self.open()
        self.assertEqual(await self.prompt("SPAWN_WRITE"), {"stopReason": "end_turn"})
        spawn = self.agent_call()
        self.assertEqual(self.card(spawn)[-1].get("status"), "completed")
        await self.until(lambda: self.child_ended(spawn))
        nested = [u for u in self.updates() if (u.get("_meta") or {}).get("parentToolCallId") == spawn]
        self.assertEqual([(u["sessionUpdate"], u["status"]) for u in nested],
                         [("tool_call", "in_progress"), ("tool_call_update", "completed")])
        child = nested[0]["_meta"]["aelix/child"]["id"]
        self.assertTrue(nested[0]["toolCallId"].startswith(child + ":"))
        self.assertEqual(nested[0]["title"], "write")
        entry = self.last_child(spawn)
        self.assertEqual((entry["id"], entry["state"], entry["end"], entry["lastText"]),
                         (child, "done", "completed", "CHILD_DONE wrote"))
        self.assertEqual((self.root / "child.txt").read_text(), "child")

    async def test_a_spawned_childs_card_keeps_its_profile_and_state_after_the_next_prompt(self):
        """Fix round 1, V-F1: the frames after the next prompt fold onto the entry the card showed."""
        await self.open(capabilities={"aelix/clientActions": True})
        self.assertEqual(await self.prompt("SPAWN_HOLD"), {"stopReason": "end_turn"})
        await asyncio.wait_for(self.child_waiting.wait(), 60)
        spawn = self.agent_call()
        self.assertEqual(await self.prompt("hello"), {"stopReason": "end_turn"})
        mark = len(self.card(spawn))
        self.child_gate.set()
        await self.until(lambda: self.child_ended(spawn))
        after = [u["_meta"]["aelix/subagents"]["children"][-1] for u in self.card(spawn)[mark:]
                 if "aelix/subagents" in (u.get("_meta") or {})]
        self.assertGreaterEqual(len(after), 2, after)  # the child's text, then its end
        self.assertEqual({(entry["profile"], entry["state"] in ("running", "done")) for entry in after},
                         {("worker", True)}, after)
        self.assertEqual((after[-1]["state"], after[-1]["end"], after[-1]["lastText"]),
                         ("done", "completed", "CHILD_DONE first"))

    async def test_a_client_that_did_not_ask_gets_the_card_meta_only(self):
        await self.open(capabilities={"aelix/clientActions": True})
        self.assertEqual(await self.prompt("SPAWN_WRITE"), {"stopReason": "end_turn"})
        spawn = self.agent_call()
        await self.until(lambda: self.child_ended(spawn))
        await asyncio.sleep(0.5)  # A notice would follow the child's end.
        self.assertFalse([u for u in self.updates() if ":" in u.get("toolCallId", "") or
                          "parentToolCallId" in (u.get("_meta") or {})])
        self.assertEqual(self.last_child(spawn)["end"], "completed")
        self.assertFalse([e for e in self.client.updates if e.get("method") == "_aelix/notice"])

    # === notices ===

    async def test_children_notices_after_a_session_cancel_runs_its_tools(self):
        await self.open()
        self.assertEqual(await self.prompt("SPAWN_HOLD"), {"stopReason": "end_turn"})
        await asyncio.wait_for(self.child_waiting.wait(), 60)
        held = asyncio.create_task(self.prompt("PARENT_HOLD"))
        await self.until(lambda: "PARENT_HOLD" in json.dumps(self.parent_requests[-1]["messages"][-1]))
        await self.client.request("session/cancel", {"sessionId": self.session})
        self.assertEqual(await held, {"stopReason": "cancelled"})
        self.child_gate.set()
        await self.until(lambda: any(e.get("method") == "_aelix/notice" for e in self.client.updates))
        notice = next(e for e in self.client.updates if e.get("method") == "_aelix/notice")
        self.assertEqual(notice["params"], {"sessionId": self.session, "event": "queued", "data": {}})
        result = await self.manage("children/notices", timeout=120)
        self.assertEqual((result["started"], result["stopReason"], result["remaining"]), (True, "end_turn", 0), result)
        self.assertEqual((self.root / "notice.txt").read_text(), "notice")

    # === approvals ===

    async def test_a_childs_change_is_asked_under_its_nested_id_allow_once_then_reject_once(self):
        await self.open(mode="default")
        asked, answers = [], ["allow-once", "reject-once"]

        def decide(params):
            call = params["toolCall"]
            asked.append(params)
            return answers.pop(0) if ":" in call["toolCallId"] else "allow-once"
        self.client.permission = decide
        self.assertEqual(await self.prompt("DELEGATE_WRITE"), {"stopReason": "end_turn"})
        self.assertEqual((self.root / "child.txt").read_text(), "child")
        agent = self.agent_call()
        request = next(p for p in asked if ":" in p["toolCall"]["toolCallId"])
        call = request["toolCall"]
        child = call["_meta"]["aelix/child"]["id"]
        self.assertTrue(call["toolCallId"].startswith(child + ":"))
        self.assertEqual((call["_meta"]["parentToolCallId"], call["kind"], call["status"], call["rawInput"]["path"]),
                         (agent, "edit", "pending", "child.txt"))
        self.assertEqual([o["optionId"] for o in request["options"]], ["allow-once", "allow-always", "reject-once"])
        nested = [u for u in self.updates() if u.get("toolCallId") == call["toolCallId"]]
        self.assertEqual([u["status"] for u in nested], ["in_progress", "pending", "in_progress", "completed"])
        (self.root / "child.txt").unlink()
        self.assertEqual(await self.prompt("DELEGATE_WRITE"), {"stopReason": "end_turn"})
        self.assertFalse((self.root / "child.txt").exists())
        self.assertIn("Denied by the user", json.dumps(self.child_requests[-1]["messages"][-1]))

    async def test_allow_for_this_agent_reaches_native_while_history_is_locked(self):
        await self.open(mode="default")
        asked, allow = asyncio.Event(), asyncio.Event()

        async def decide(params):
            if ":" not in params["toolCall"]["toolCallId"]:
                return "allow-once"
            asked.set()
            await allow.wait()
            return "allow-always"

        self.client.permission = decide
        turn = asyncio.create_task(self.prompt("DELEGATE_WRITE"))
        await asyncio.wait_for(asked.wait(), 30)
        proof = self.root / "child.txt"
        self.assertFalse(proof.exists())
        with contextlib.closing(sqlite3.connect(self.root / "state/sessions.sqlite3", timeout=0)) as db:
            async with asyncio.timeout(8):
                while True:
                    try:
                        db.execute("BEGIN IMMEDIATE")
                        break
                    except sqlite3.OperationalError as error:
                        if error.sqlite_errorcode != sqlite3.SQLITE_BUSY:
                            raise
                        await asyncio.sleep(0.01)
            try:
                allow.set()
                await self.until(lambda: any(u.get("status") == "completed" and ":" in u.get("toolCallId", "")
                                             for u in self.updates()), timeout=8)
                self.assertEqual(proof.read_text(encoding="utf-8"), "child")
                await self.manage("children/list", timeout=8)
            finally:
                db.rollback()
        self.assertEqual(await turn, {"stopReason": "end_turn"})

    async def test_session_cancel_denies_an_open_child_request(self):
        await self.open(mode="default")
        opened, never = asyncio.Event(), asyncio.Event()

        async def decide(params):
            if ":" not in params["toolCall"]["toolCallId"]:
                return "allow-once"
            opened.set()
            await never.wait()
            return "allow-once"
        self.client.permission = decide
        self.assertEqual(await self.prompt("SPAWN_WRITE"), {"stopReason": "end_turn"})
        await asyncio.wait_for(opened.wait(), 60)
        await self.client.request("session/cancel", {"sessionId": self.session})
        await self.until(lambda: self.child_ended(self.agent_call()))
        self.assertFalse((self.root / "child.txt").exists())
        self.assertIn("the host cancelled the request", json.dumps(self.child_requests[-1]["messages"][-1]))
        never.set()

    async def held_question(self):
        """A spawned child's change, asked and held open by the client; its request."""
        await self.open(mode="default")
        opened, self.release = asyncio.Event(), asyncio.Event()

        async def decide(params):
            if ":" not in params["toolCall"]["toolCallId"]:
                return "allow-once"
            opened.set()
            await self.release.wait()
            return "allow-once"
        self.client.permission = decide
        self.assertEqual(await self.prompt("SPAWN_WRITE"), {"stopReason": "end_turn"})
        await asyncio.wait_for(opened.wait(), 60)
        return next(m for m in self.client.requests if m["method"] == "session/request_permission"
                    and ":" in m["params"]["toolCall"]["toolCallId"])

    async def test_a_restart_with_a_child_asking_closes_its_cards(self):
        """Fix round 1, P-LOW-2 / Codex C-M3: the nested card and the child's entry end when its worker is replaced."""
        request = await self.held_question()
        nested, spawn = request["params"]["toolCall"]["toolCallId"], self.agent_call()
        self.assertEqual(await self.manage("workspace/trust", {"trusted": True}, timeout=120), {"trusted": True})
        self.assertEqual(self.card(nested)[-1]["status"], "failed", self.card(nested))
        self.assertEqual([e["params"] for e in self.client.updates if e.get("method") == "$/cancel_request"],
                         [{"requestId": request["id"]}])
        self.assertTrue(self.child_ended(spawn), self.card(spawn)[-1])
        entry = self.last_child(spawn)
        self.assertEqual((entry["profile"], entry["state"] in ("stopped", "done", "error"), entry["end"] is not None),
                         ("worker", True, True), entry)
        self.release.set()
        await asyncio.sleep(0.5)
        self.assertFalse((self.root / "child.txt").exists())
        self.assertEqual(await self.prompt("hello"), {"stopReason": "end_turn"})  # the new worker runs

    async def check_taken_back(self, act):
        """Fix round 1, P-LOW-1: the client's open question is taken back when native drops it, and an Allow after it writes nothing."""
        request = await self.held_question()
        spawn = self.agent_call()
        await act(request["params"]["toolCall"]["_meta"]["aelix/child"]["id"])
        await self.until(lambda: any(e.get("method") == "$/cancel_request" for e in self.client.updates), 60)
        self.assertEqual([e["params"] for e in self.client.updates if e.get("method") == "$/cancel_request"],
                         [{"requestId": request["id"]}])
        self.release.set()  # the human clicks Allow on a dialog the client kept open
        await self.until(lambda: self.child_ended(spawn), 90)
        await asyncio.sleep(0.5)
        self.assertFalse((self.root / "child.txt").exists())

    async def test_children_stop_takes_back_an_open_question(self):
        await self.check_taken_back(lambda child: self.manage("children/stop", {"id": child}))

    async def test_a_wrap_up_takes_back_an_open_question(self):
        await self.check_taken_back(lambda child: self.manage("children/wrap_up", {"id": child}))

    async def test_a_reload_takes_back_an_open_question(self):
        await self.check_taken_back(lambda child: self.manage("resources/reload", timeout=120))

    async def test_a_parents_and_a_childs_request_open_together_are_both_answered(self):
        await self.open(mode="default")
        open_requests, release = [], asyncio.Event()

        async def decide(params):
            call_id = params["toolCall"]["toolCallId"]
            if params["toolCall"].get("title") == "agent":
                return "allow-once"
            open_requests.append(call_id)
            await release.wait()
            return "allow-once"
        self.client.permission = decide
        self.assertEqual(await self.prompt("SPAWN_WRITE"), {"stopReason": "end_turn"})
        await self.until(lambda: len(open_requests) == 1)
        parent = asyncio.create_task(self.prompt("PARENT_WRITE"))
        await self.until(lambda: len(open_requests) == 2)
        self.assertEqual(sum(":" in call_id for call_id in open_requests), 1)
        release.set()
        self.assertEqual(await parent, {"stopReason": "end_turn"})
        await self.until(lambda: (self.root / "child.txt").exists())
        self.assertEqual((self.root / "parent.txt").read_text(), "parent")

    # === #800 on ACP ===

    async def test_a_cancelled_calls_report_is_replayed_by_session_load(self):
        """A guard: the recorded terminal update of the cancelled `agent` call replays (passes on a78db59 too)."""
        await self.open()
        prompt = asyncio.create_task(self.prompt("DELEGATE_HOLD"))
        await asyncio.wait_for(self.child_waiting.wait(), 60)
        await self.client.request("session/cancel", {"sessionId": self.session})
        self.assertEqual(await prompt, {"stopReason": "cancelled"})
        agent = self.agent_call()
        final = self.card(agent)[-1]
        self.assertEqual(final["status"], "failed")
        report = final["content"][0]["content"]["text"]
        self.assertTrue(report)
        await self.client.close()
        self.clients.remove(self.client)
        self.client = await AcpClient.start(self.root, EXECUTABLE, adapter_args=["--enable-agents"])
        self.clients.append(self.client)
        await self.client.request("session/load", {"cwd": str(self.root), "sessionId": self.session, "mcpServers": []},
                                  timeout=120)
        replayed = [u for u in self.card(agent) if u.get("status") == "failed"]
        self.assertEqual(replayed[-1]["content"][0]["content"]["text"], report)

    async def test_one_session_cancel_sends_one_native_abort_in_the_first_two_seconds(self):
        extension = self.root / "slow_abort.py"
        extension.write_text(SLOW_ABORT, encoding="utf-8")
        await self.open(extensions=[extension])
        prompt = asyncio.create_task(self.prompt("PARENT_HOLD"))
        await self.until(lambda: self.parent_requests)
        await self.client.request("session/cancel", {"sessionId": self.session})
        self.assertEqual(await asyncio.wait_for(prompt, 30), {"stopReason": "cancelled"})
        # Native's receipt times: one abort in the re-abort window after the first (less 0.5 s of
        # jitter). A runner slow enough to settle after 2 s gets the adapter's re-abort, which is due.
        times = [float(line.split()[1]) for line in (self.root / "aborts.txt").read_text().splitlines()]
        self.assertEqual([at - times[0] for at in times if at - times[0] < REABORT_SECONDS - 0.5], [0.0], times)


if __name__ == "__main__":
    unittest.main()
