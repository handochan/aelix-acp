"""Delegated children over ACP (aelix-ai #612) without a native: routing, approvals, fences, cadence."""
import asyncio
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from aelix_acp import session as session_module
from aelix_acp.server import ProtocolError, Server, Session
from aelix_acp.runtime.children import MAX_CHILDREN, ChildRoutes, nested_only, wire_call_id
from aelix_acp.runtime.leases import acquire
from aelix_acp.runtime.store import history
from aelix_acp.runtime.subagents import MAX_CHILDREN as MAX_SUBAGENTS, META_KEY
from aelix_acp.wire.errors import TURN_FAILED_AFTER_DISPATCH
from aelix_acp.wire.process import terminate_owned

EPOCH = "e1"


def frame(event, child="sub-1", seq=1, epoch=EPOCH, **data):
    return {"type": "aelix_child", "event": event, "data": {"id": child, "seq": seq, "epoch": epoch, **data}}


def state(child="sub-1", seq=1, epoch=EPOCH, parent="call-1", value="running", **extra):
    return frame("state", child, seq, epoch, state=value, profile="reviewer", sessionId=None, jobId=None,
                 parentToolCallId=parent, background=False, wrappingUp=False, **extra)


def spawned(seq=1, value="running", **extra):
    """A background child's state frame: a job, its parent call ended."""
    update = state(seq=seq, value=value, **extra)
    update["data"].update(jobId="job-1", background=True)
    return update


def approval_request(child="sub-1", call="call-a", epoch=EPOCH, parent="call-1", cwd=None):
    return {"type": "extension_ui_request", "id": "ui-1", "method": "childApproval",
            "title": "reviewer agent sub-1 asks: Create/overwrite out.txt?",
            "child": {"id": child, "profile": "reviewer", "sessionId": None, "jobId": None,
                      "parentToolCallId": parent, "background": False, "cwd": cwd, "epoch": epoch},
            "tool": {"callId": call, "name": "write", "kind": "write", "args": {"path": "out.txt", "content": "x"}},
            "choices": [{"id": "yes", "label": "Yes", "kind": "allow_once"},
                        {"id": "yes_session", "label": "Yes, for this agent", "kind": "allow_always"},
                        {"id": "no", "label": "No", "kind": "reject_once"}]}


class Stdin:
    def __init__(self):
        self.lines = []

    def write(self, data):
        self.lines.extend(json.loads(line) for line in data.decode().splitlines())

    async def drain(self):
        pass


class RoutesTests(unittest.TestCase):
    def test_a_child_keeps_its_parent_until_its_terminal_frame(self):
        routes = ChildRoutes()
        self.assertEqual(routes.accept("state", state()["data"]), ((EPOCH, "sub-1"), "call-1"))
        self.assertEqual(routes.accept("tool_start", frame("tool_start", seq=2, callId="c", name="read", args="")["data"]),
                         ((EPOCH, "sub-1"), "call-1"))
        self.assertEqual(routes.accept("state", state(seq=3, value="done", end="completed")["data"]),
                         ((EPOCH, "sub-1"), "call-1"))
        routes.ended((EPOCH, "sub-1"))
        self.assertNotIn((EPOCH, "sub-1"), routes.parents)
        self.assertIsNone(routes.accept("text", frame("text", seq=4, text="late")["data"]))
        # The repeated terminal frame is still a duplicate after the entry went.
        self.assertIsNone(routes.accept("state", state(seq=3, value="done", end="completed")["data"]))

    def test_frames_are_deduplicated_by_epoch_id_and_seq(self):
        routes = ChildRoutes()
        routes.accept("state", state()["data"])
        self.assertIsNotNone(routes.accept("text", frame("text", seq=2, text="a")["data"]))
        self.assertIsNone(routes.accept("text", frame("text", seq=2, text="a")["data"]))
        self.assertIsNone(routes.accept("text", frame("text", seq=1, text="a")["data"]))
        self.assertIsNotNone(routes.accept("text", frame("text", seq=3, text="b")["data"]))

    def test_a_newer_epoch_drops_the_older_one_and_its_late_frames(self):
        routes = ChildRoutes()
        routes.accept("state", state()["data"])
        routes.remember((EPOCH, "sub-1"), {"profile": "reviewer"})
        routes.accept("state", state(child="sub-2", epoch="e2", parent="call-9")["data"])
        self.assertEqual(list(routes.parents), [("e2", "sub-2")])
        self.assertEqual(routes.known, {})  # an older epoch's card fields go with its routes
        self.assertIsNone(routes.accept("text", frame("text", seq=2, text="late")["data"]))
        self.assertTrue(routes.stale(EPOCH))
        self.assertFalse(routes.stale("e2"))

    def test_routes_are_bounded_and_unknown_children_are_not_routed(self):
        routes = ChildRoutes()
        for index in range(MAX_CHILDREN + 3):
            routes.accept("state", state(child=f"sub-{index}")["data"])
        self.assertEqual(len(routes.parents), MAX_CHILDREN)
        self.assertNotIn((EPOCH, "sub-0"), routes.parents)
        self.assertIsNone(routes.accept("text", frame("text", child="never-seen", seq=2, text="x")["data"]))
        for bad in ({"id": "x", "seq": 0, "epoch": EPOCH}, {"id": "x", "seq": True, "epoch": EPOCH},
                    {"id": "", "seq": 1, "epoch": EPOCH}, {"id": "x", "seq": 1}, None):
            self.assertIsNone(routes.accept("state", bad))
        self.assertIsNone(routes.accept("unknown", state()["data"]))

    def test_an_unknown_event_of_a_routed_child_is_dropped_without_its_seq(self):
        """Guard (passes on c09fc27): pins V-F4's M1, the event filter, for a child whose route exists."""
        routes = ChildRoutes()
        routes.accept("state", state()["data"])
        self.assertIsNone(routes.accept("unknown", frame("unknown", seq=2)["data"]))
        self.assertEqual(routes.accept("text", frame("text", seq=2, text="t")["data"]), ((EPOCH, "sub-1"), "call-1"))

    def test_a_parent_id_that_is_not_a_non_empty_string_is_no_parent(self):
        """Guard (passes on c09fc27): pins V-F4's M8."""
        routes = ChildRoutes()
        for index, parent in enumerate((5, "", ["call-1"], None)):
            self.assertEqual(routes.accept("state", state(child=f"sub-{index}", parent=parent)["data"]),
                             ((EPOCH, f"sub-{index}"), None), parent)

    def test_an_evicted_child_takes_its_card_fields_and_open_calls_with_it(self):
        """Guard (passes on 91c4ed1); fix round 2, F3: kills the verifier's D17 (eviction kept `known` and `calls`)."""
        routes = ChildRoutes()
        oldest = (EPOCH, "sub-0")
        routes.accept("state", state(child="sub-0")["data"])
        routes.remember(oldest, {"profile": "reviewer"})
        routes.tool(oldest, "c1", "read")
        for index in range(1, MAX_CHILDREN + 1):
            routes.accept("state", state(child=f"sub-{index}")["data"])
        self.assertEqual((oldest in routes.parents, oldest in routes.known, oldest in routes.calls), (False, False, False))
        self.assertEqual(len(routes.live()), MAX_CHILDREN)

    def test_a_childs_open_calls_are_bounded_oldest_first(self):
        """Guard (passes on 91c4ed1); fix round 2, F3: kills D21 (the per-child bound on open calls)."""
        routes = ChildRoutes()
        routes.accept("state", state()["data"])
        key = (EPOCH, "sub-1")
        for index in range(MAX_CHILDREN + 2):
            self.assertEqual(routes.tool(key, f"c{index}", f"tool-{index}"), f"tool-{index}")
        self.assertEqual(list(routes.calls[key]), [f"c{index}" for index in range(2, MAX_CHILDREN + 2)])

    def test_an_ended_child_leaves_no_open_calls(self):
        """Guard (passes on 91c4ed1); fix round 2, F3: kills D24 (ended() kept `calls`)."""
        routes = ChildRoutes()
        routes.accept("state", state()["data"])
        key = (EPOCH, "sub-1")
        routes.tool(key, "c1", "read")
        routes.ended(key)
        self.assertEqual((routes.calls, routes.known, routes.live()), ({}, {}, []))

    def test_the_parent_id_aliases_are_bounded_oldest_first(self):
        """Guard (passes on 91c4ed1); fix round 2, F3: kills D15 (the aliases bound)."""
        routes = ChildRoutes()
        raws = [f"{index:03d}" + "x" * 200 for index in range(MAX_CHILDREN + 2)]
        for raw in raws:
            routes.call_started(raw)
        self.assertEqual(len(routes.aliases), MAX_CHILDREN)
        self.assertEqual(routes.card_of(wire_call_id(raws[0])), wire_call_id(raws[0]))  # dropped: no card
        self.assertEqual(routes.card_of(wire_call_id(raws[-1])), raws[-1])


class WireCallIdTests(unittest.TestCase):
    """Fix round 1, Codex C-L6: the adapter projects a call id as native does."""

    CASES = ["call_1", "x" * 128, "x" * 129, "\u00e9" * 64, "\u00e9" * 65, "\x01" * 21, "\x01" * 22, '"' * 64,
             '"' * 65, "h:abc", "\ud800", "\U0001f600" * 32, "\U0001f600" * 33]

    def test_known_values(self):
        self.assertEqual(wire_call_id("x" * 129), "h:0ec9eb33e74510bcdd1f2ea55206e82f")
        self.assertEqual(wire_call_id("x" * 128), "x" * 128)
        self.assertEqual(wire_call_id("\x01" * 21), "\x01" * 21)  # 126 bytes escaped
        self.assertTrue(wire_call_id("\x01" * 22).startswith("h:"))  # 132 bytes escaped
        self.assertTrue(wire_call_id("h:abc").startswith("h:") and wire_call_id("h:abc") != "h:abc")
        self.assertEqual(len(wire_call_id("\ud800")), 34)

    def test_the_same_as_natives(self):
        try:
            from aelix_agents.child_events import wire_call_id as native
        except ImportError:
            self.skipTest("the native runtime is not installed")
        for raw in self.CASES:
            self.assertEqual(wire_call_id(raw), native(raw), repr(raw))


class Base(unittest.IsolatedAsyncioTestCase):
    capabilities = {}

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = Server(SimpleNamespace(state_dir=self.tmp.name, profile="coding", turn_timeout=600))
        self.server.client_capabilities = {"_meta": dict(self.capabilities)}
        self.events = []

        async def send(event):
            self.events.append(event)
        self.server.send = send
        self.session = Session(self.server, "session", self.tmp.name)
        self.stdin = Stdin()
        self.session.proc = SimpleNamespace(stdin=self.stdin, returncode=None)

    async def asyncTearDown(self):
        await self.session.live_history.flush()
        self.server.db.close()
        self.tmp.cleanup()

    def updates(self):
        return [e["params"]["update"] for e in self.events if e.get("method") == "session/update"]

    async def feed(self, *events):
        for event in events:
            await self.session.event(event)


class StandardClientTests(Base):
    async def test_the_parent_card_carries_the_childs_text_stuck_wrap_up_and_end(self):
        await self.feed({"type": "tool_execution_start", "toolCallId": "call-1", "toolName": "agent", "args": {}},
                        state(), frame("tool_start", seq=2, callId="c1", name="read", args="path=a"),
                        frame("text", seq=3, text="half way\x1b[2J"), frame("stuck", seq=4, stuck="repeat"),
                        frame("wrap_up", seq=5, requested=True),
                        {**state(seq=6, value="done", end="wrapped_up", outcome="aborted", reason="r", note=None),
                         "data": {**state(seq=6, value="done", end="wrapped_up", outcome="aborted", reason="r",
                                          note=None)["data"], "wrappingUp": True}})
        cards = [u for u in self.updates() if u.get("toolCallId") == "call-1" and "_meta" in u]
        child = cards[-1]["_meta"][META_KEY]["children"][0]
        self.assertEqual((child["id"], child["profile"], child["state"]), ("sub-1", "reviewer", "done"))
        self.assertEqual((child["lastText"], child["stuck"], child["wrappingUp"], child["end"]),
                         ("half way [2J", "repeat", True, "wrapped_up"))
        self.assertEqual(cards[-1]["_meta"]["aelix/child"], {"id": "sub-1", "epoch": EPOCH, "seq": 6,
                                                             "event": "state", "end": "wrapped_up"})
        self.assertTrue(all(u["status"] == "in_progress" for u in cards))
        self.assertEqual(self.session.children.parents, {})  # The route went with the child's end.
        # No nested tool calls for a client that did not ask for them.
        self.assertFalse([u for u in self.updates() if ":" in str(u.get("toolCallId"))])

    async def test_a_spawned_childs_frames_after_the_call_ended_and_the_next_prompt_still_reach_its_card(self):
        await self.feed({"type": "tool_execution_start", "toolCallId": "call-1", "toolName": "agent", "args": {}},
                        state(), {"type": "tool_execution_end", "toolCallId": "call-1", "toolName": "agent",
                                  "result": {"content": []}})
        self.session.subagents.clear()  # The next prompt's per-turn reset.
        self.session.tools.clear()
        await self.feed(frame("text", seq=2, text="still going"))
        last = self.updates()[-1]
        self.assertEqual((last["toolCallId"], last.get("status")), ("call-1", None))
        self.assertEqual(last["_meta"][META_KEY]["children"][0]["lastText"], "still going")

    async def test_a_spawned_childs_card_keeps_every_field_after_the_next_prompt(self):
        """Fix round 1, V-F1 / Codex C-M5: a frame after the next prompt folds onto what the card showed."""
        await self.feed({"type": "tool_execution_start", "toolCallId": "call-1", "toolName": "agent", "args": {}},
                        spawned(), frame("wrap_up", seq=2, requested=True),
                        frame("stuck", seq=3, stuck="repeat"), frame("text", seq=4, text="first"),
                        {"type": "tool_execution_end", "toolCallId": "call-1", "toolName": "agent",
                         "result": {"content": []}})
        fields = ("id", "profile", "state", "wrappingUp", "stuck", "lastText", "end")
        before = self.updates()[-1]["_meta"][META_KEY]["children"][0]
        self.assertEqual(tuple(before.get(name) for name in fields),
                         ("sub-1", "reviewer", "running", True, "repeat", "first", None))

        async def rpc(command, **kwargs):
            return {"isStreaming": False} if command == "get_state" else {}
        self.session.rpc = rpc
        # The real next prompt: drive_turn clears the per-turn tracker.
        self.assertEqual(await self.session._prompt([{"type": "text", "text": "next"}]), {"stopReason": "end_turn"})
        await self.feed(frame("text", seq=5, text="still going"))
        last = self.updates()[-1]
        self.assertEqual((last["toolCallId"], last.get("status")), ("call-1", None))
        entry = last["_meta"][META_KEY]["children"][0]
        self.assertEqual(tuple(entry.get(name) for name in fields),
                         ("sub-1", "reviewer", "running", True, "repeat", "still going", None))
        await self.feed(spawned(seq=6, value="done", end="completed", outcome="ok", reason=None, note=None))
        entry = self.updates()[-1]["_meta"][META_KEY]["children"][0]
        self.assertEqual(tuple(entry.get(name) for name in fields),
                         ("sub-1", "reviewer", "done", False, "repeat", "still going", "completed"))
        self.assertEqual(self.session.children.known, {})  # Gone with the child's route.

    async def test_a_background_childs_tool_shows_on_its_card_entry(self):
        """Fix round 1, Codex C-L7: a client without nesting saw nothing of a spawned child's tools."""
        await self.feed({"type": "tool_execution_start", "toolCallId": "call-1", "toolName": "agent", "args": {}},
                        spawned(), {"type": "tool_execution_end", "toolCallId": "call-1", "toolName": "agent",
                                    "result": {"content": []}})
        seen = []
        for event in (frame("tool_start", seq=2, callId="c1", name="read", args="a"),
                      frame("tool_start", seq=3, callId="c2", name="bash", args="ls"),
                      frame("tool_end", seq=4, callId="c2", status="ok"),
                      frame("tool_end", seq=5, callId="c1", status="ok")):
            count = len(self.updates())
            await self.feed(event)
            self.assertEqual(len(self.updates()), count + 1, event)
            last = self.updates()[-1]
            seen.append((last["toolCallId"], last["_meta"][META_KEY]["children"][0]["currentTool"]))
        self.assertEqual(seen, [("call-1", "read"), ("call-1", "bash"), ("call-1", "read"), ("call-1", None)])
        self.assertEqual(self.updates()[-1]["_meta"][META_KEY]["children"][0]["state"], "running")
        # A terminal frame with a call still open (its tool_end lost) shows no running tool.
        await self.feed(frame("tool_start", seq=6, callId="c3", name="grep", args="x"),
                        spawned(seq=7, value="stopped", end="stopped", outcome="aborted", reason=None, note=None))
        entry = self.updates()[-1]["_meta"][META_KEY]["children"][0]
        self.assertEqual((entry["currentTool"], entry["end"]), (None, "stopped"))

    async def test_an_unchanged_frame_sends_nothing_and_a_subagent_snapshot_keeps_the_extras(self):
        await self.feed({"type": "tool_execution_start", "toolCallId": "call-1", "toolName": "agent", "args": {}},
                        state(), frame("text", seq=2, text="t"))
        count = len(self.updates())
        # A state frame for a receipt's sessionId changes nothing the card shows.
        await self.feed(state(seq=3))
        self.assertEqual(len(self.updates()), count)
        await self.feed({"type": "aelix_subagent", "event": "subagent_tool",
                         "data": {"id": "sub-1", "profile": "reviewer", "state": "running", "current_tool": "read"}})
        child = self.updates()[-1]["_meta"][META_KEY]["children"][0]
        self.assertEqual((child["currentTool"], child["lastText"]), ("read", "t"))

    async def test_a_late_subagent_event_of_a_child_known_from_aelix_child_stays_on_its_card(self):
        await self.feed({"type": "tool_execution_start", "toolCallId": "call-1", "toolName": "agent", "args": {}},
                        state(), {"type": "tool_execution_end", "toolCallId": "call-1", "toolName": "agent",
                                  "result": {"content": []}})
        self.session.subagents.children["call-1"].clear()  # Only aelix_child named this child.
        self.session.subagents.parents.clear()
        await self.feed(frame("text", seq=2, text="t"),
                        {"type": "aelix_subagent", "event": "subagent_end",
                         "data": {"id": "sub-1", "profile": "reviewer", "state": "done"}})
        self.assertFalse([e for e in self.events if e.get("method") == "_aelix/subagent"])
        self.assertEqual(self.updates()[-1]["_meta"][META_KEY]["children"][0]["state"], "done")

    async def test_an_unparented_child_keeps_the_extension_notification_only(self):
        await self.feed(state(parent=None), frame("tool_start", seq=2, callId="c1", name="read", args=""),
                        state(seq=3, parent=None, value="done", end="completed"))
        self.assertEqual(self.updates(), [])
        self.assertEqual(self.session.children.parents, {})

    async def test_a_notice_is_not_forwarded_to_a_client_that_did_not_ask(self):
        """Guard: a78db59 drops every aelix_notice."""
        await self.feed({"type": "aelix_notice", "event": "queued", "data": {}})
        self.assertEqual(self.events, [])


class BoundsTests(Base):
    async def test_a_card_shows_at_most_32_children(self):
        """Guard (passes on c09fc27): pins V-F3's M15, the bound in SubagentTracker.merge."""
        await self.feed({"type": "tool_execution_start", "toolCallId": "call-1", "toolName": "agent", "args": {}},
                        *(state(child=f"sub-{index}") for index in range(MAX_SUBAGENTS + 1)))
        children = self.updates()[-1]["_meta"][META_KEY]["children"]
        self.assertEqual([child["id"] for child in children], [f"sub-{index}" for index in range(MAX_SUBAGENTS)])
        self.assertFalse([u for u in self.updates() if u.get("_meta", {}).get("aelix/child", {}).get("id")
                          == f"sub-{MAX_SUBAGENTS}"])


class TruthyCapabilityTests(Base):
    """Guard (passes on c09fc27): pins V-F4's M29, only the JSON value true declares a capability."""

    capabilities = {"aelix/nestedToolCalls": 1, "aelix/notices": "true"}

    async def test_a_truthy_value_that_is_not_true_declares_nothing(self):
        await self.feed(state(parent=None), frame("tool_start", seq=2, callId="c1", name="read", args=""),
                        {"type": "aelix_notice", "event": "queued", "data": {}})
        self.assertEqual(self.events, [])


class NestingClientTests(Base):
    capabilities = {"aelix/nestedToolCalls": True, "aelix/notices": True}

    async def test_a_childs_calls_nest_under_its_parent_card_with_stable_ids(self):
        await self.feed({"type": "tool_execution_start", "toolCallId": "call-1", "toolName": "agent", "args": {}},
                        state(), frame("tool_start", seq=2, callId="c1", name="write", args="path=out.txt"),
                        frame("approval", seq=3, callId="c1", toolName="write", step="asked"),
                        frame("approval", seq=4, callId="c1", toolName="write", step="allowed"),
                        frame("tool_end", seq=5, callId="c1", status="ok"),
                        frame("tool_start", seq=6, callId="c2", name="bash", args="ls"),
                        frame("tool_end", seq=7, callId="c2", status="no_result"))
        nested = [u for u in self.updates() if u.get("toolCallId", "").startswith("sub-1:")]
        self.assertEqual([(u["sessionUpdate"], u["toolCallId"], u["status"]) for u in nested], [
            ("tool_call", "sub-1:c1", "in_progress"), ("tool_call_update", "sub-1:c1", "pending"),
            ("tool_call_update", "sub-1:c1", "in_progress"), ("tool_call_update", "sub-1:c1", "completed"),
            ("tool_call", "sub-1:c2", "in_progress"), ("tool_call_update", "sub-1:c2", "failed")])
        self.assertTrue(all(u["_meta"]["parentToolCallId"] == "call-1" for u in nested))
        self.assertEqual((nested[0]["title"], nested[0]["kind"]), ("write", "edit"))
        self.assertEqual(nested[0]["content"][0]["content"]["text"], "path=out.txt")
        self.assertEqual([u["_meta"]["aelix/child"]["seq"] for u in nested], [2, 3, 4, 5, 6, 7])

    async def test_a_parent_call_id_native_hashes_still_finds_its_card(self):
        """Fix round 1, Codex C-L6: native names a parent id over 128 wire bytes h: + 32 hex of its sha256."""
        raw = "x" * 129
        named = "h:0ec9eb33e74510bcdd1f2ea55206e82f"  # aelix_agents.child_events.wire_call_id(raw)
        await self.feed({"type": "tool_execution_start", "toolCallId": raw, "toolName": "agent", "args": {}},
                        state(parent=named), frame("tool_start", seq=2, callId="c1", name="read", args=""))
        self.assertEqual({u["toolCallId"] for u in self.updates() if "aelix/subagents" in u.get("_meta", {})}, {raw})
        nested = [u for u in self.updates() if u["toolCallId"] == "sub-1:c1"]
        self.assertEqual(nested[0]["_meta"]["parentToolCallId"], raw)
        asked = []

        async def client_request(method, params, **options):
            asked.append(params)
            return {"outcome": {"outcome": "cancelled"}}
        self.server.client_request = client_request
        await self.session.handle_ui(approval_request(parent=named))
        self.assertEqual(asked[0]["toolCall"]["_meta"]["parentToolCallId"], raw)

    async def test_an_unparented_child_gets_a_top_level_card_closed_by_its_end(self):
        await self.feed(state(parent=None), frame("tool_start", seq=2, callId="c1", name="read", args=""),
                        frame("tool_end", seq=3, callId="c1", status="failed"),
                        state(seq=4, parent=None, value="stopped", end="stopped"))
        updates = self.updates()
        self.assertEqual((updates[0]["sessionUpdate"], updates[0]["toolCallId"], updates[0]["title"],
                          updates[0]["kind"]), ("tool_call", "aelix-child:sub-1", "agent reviewer", "other"))
        nested = [u for u in updates if u["toolCallId"] == "sub-1:c1"]
        self.assertEqual({u["_meta"]["parentToolCallId"] for u in nested}, {"aelix-child:sub-1"})
        last = updates[-1]
        self.assertEqual((last["toolCallId"], last["status"]), ("aelix-child:sub-1", "failed"))
        # Opened, its state, its tool started and ended (currentTool, fix round 1 C-L7), closed.
        self.assertEqual([u["sessionUpdate"] for u in updates if u["toolCallId"] == "aelix-child:sub-1"],
                         ["tool_call"] + ["tool_call_update"] * 4)
        self.assertEqual(last["_meta"][META_KEY]["children"][0]["end"], "stopped")
        self.assertEqual(self.session.child_calls, set())

    async def test_a_parented_childs_route_goes_with_its_end(self):
        await self.feed({"type": "tool_execution_start", "toolCallId": "call-1", "toolName": "agent", "args": {}},
                        state(), state(seq=2, value="done", end="completed"))
        self.assertEqual(self.session.children.parents, {})

    async def test_a_completed_unparented_child_closes_its_card_completed(self):
        await self.feed(state(parent=None), state(seq=2, parent=None, value="done", end="completed"))
        self.assertEqual(self.updates()[-1]["status"], "completed")

    async def test_a_notices_data_that_is_not_an_object_is_sent_empty(self):
        """Guard (passes on c09fc27): pins V-F4's M28."""
        for data in ("x", None, [1]):
            self.events.clear()
            await self.feed({"type": "aelix_notice", "event": "queued", "data": data})
            self.assertEqual(self.events[0]["params"]["data"], {}, data)

    async def test_a_notice_reaches_a_client_that_asked_and_is_not_recorded(self):
        await self.feed({"type": "aelix_notice", "event": "queued", "data": {}})
        self.assertEqual(self.events, [{"jsonrpc": "2.0", "method": "_aelix/notice",
                                        "params": {"sessionId": "session", "event": "queued", "data": {}}}])
        self.assertEqual(self.server.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)


class ApprovalTests(Base):
    capabilities = {"aelix/nestedToolCalls": True}

    async def ask(self, reply, request=None):
        asked = []

        async def client_request(method, params, **options):
            asked.append((method, params))
            if isinstance(reply, BaseException):
                raise reply
            return reply
        self.server.client_request = client_request
        await self.session.handle_ui(request or approval_request(cwd=self.tmp.name))
        return asked

    async def test_each_option_maps_to_the_natives_choice(self):
        for option, value in (("allow-once", "yes"), ("allow-always", "yes_session"), ("reject-once", "no")):
            self.stdin.lines.clear()
            await self.ask({"outcome": {"outcome": "selected", "optionId": option}})
            self.assertEqual(self.stdin.lines, [{"type": "extension_ui_response", "id": "ui-1", "value": value}])

    async def test_the_request_names_the_childs_call_and_offers_three_options(self):
        asked = await self.ask({"outcome": {"outcome": "selected", "optionId": "allow-once"}})
        method, params = asked[0]
        self.assertEqual(method, "session/request_permission")
        call = params["toolCall"]
        self.assertEqual((call["toolCallId"], call["kind"], call["status"]), ("sub-1:call-a", "edit", "pending"))
        self.assertEqual(call["title"], "write")  # the nested card's own title (fix round 1, NIT-5)
        self.assertEqual(call["rawInput"], {"path": "out.txt", "content": "x"})
        self.assertEqual(call["locations"], [{"path": str(Path(self.tmp.name) / "out.txt")}])
        self.assertEqual(call["_meta"]["parentToolCallId"], "call-1")
        self.assertEqual(call["_meta"]["aelix/child"]["id"], "sub-1")
        self.assertEqual([(o["optionId"], o["name"], o["kind"]) for o in params["options"]], [
            ("allow-once", "Allow once", "allow_once"), ("allow-always", "Allow for this agent", "allow_always"),
            ("reject-once", "Reject", "reject_once")])

    async def test_a_child_in_another_directory_names_its_own_paths(self):
        sub = Path(self.tmp.name) / "pkg"
        asked = await self.ask({"outcome": {"outcome": "cancelled"}}, approval_request(cwd=str(sub)))
        self.assertEqual(asked[0][1]["toolCall"]["locations"], [{"path": str(sub / "out.txt")}])

    async def test_only_the_choices_native_offers_are_options(self):
        request = approval_request()
        request["choices"] = [c for c in request["choices"] if c["kind"] != "allow_always"]
        asked = await self.ask({"outcome": {"outcome": "selected", "optionId": "allow-always"}}, request)
        self.assertEqual([o["optionId"] for o in asked[0][1]["options"]], ["allow-once", "reject-once"])
        self.assertEqual(self.stdin.lines, [{"type": "extension_ui_response", "id": "ui-1", "cancelled": True}])

    async def test_an_unparented_child_nests_under_its_own_card(self):
        asked = await self.ask({"outcome": {"outcome": "cancelled"}}, approval_request(parent=None))
        self.assertEqual(asked[0][1]["toolCall"]["_meta"]["parentToolCallId"], "aelix-child:sub-1")

    async def test_every_other_ending_is_cancelled(self):
        """Guard: a78db59 also answers these cancelled (its dialog action is refused here)."""
        for reply in ({"outcome": {"outcome": "cancelled"}}, {"outcome": {"outcome": "selected", "optionId": "other"}},
                      # Fix round 1, V-F2 M53: a cancelled outcome that names an offered option.
                      {"outcome": {"outcome": "cancelled", "optionId": "allow-once"}},
                      {"outcome": {"outcome": "SELECTED", "optionId": "allow-once"}},
                      {}, ProtocolError("Client request failed", -32603), TimeoutError()):
            self.stdin.lines.clear()
            await self.ask(reply)
            self.assertEqual(self.stdin.lines, [{"type": "extension_ui_response", "id": "ui-1", "cancelled": True}],
                             reply)

    async def test_no_answer_is_written_once_natives_stream_closed(self):
        """Guard (passes on c09fc27): pins V-F4's M54."""
        self.session.rpc_closed = True
        await self.ask({"outcome": {"outcome": "selected", "optionId": "allow-once"}})
        self.assertEqual(self.stdin.lines, [])

    async def test_a_stale_epoch_is_cancelled_without_asking(self):
        self.session.children.accept("state", state()["data"])
        self.session.children.accept("state", state(epoch="e2")["data"])
        asked = await self.ask({"outcome": {"outcome": "selected", "optionId": "allow-once"}})
        self.assertEqual(asked, [])
        self.assertEqual(self.stdin.lines, [{"type": "extension_ui_response", "id": "ui-1", "cancelled": True}])

    async def test_a_malformed_request_is_cancelled_without_asking(self):
        """Guard: a78db59 also answers these cancelled (its dialog action is refused here)."""
        for broken in ({"child": None}, {"tool": {"callId": 5}}, {"choices": [{"id": "yes", "kind": "allow"}]}):
            self.stdin.lines.clear()
            asked = await self.ask({"outcome": {"outcome": "selected", "optionId": "allow-once"}},
                                   {**approval_request(), **broken})
            self.assertEqual((asked, self.stdin.lines[0].get("cancelled")), ([], True), broken)

    async def test_session_cancel_answers_an_open_request_cancelled(self):
        opened = asyncio.Event()

        async def client_request(method, params, **options):
            opened.set()
            await asyncio.Event().wait()
        self.server.client_request = client_request
        self.session.controls.capabilities = {"methods": []}
        await self.session.event(approval_request())
        await asyncio.wait_for(opened.wait(), 5)
        self.session.proc = SimpleNamespace(stdin=self.stdin, returncode=None)
        rpc_calls = []

        async def rpc(command, **kwargs):
            rpc_calls.append(command)
            return {}
        self.session.rpc = rpc
        await self.session.cancel()
        await asyncio.wait_for(asyncio.gather(*self.session.ui_tasks), 5)
        self.assertEqual(self.stdin.lines, [{"type": "extension_ui_response", "id": "ui-1", "cancelled": True}])

    async def test_a_request_during_a_cancelled_turn_is_cancelled_without_asking(self):
        self.session.cancelled = True
        self.session.done = asyncio.get_running_loop().create_future()
        asked = await self.ask({"outcome": {"outcome": "selected", "optionId": "allow-once"}})
        self.assertEqual(asked, [])
        self.session.done.set_result(None)
        # An idle session asks again: a background child's request is not the cancelled turn's.
        asked = await self.ask({"outcome": {"outcome": "selected", "optionId": "allow-once"}})
        self.assertEqual(len(asked), 1)

    async def test_a_parents_and_a_childs_request_are_open_together_and_both_answered(self):
        release = asyncio.Event()
        asked = []

        async def client_request(method, params, **options):
            asked.append(params["toolCall"]["toolCallId"])
            await release.wait()
            return {"outcome": {"outcome": "selected", "optionId": "allow-once"}}
        self.server.client_request = client_request
        listener = await asyncio.start_server(self.session.permission, host="127.0.0.1", port=0)
        writer = None
        try:
            host, port = listener.sockets[0].getsockname()[:2]
            await self.session.event({"type": "tool_execution_start", "toolCallId": "t1", "toolName": "write",
                                      "args": {"path": "p.txt"}})
            reader, writer = await asyncio.open_connection(host, port)
            writer.write(json.dumps({"toolCallId": "t1", "name": "write", "args": {"path": "p.txt"}}).encode() + b"\n")
            await writer.drain()
            await self.session.event(approval_request())
            async with asyncio.timeout(5):
                while len(asked) < 2:
                    await asyncio.sleep(0.01)
            self.assertEqual(sorted(asked), ["sub-1:call-a", "t1"])
            async with self.session.mutation_lock:  # Neither waits for the session's lock.
                release.set()
                self.assertEqual(json.loads(await asyncio.wait_for(reader.readline(), 5)), {"allow": True})
                await asyncio.wait_for(asyncio.gather(*self.session.ui_tasks), 5)
            self.assertEqual(self.stdin.lines, [{"type": "extension_ui_response", "id": "ui-1", "value": "yes"}])
        finally:
            release.set()
            if writer is not None:
                writer.close()
                await writer.wait_closed()
            listener.close()
            await asyncio.wait_for(listener.wait_closed(), 5)


class VoidQuestionTests(Base):
    """Fix round 1, P-LOW-1: a question native dropped is taken back from the client and answered cancelled."""

    capabilities = {"aelix/nestedToolCalls": True}

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.server.sessions[self.session.id] = self.session
        self.session.controls.capabilities = {"methods": ["children/list", "children/events"]}

    def asked(self):
        return [e["id"] for e in self.events if e.get("method") == "session/request_permission"]

    def withdrawn(self):
        return [e["params"] for e in self.events if e.get("method") == "$/cancel_request"]

    async def open(self, request=None):
        """One child's question, asked through the real client_request and held open by the client."""
        await self.feed(state(), frame("tool_start", seq=2, callId="call-a", name="write", args="out.txt"),
                        frame("approval", seq=3, callId="call-a", toolName="write", step="asked"))
        await self.session.event(request or approval_request())
        async with asyncio.timeout(5):
            while not self.asked():
                await asyncio.sleep(0)
        return self.asked()[0]

    async def settled(self):
        await asyncio.wait_for(asyncio.gather(*self.session.ui_tasks), 5)
        return self.stdin.lines

    async def check_void(self, *events):
        request_id = await self.open()
        await self.feed(*events)
        self.assertEqual(await self.settled(), [{"type": "extension_ui_response", "id": "ui-1", "cancelled": True}])
        self.assertEqual(self.withdrawn(), [{"requestId": request_id}])
        self.assertEqual((self.session.questions, request_id in self.server.pending), ({}, False))

    async def test_a_withdrawn_step_voids_it(self):
        await self.check_void(frame("approval", seq=4, callId="call-a", toolName="write", step="withdrawn"))

    async def test_a_denied_step_voids_it(self):
        await self.check_void(frame("approval", seq=4, callId="call-a", toolName="write", step="denied"))

    async def test_the_childs_end_voids_it(self):
        await self.check_void(state(seq=4, value="stopped", end="stopped", outcome="aborted", reason=None, note=None))

    async def test_a_frame_of_a_newer_epoch_voids_it(self):
        await self.check_void(state(child="sub-2", epoch="e2", parent="call-9"))

    async def test_a_children_list_reply_of_a_newer_epoch_voids_it(self):
        request_id = await self.open()

        async def rpc(command, **kwargs):
            return {"epoch": "e2", "children": []}
        self.session.rpc = rpc
        self.assertEqual(await asyncio.wait_for(self.server.dispatch(
            "_aelix/manage", {"sessionId": "session", "action": "children/list"}), 5), {"epoch": "e2", "children": []})
        self.assertEqual(await self.settled(), [{"type": "extension_ui_response", "id": "ui-1", "cancelled": True}])
        self.assertEqual(self.withdrawn(), [{"requestId": request_id}])
        self.assertTrue(self.session.children.stale(EPOCH))

    async def test_a_children_events_reply_of_the_same_epoch_leaves_it_open(self):
        """Guard (passes on c09fc27): only the question's own call, child and epoch void it."""
        request_id = await self.open()

        async def rpc(command, **kwargs):
            return {"epoch": EPOCH, "events": [], "nextSeq": 4, "lowSeq": 1, "gap": False}
        self.session.rpc = rpc
        await asyncio.wait_for(self.server.dispatch("_aelix/manage", {"sessionId": "session",
                                                                      "action": "children/events"}), 5)
        await self.feed(frame("approval", seq=4, callId="call-b", toolName="write", step="withdrawn"),
                        frame("approval", seq=5, callId="call-a", toolName="write", step="allowed"),
                        state(child="sub-2", seq=1), state(child="sub-2", seq=2, value="done", end="completed"))
        await asyncio.sleep(0.05)
        self.assertEqual([not task.done() for task in self.session.ui_tasks], [True])  # still open
        self.server.pending[request_id].set_result({"outcome": {"outcome": "selected", "optionId": "allow-once"}})
        self.assertEqual(await self.settled(), [{"type": "extension_ui_response", "id": "ui-1", "value": "yes"}])
        self.assertEqual(self.withdrawn(), [])

    async def test_a_question_from_an_epoch_never_seen_is_still_asked(self):
        """Guard (passes on c09fc27); the lead's call: only an epoch the adapter retired is stale."""
        await self.feed(state())
        await self.session.event(approval_request(epoch="e9"))
        async with asyncio.timeout(5):
            while not self.asked():
                await asyncio.sleep(0)
        self.server.pending[self.asked()[0]].set_result({"outcome": {"outcome": "selected", "optionId": "reject-once"}})
        self.assertEqual(await self.settled(), [{"type": "extension_ui_response", "id": "ui-1", "value": "no"}])

    async def test_session_cancel_during_a_manual_compaction_ends_it(self):
        """P-LOW-3: the early return for a manual /compact came before the question was cancelled."""
        request_id = await self.open()
        dialog_open, dialog = asyncio.Event(), asyncio.get_running_loop().create_future()

        async def client_action(session, action, arguments):
            dialog_open.set()
            return await dialog
        self.server.client_action = client_action
        await self.session.event({"type": "extension_ui_request", "id": "ui-2", "method": "confirm", "title": "t"})
        await asyncio.wait_for(dialog_open.wait(), 5)
        self.session.context.compaction = ("aelix-compact-x", "manual")
        await self.session.cancel()
        await asyncio.sleep(0.05)
        self.assertEqual(self.stdin.lines, [{"type": "extension_ui_response", "id": "ui-1", "cancelled": True}])
        self.assertEqual(self.withdrawn(), [{"requestId": request_id}])
        # The extension's dialog keeps its behaviour from before: still open, answered by the human.
        self.assertEqual(len([task for task in self.session.ui_tasks if not task.done()]), 1)
        dialog.set_result({"confirmed": True})
        await self.settled()
        self.assertEqual(self.stdin.lines[-1], {"type": "extension_ui_response", "id": "ui-2", "confirmed": True})

    async def test_native_owns_a_questions_timeout_and_the_adapter_only_backs_it_up(self):
        """P-LOW-4: the adapter's 290 s bound for a client's answer ended a child's question before native's 300 s."""
        from aelix_acp import server as server_module
        try:
            from aelix_coding_agent.rpc.control_ui import DIALOG_TIMEOUT_SECONDS
        except ImportError:  # the unit job has no native installed
            DIALOG_TIMEOUT_SECONDS = 300.0
        self.assertGreater(session_module.CHILD_QUESTION_SECONDS, DIALOG_TIMEOUT_SECONDS)
        with patch.object(server_module, "CLIENT_REQUEST_SECONDS", 0.05), \
                patch.object(session_module, "CHILD_QUESTION_SECONDS", 0.6):
            request_id = await self.open()
            await asyncio.sleep(0.3)  # past the general bound, before the question's
            self.assertEqual((self.stdin.lines, self.withdrawn()), ([], []))
            # Native's own timeout: it answered the child itself, and its denied step takes the request back.
            await self.feed(frame("approval", seq=4, callId="call-a", toolName="write", step="denied"))
            self.assertEqual(await self.settled(), [{"type": "extension_ui_response", "id": "ui-1", "cancelled": True}])
            self.assertEqual(self.withdrawn(), [{"requestId": request_id}])
            # With no step from native, the backstop ends it the same way.
            self.stdin.lines.clear()
            await self.session.event({**approval_request(call="call-b"), "id": "ui-3"})
            async with asyncio.timeout(5):
                while len(self.asked()) < 2:
                    await asyncio.sleep(0)
            started = time.monotonic()
            self.assertEqual(await self.settled(), [{"type": "extension_ui_response", "id": "ui-3", "cancelled": True}])
            self.assertGreaterEqual(time.monotonic() - started, 0.4)
            self.assertEqual(self.withdrawn(), [{"requestId": request_id}, {"requestId": self.asked()[1]}])

    async def test_a_client_that_is_gone_is_not_sent_the_take_back(self):
        await self.open()
        self.server.closing = True  # the adapter's stdin ended
        await self.feed(frame("approval", seq=4, callId="call-a", toolName="write", step="withdrawn"))
        self.assertEqual(await self.settled(), [{"type": "extension_ui_response", "id": "ui-1", "cancelled": True}])
        self.assertEqual(self.withdrawn(), [])

    async def test_the_adapters_stdin_ending_sends_the_gone_client_nothing(self):
        """Guard (passes on 91c4ed1); fix round 2, F3: kills S7 (run() never set `closing`).

        The client's stdin ends with a child's question open: the session's close
        takes nothing back and sends no card update to a client that is gone.
        """
        await self.open()
        self.session.proc = None  # no worker to end
        reader = asyncio.StreamReader()
        reader.feed_eof()

        class Input:
            readline = staticmethod(reader.readline)

            def close(self):
                pass
        before = len(self.events)
        with patch("aelix_acp.server.StdinReader", return_value=Input()):
            await asyncio.wait_for(self.server.run(), 10)
        self.assertEqual((self.server.closing, self.session.questions, "session" in self.server.sessions),
                         (True, {}, False))
        self.assertEqual(self.events[before:], [])

    async def test_a_dialog_native_opens_while_it_is_being_ended_is_answered_cancelled_unasked(self):
        """Fix round 1, Codex C-M3: frames are read through native's shutdown; no one is asked then."""
        self.session.ending = True
        await self.feed({"type": "extension_ui_request", "id": "ui-5", "method": "confirm", "title": "t"},
                        {"type": "extension_ui_request", "id": "ui-6", "method": "notify", "message": "m"},
                        approval_request())
        self.assertEqual((self.session.ui_tasks, self.asked()), (set(), []))
        self.assertEqual(self.stdin.lines, [{"type": "extension_ui_response", "id": "ui-5", "cancelled": True},
                                            {"type": "extension_ui_response", "id": "ui-1", "cancelled": True}])

    async def test_a_void_wins_over_a_client_answer_in_the_same_loop_turn(self):
        request_id = await self.open()
        self.server.pending[request_id].set_result({"outcome": {"outcome": "selected", "optionId": "allow-once"}})
        await self.feed(frame("approval", seq=4, callId="call-a", toolName="write", step="withdrawn"))  # no yield
        self.assertEqual(await self.settled(), [{"type": "extension_ui_response", "id": "ui-1", "cancelled": True}])
        self.assertEqual(self.withdrawn(), [])  # the client answered it: nothing to take back (fix round 2, S3)

    async def test_a_question_voided_before_its_task_ran_is_not_asked(self):
        await self.feed(state())
        await self.session.event(approval_request())  # its task has not run yet
        await self.feed(frame("approval", seq=2, callId="call-a", toolName="write", step="withdrawn"))
        self.assertEqual(await self.settled(), [{"type": "extension_ui_response", "id": "ui-1", "cancelled": True}])
        self.assertEqual((self.asked(), self.withdrawn()), ([], []))


class CancelBeforeTheDialogRanTests(Base):
    """Fix round 2, F2: a session/cancel handled in the loop turn that created a dialog's task (verifier r2's race)."""

    capabilities = {"aelix/nestedToolCalls": True}

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.server.sessions[self.session.id] = self.session
        self.session.controls.capabilities = {"methods": []}
        self.asked, self.sent = [], []

        async def client_request(method, params, **options):
            self.asked.append(params)
            return await asyncio.get_running_loop().create_future()

        async def client_action(session, action, arguments):
            self.asked.append(arguments)
            return await asyncio.get_running_loop().create_future()

        async def rpc(command, **kwargs):
            self.sent.append(command)
            return {}
        self.server.client_request, self.server.client_action, self.session.rpc = client_request, client_action, rpc

    async def answers_after_a_cancel(self, request):
        # The stdin loop read the client's cancel first and scheduled its handler; the RPC reader then read
        # native's request and created its task in the same loop turn, so the cancel reaches a task not yet run.
        handler = asyncio.create_task(self.server.handle({"jsonrpc": "2.0", "method": "session/cancel",
                                                          "params": {"sessionId": "session"}}))
        await self.session.event(request)
        await asyncio.wait_for(handler, 5)
        async with asyncio.timeout(5):
            while self.session.ui_tasks:
                await asyncio.sleep(0)
        await asyncio.sleep(0.05)  # an answer, or a second one, would be written by now
        return self.stdin.lines

    async def test_a_childs_question_is_answered_cancelled_once(self):
        """Fails on 91c4ed1: native got no answer (it waits its 300 s, and the next child's question behind it)."""
        self.assertEqual(await self.answers_after_a_cancel(approval_request()),
                         [{"type": "extension_ui_response", "id": "ui-1", "cancelled": True}])
        self.assertEqual((self.asked, self.sent, self.session.questions, self.session.unstarted),
                         ([], ["abort"], {}, set()))

    async def test_an_extensions_dialog_is_answered_cancelled_once(self):
        """Fails on 91c4ed1 (and on a78db59): the same race left an extension's dialog unanswered."""
        self.assertEqual(await self.answers_after_a_cancel(
            {"type": "extension_ui_request", "id": "ui-2", "method": "confirm", "title": "t"}),
            [{"type": "extension_ui_response", "id": "ui-2", "cancelled": True}])
        self.assertEqual((self.asked, self.session.unstarted), ([], set()))

    async def test_a_worker_that_ended_or_closed_its_stream_is_not_written_to(self):
        """The cancelled answer goes only to a worker still reading its stdin.

        Fails on 91c4ed1 by the question left in `questions`; nothing was written there either.
        """
        for gone in ("rpc_closed", "returncode"):
            with self.subTest(gone):
                self.stdin.lines.clear()
                self.session.rpc_closed = gone == "rpc_closed"
                self.session.proc.returncode = 1 if gone == "returncode" else None
                self.assertEqual(await self.answers_after_a_cancel(approval_request()), [])
                self.assertEqual((self.session.questions, getattr(self.session, "unstarted", set())), ({}, set()))

    async def test_a_notification_gets_no_answer(self):
        """Guard (passes on 91c4ed1): a method that needs no answer gets none from the cancel either."""
        handler = asyncio.create_task(self.server.handle({"jsonrpc": "2.0", "method": "session/cancel",
                                                          "params": {"sessionId": "session"}}))
        await self.session.event({"type": "extension_ui_request", "id": "ui-3", "method": "notify", "message": "m"})
        await asyncio.wait_for(handler, 5)
        await asyncio.sleep(0.05)
        self.assertEqual((self.stdin.lines, getattr(self.session, "unstarted", set())), ([], set()))  # 91c4ed1 has none

    async def test_a_dialog_cancelled_after_it_ran_is_answered_once(self):
        """Guard (passes on 91c4ed1): its own answer stands, and the cancelled-before-it-ran answer is not added."""
        await self.session.event(approval_request())
        await self.session.event({"type": "extension_ui_request", "id": "ui-2", "method": "confirm", "title": "t"})
        async with asyncio.timeout(5):
            while len(self.asked) < 2:
                await asyncio.sleep(0)
        await asyncio.wait_for(self.server.handle({"jsonrpc": "2.0", "method": "session/cancel",
                                                   "params": {"sessionId": "session"}}), 5)
        await asyncio.wait_for(asyncio.gather(*self.session.ui_tasks, return_exceptions=True), 5)
        await asyncio.sleep(0.05)
        self.assertEqual(sorted(self.stdin.lines, key=lambda line: line["id"]),
                         [{"type": "extension_ui_response", "id": "ui-1", "cancelled": True},
                          {"type": "extension_ui_response", "id": "ui-2", "cancelled": True}])


class StandardApprovalTests(Base):
    async def test_a_client_without_nesting_gets_no_parent_call_id(self):
        asked = []

        async def client_request(method, params, **options):
            asked.append(params)
            return {"outcome": {"outcome": "selected", "optionId": "reject-once"}}
        self.server.client_request = client_request
        await self.session.handle_ui(approval_request())
        self.assertNotIn("parentToolCallId", asked[0]["toolCall"]["_meta"])
        # It has no card for this id: native's title says who asks for what.
        self.assertEqual(asked[0]["toolCall"]["title"], "reviewer agent sub-1 asks: Create/overwrite out.txt?")
        self.assertEqual(self.stdin.lines, [{"type": "extension_ui_response", "id": "ui-1", "value": "no"}])


class NestedOnlyTests(unittest.TestCase):
    def test_a_parent_call_whose_id_has_the_synthetic_prefix_is_not_nested_only(self):
        """Guard (passes on 91c4ed1); fix round 2, F3: kills D8. A card is synthetic by its prefix and its child meta."""
        children = {META_KEY: {"version": 1, "children": []}}
        self.assertFalse(nested_only({"sessionUpdate": "tool_call_update", "toolCallId": "aelix-child:call_1",
                                      "_meta": children}))
        self.assertTrue(nested_only({"sessionUpdate": "tool_call_update", "toolCallId": "aelix-child:sub-1",
                                     "_meta": {**children, "aelix/child": {"id": "sub-1"}}}))
        self.assertTrue(nested_only({"toolCallId": "sub-1:c1", "_meta": {"parentToolCallId": "call_1"}}))
        self.assertFalse(nested_only({"toolCallId": "call_1", "_meta": {**children, "aelix/child": {"id": "sub-1"}}}))


class ClientRequestTests(unittest.IsolatedAsyncioTestCase):
    """Fix round 2, F3: which requests the adapter takes back with `$/cancel_request` (kills S1 and S3)."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = Server(SimpleNamespace(state_dir=self.tmp.name, profile="coding", turn_timeout=600))
        self.sent = []

        async def send(event):
            self.sent.append(event)
        self.server.send = send

    async def asyncTearDown(self):
        self.server.db.close()
        self.tmp.cleanup()

    def withdrawn(self):
        return [event["params"]["requestId"] for event in self.sent if event.get("method") == "$/cancel_request"]

    async def request(self, **options):
        task = asyncio.create_task(self.server.client_request("session/request_permission", {}, **options))
        async with asyncio.timeout(5):
            while not self.server.pending:
                await asyncio.sleep(0)
        return task, next(iter(self.server.pending))

    async def test_only_a_childs_question_is_taken_back(self):
        """Guard (passes on 91c4ed1): kills S1. The parent's own request and a client action are only dropped."""
        task, _request_id = await self.request()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        with self.assertRaises(TimeoutError):
            await self.server.client_request("_aelix/client_action", {}, timeout=0.01)
        self.assertEqual(self.withdrawn(), [])
        task, request_id = await self.request(withdraw=True)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.withdrawn(), [request_id])

    async def test_child_approval_keeps_its_slot_until_withdrawal_is_sent(self):
        self.server.withdrawal_slots = asyncio.BoundedSemaphore(1)
        started, release = asyncio.Event(), asyncio.Event()
        send = self.server.send

        async def blocked_send(event):
            if event.get("method") == "$/cancel_request":
                started.set()
                await release.wait()
            await send(event)

        self.server.send = blocked_send
        task, request_id = await self.request(withdraw=True)
        try:
            for state in ("displayed", "withdrawing"):
                with self.subTest(state=state), self.assertRaisesRegex(ProtocolError, "Too many outstanding"):
                    await self.server.client_request("session/request_permission", {}, withdraw=True, timeout=0.1)
                if state == "displayed":
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    await asyncio.wait_for(started.wait(), 5)
        finally:
            release.set()
            await asyncio.gather(task, *self.server.withdrawals, return_exceptions=True)
        self.assertEqual(self.withdrawn(), [request_id])

        next_task, next_id = await self.request(withdraw=True)
        self.server.pending[next_id].set_result({})
        self.assertEqual(await next_task, {})
        self.assertFalse(self.server.withdrawal_slots.locked())

    async def test_a_question_the_client_answered_is_not_taken_back(self):
        """Guard (passes on 91c4ed1): kills S3 on Python 3.12 and later.

        The client's answer is read, then its task is cancelled before it resumed;
        Python 3.11's wait_for returns the answer instead, so nothing is taken back
        there either.
        """
        task, request_id = await self.request(withdraw=True)
        self.server.pending[request_id].set_result({"outcome": {"outcome": "selected", "optionId": "allow-once"}})
        task.cancel()  # before the task resumed with the answer
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.withdrawn(), [])


class ReplayTests(unittest.IsolatedAsyncioTestCase):
    """Fix round 1, Codex C-M4: session/load replays nested updates only to a client that declared nesting."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = str(Path(self.tmp.name).resolve())  # session/load resolves the cwd

    async def server(self, capabilities):
        server = Server(SimpleNamespace(state_dir=self.tmp.name, profile="coding", turn_timeout=600))
        self.addCleanup(server.db.close)
        server.client_capabilities = {"_meta": dict(capabilities)}
        server.events = []

        async def send(event):
            server.events.append(json.loads(json.dumps(event)))  # as written, not the live entries
        server.send = send
        return server

    async def record(self, capabilities):
        server = await self.server(capabilities)
        session = Session(server, "session", self.root)
        session.proc = SimpleNamespace(stdin=Stdin(), returncode=None)
        await session.save()
        for event in ({"type": "tool_execution_start", "toolCallId": "call-1", "toolName": "agent", "args": {}},
                      state(), frame("tool_start", seq=2, callId="c1", name="read", args="a"),
                      frame("tool_end", seq=3, callId="c1", status="ok"), frame("text", seq=4, text="done"),
                      {"type": "tool_execution_end", "toolCallId": "call-1", "toolName": "agent",
                       "result": {"content": []}},
                      state(child="sub-2", parent=None), frame("tool_start", child="sub-2", seq=2, callId="c2",
                                                               name="read", args="b"),
                      state(child="sub-2", seq=3, parent=None, value="done", end="completed")):
            await session.event(event)
        await session.live_history.flush()
        return [e["params"]["update"] for e in server.events if e.get("method") == "session/update"]

    async def load(self, capabilities):
        server = await self.server(capabilities)

        async def start(session, filename):
            session.proc = SimpleNamespace(returncode=None)
            session.lease = acquire(self.root, session.id)
            self.addCleanup(session.lease.close)

            async def rpc(command, **kwargs):
                return {}
            session.rpc = rpc
        with patch.object(Session, "start", start):
            await server.dispatch("session/load", {"sessionId": "session", "cwd": self.root, "mcpServers": []})
        return [e["params"]["update"] for e in server.events if e.get("method") == "session/update"]

    @staticmethod
    def calls(updates):
        return [(u["sessionUpdate"], u.get("toolCallId")) for u in updates if "toolCallId" in u]

    async def test_a_client_without_nesting_loads_a_nesting_recording_as_the_live_path_sends_it(self):
        live = await self.record({"aelix/nestedToolCalls": True})
        self.assertIn(("tool_call", "sub-1:c1"), self.calls(live))
        self.assertIn(("tool_call", "aelix-child:sub-2"), self.calls(live))
        replayed = await self.load({})
        self.assertEqual(self.calls(replayed), [call for call in self.calls(live) if call[1] == "call-1"])
        self.assertGreaterEqual(len(self.calls(replayed)), 4)
        self.assertFalse([u for u in replayed if "parentToolCallId" in (u.get("_meta") or {})])
        # The parent card's _meta updates are the ones a client without nesting got live.
        plain = [u for u in live if u.get("toolCallId") == "call-1"]
        self.assertEqual([u for u in replayed if u.get("toolCallId") == "call-1"], plain)

    async def test_a_nesting_client_loads_every_recorded_update(self):
        """Guard: passes on c09fc27 too (it replayed every update to every client)."""
        nested = await self.record({"aelix/nestedToolCalls": True})
        self.assertEqual(self.calls(await self.load({"aelix/nestedToolCalls": True})), self.calls(nested))

    async def test_a_nesting_client_loads_a_recording_without_nesting_as_it_was(self):
        """Guard: the reverse of the first; passes on c09fc27 too."""
        plain = await self.record({})
        self.assertEqual([u for u in await self.load({"aelix/nestedToolCalls": True}) if "toolCallId" in u],
                         [u for u in plain if "toolCallId" in u])


class ManageNoticesTests(Base):
    async def test_children_notices_runs_the_notice_turn_under_the_lock(self):
        self.session.controls.capabilities = {"methods": ["children/notices"]}
        self.server.sessions[self.session.id] = self.session
        sent = []

        async def rpc(command, **kwargs):
            sent.append(kwargs.get("method", command))
            if command == "get_state":
                return {"isStreaming": False}
            self.assertTrue(self.session.mutation_lock.locked())
            return {"started": False, "placed": 0, "reason": "nothing", "remaining": 0}
        self.session.rpc = rpc
        result = await asyncio.wait_for(self.server.dispatch(
            "_aelix/manage", {"sessionId": "session", "action": "children/notices"}), 5)
        self.assertEqual(result, {"started": False, "placed": 0, "reason": "nothing", "remaining": 0,
                                  "stopReason": "end_turn"})
        self.assertEqual(sent[0], "children/notices")


class FenceTests(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.session.controls.capabilities = {"methods": ["children/steer", "children/list", "children/notices"]}
        self.server.sessions[self.session.id] = self.session
        self.sent = []
        self.answer = asyncio.Event()

        async def rpc(command, **kwargs):
            self.sent.append(kwargs.get("method", command))
            await self.answer.wait()
            return {"outcome": "queued"}
        self.session.rpc = rpc

    def steer(self, bound=None):
        call = self.server.dispatch("_aelix/manage", {"sessionId": "session", "action": "children/steer",
                                                      "arguments": {"id": "sub-1", "text": "go"}})
        return call if bound is None else asyncio.wait_for(call, bound)

    async def test_an_intervention_needs_neither_the_lock_nor_the_activity_lease(self):
        self.answer.set()
        with acquire(self.tmp.name, "session", purpose="activity"):
            async with self.session.mutation_lock:
                self.assertEqual(await self.steer(5), {"outcome": "queued"})
        self.assertEqual(self.sent, ["children/steer"])

    async def test_a_malformed_action_is_still_a_parameter_error(self):
        """Guard: -32602 on a78db59 too; the children routing in front of it must not turn it into a TypeError."""
        with self.assertRaisesRegex(ProtocolError, "action must be a non-empty string") as refused:
            await asyncio.wait_for(self.server.dispatch("_aelix/manage", {"sessionId": "session", "action": ["x"]}), 5)
        self.assertEqual(refused.exception.code, -32602)

    async def test_children_notices_stays_under_the_lock(self):
        """Guard: busy on a78db59 too."""
        async with self.session.mutation_lock:
            with self.assertRaisesRegex(ProtocolError, "Session is busy"):
                await self.server.dispatch_request("_aelix/manage", {"sessionId": "session",
                                                                     "action": "children/notices"})

    async def test_an_action_native_does_not_list_is_refused(self):
        """Guard: refused on a78db59 too (it has no children actions at all)."""
        with self.assertRaisesRegex(ProtocolError, "Unsupported management action"):
            await asyncio.wait_for(self.server.dispatch("_aelix/manage", {"sessionId": "session", "action": "children/stop",
                                                                          "arguments": {"id": "x"}}), 5)

    async def test_a_changing_session_refuses_before_anything_is_written(self):
        proc = self.session.proc
        for prepare in (lambda: setattr(self.session, "changing", 1),
                        lambda: self.server.starting_ids.add("session"),
                        lambda: setattr(self.session, "_close_task", object()),
                        lambda: setattr(self.session, "rpc_closed", True),
                        # Fix round 1, V-F4 M61: a worker that is gone or has exited.
                        lambda: setattr(self.session, "proc", None),
                        lambda: setattr(self.session, "proc", SimpleNamespace(stdin=self.stdin, returncode=1))):
            self.session.changing, self.session._close_task, self.session.rpc_closed = 0, None, False
            self.session.proc = proc
            self.server.starting_ids.clear()
            prepare()
            with self.assertRaises(ProtocolError) as refused:
                await self.steer(5)
            self.assertEqual((str(refused.exception), refused.exception.code, refused.exception.data),
                             ("the session is changing", -32000, {"kind": "session_changing"}))
        self.assertEqual(self.sent, [])

    async def test_an_intervention_in_flight_when_the_fence_rises_answers_delivery_unknown(self):
        steer = asyncio.create_task(self.steer())
        async with asyncio.timeout(5):
            while not self.session.interventions:
                await asyncio.sleep(0)
        self.session.raise_fence()
        done, _pending = await asyncio.wait({steer}, timeout=2)
        self.assertIn(steer, done, "the fence did not end the waiting intervention")
        with self.assertRaises(ProtocolError) as unknown:
            await steer
        self.assertEqual(str(unknown.exception), "the session changed while this request ran; its delivery is unknown")
        self.assertEqual(unknown.exception.code, TURN_FAILED_AFTER_DISPATCH)
        self.assertEqual(unknown.exception.data["cause"], {"kind": "session_changed", "command": "children/steer"})
        self.assertEqual(self.sent, ["children/steer"])
        self.assertEqual((self.session.interventions, self.session.fenced), ({}, set()))
        self.assertEqual(steer.cancelling(), 0)

    async def test_close_raises_the_fence_and_lowers_it_after(self):
        steer = asyncio.create_task(self.steer())
        async with asyncio.timeout(5):
            while not self.session.interventions:
                await asyncio.sleep(0)
        self.session.proc = None
        await asyncio.wait_for(self.session.close(), 5)
        done, _pending = await asyncio.wait({steer}, timeout=2)
        self.assertIn(steer, done, "close did not end the waiting intervention")
        with self.assertRaisesRegex(ProtocolError, "delivery is unknown"):
            await steer
        self.assertEqual(self.session.changing, 0)

    async def test_session_close_goes_on_past_an_intervention_and_ends_it(self):
        """Guard (passes on 91c4ed1 merged with dd772a6): aelix-acp #35's session/close passes the fence too."""
        steer = asyncio.create_task(self.steer())
        async with asyncio.timeout(5):
            while not self.session.interventions:
                await asyncio.sleep(0)
        self.session.proc = None
        self.assertEqual(await asyncio.wait_for(self.server.dispatch("session/close", {"sessionId": "session"}), 5), {})
        with self.assertRaisesRegex(ProtocolError, "delivery is unknown"):
            await asyncio.wait_for(steer, 2)
        self.assertEqual((self.session.changing, "session" in self.server.sessions), (0, False))

    async def test_a_cancel_that_is_not_the_fences_propagates(self):
        steer = asyncio.create_task(self.steer())
        async with asyncio.timeout(5):
            while not self.session.interventions:
                await asyncio.sleep(0)
        steer.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await steer


class Pipe:
    """A worker's stdin whose lines a test awaits one at a time."""

    def __init__(self):
        self.lines = asyncio.Queue()

    def write(self, data):
        for line in data.decode().splitlines():
            self.lines.put_nowait(json.loads(line))

    async def drain(self):
        pass

    async def read(self):
        return await asyncio.wait_for(self.lines.get(), 5)


def answer_line(command, data):
    return (json.dumps({"type": "response", "id": command["id"], "success": True, "data": data}) + "\n").encode()


class FenceOrderTests(Base):
    """Fix round 1, Codex C-M1: one answer for one interleaving on Python 3.11 to 3.14 (the real rpc and reader)."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.session.controls.capabilities = {"methods": ["children/steer"]}
        self.server.sessions[self.session.id] = self.session
        self.pipe, self.stdout = Pipe(), asyncio.StreamReader()
        self.session.proc = SimpleNamespace(stdin=self.pipe, stdout=self.stdout, returncode=None)
        self.session._close_task = None
        self.reader = asyncio.create_task(self.session.read_rpc())
        self.steer = asyncio.create_task(self.server.dispatch("_aelix/manage", {
            "sessionId": "session", "action": "children/steer", "arguments": {"id": "sub-1", "text": "go"}}))
        self.command = await self.pipe.read()
        self.assertEqual(self.command["params"], {"id": "sub-1", "text": "go"})

    async def asyncTearDown(self):
        self.session._close_task = asyncio.get_running_loop().create_future()  # No close() from the reader's end.
        self.reader.cancel()
        await asyncio.gather(self.reader, self.steer, return_exceptions=True)
        await super().asyncTearDown()

    async def test_an_answer_native_gave_before_the_fence_rose_is_returned(self):
        self.stdout.feed_data(answer_line(self.command, {"outcome": "queued"}))
        async with asyncio.timeout(5):
            while not all(future.done() for future in self.session.pending.values()):
                await asyncio.sleep(0)
        self.session.raise_fence()  # The reader resolved the answer; the steer has not resumed yet.
        self.assertEqual(await asyncio.wait_for(self.steer, 5), {"outcome": "queued"})
        self.assertEqual(self.steer.cancelling(), 0)

    async def test_an_answer_that_arrives_after_the_fence_rose_is_delivery_unknown(self):
        """Fails on c09fc27 under Python 3.11 only (its wait_for returned the answer); 3.12 already raised."""
        # The answer is buffered first, so the reader resolves it before the cancelled steer resumes.
        self.stdout.feed_data(answer_line(self.command, {"outcome": "queued"}))
        self.session.raise_fence()
        with self.assertRaises(ProtocolError) as unknown:
            await asyncio.wait_for(self.steer, 5)
        self.assertEqual((unknown.exception.code, unknown.exception.data["cause"]),
                         (TURN_FAILED_AFTER_DISPATCH, {"kind": "session_changed", "command": "children/steer"}))
        self.assertEqual(self.steer.cancelling(), 0)

    async def test_an_answer_read_after_a_restart_began_and_before_its_close_is_returned(self):
        """Guard (passes on 91c4ed1); fix round 2, F1: restart() raising the counter ends nothing.

        Only the worker's close (`raise_fence()`, the first step of `_close()`)
        ends the interventions native has not answered. Codex's `fence` case, on
        the real restart() with the worker's start and end stubbed.
        """
        seen = []

        async def end_worker():
            seen.append((self.session.changing, set(self.session.fenced), self.steer.done()))

        async def nothing(*args, **kwargs):
            pass
        self.session._end_worker = end_worker
        self.session.lease = SimpleNamespace(closed=False)
        self.session.start = self.session.commands = self.session.publish_context_usage = nothing
        restart = asyncio.create_task(self.session.restart())
        # Read by the reader after restart() raised the counter, before its close() runs.
        self.stdout.feed_data(answer_line(self.command, {"outcome": "queued"}))
        await asyncio.wait_for(restart, 5)
        self.assertEqual(await asyncio.wait_for(self.steer, 5), {"outcome": "queued"})
        self.assertEqual(seen, [(2, set(), False)])  # the close's fence found it answered, not yet returned
        self.assertEqual(self.session.changing, 0)


class DispatchedTests(Base):
    async def test_a_concurrent_control_does_not_mark_the_turn_dispatched(self):
        written = []

        class Proc:
            returncode = None

            class stdin:
                @staticmethod
                def write(data):
                    written.append(json.loads(data))

                @staticmethod
                async def drain():
                    pass
        self.session.proc = Proc()
        self.session.turn_active = True
        for method in ("children/list", "children/events", "children/steer", "children/wrap_up", "children/stop"):
            with self.assertRaises(ProtocolError):
                await self.session.rpc("aelix_control", method=method, params={}, _timeout=0.01)
            self.assertFalse(self.session.dispatched, method)
        with self.assertRaises(ProtocolError):
            await self.session.rpc("aelix_control", method="children/notices", params={}, _timeout=0.01)
        self.assertTrue(self.session.dispatched)


class CadenceTests(Base):
    async def run_cancel(self, streaming_for):
        self.session.controls.capabilities = {"methods": ["commands/cancel"]}
        sent = []
        start = time.monotonic()

        async def rpc(command, **kwargs):
            sent.append((round(time.monotonic() - start, 2), kwargs.get("method", command)))
            if command == "get_state":
                return {"isStreaming": time.monotonic() - start < streaming_for}
            return {}
        self.session.rpc = rpc
        await self.session.cancel()
        await self.session.cancel(again=True)  # _prompt's re-cancel after native accepted the turn.
        await self.session.wait_until_idle()
        return [at for at, command in sent if command == "abort"], [c for _at, c in sent]

    async def test_one_session_cancel_sends_one_abort_in_the_first_two_seconds(self):
        aborts, commands = await self.run_cancel(1.0)
        self.assertEqual(len(aborts), 1, commands)
        self.assertEqual(commands.count("commands/cancel"), 1)

    async def test_a_turn_still_streaming_is_aborted_again_at_the_grace(self):
        aborts, _commands = await self.run_cancel(2.4)
        self.assertEqual(len(aborts), 2, aborts)
        self.assertGreaterEqual(aborts[1] - aborts[0], session_module.REABORT_SECONDS - 0.05)

    async def test_a_cancel_while_native_accepts_the_prompt_is_one_abort_in_the_first_two_seconds(self):
        self.session.controls.capabilities = {"methods": ["commands/cancel"]}
        sent, accepted = [], asyncio.Event()
        start = time.monotonic()

        async def rpc(command, **kwargs):
            sent.append(kwargs.get("method", command))
            if command == "prompt":
                await accepted.wait()
            if command == "get_state":
                return {"isStreaming": time.monotonic() - start < 1.0}
            return {}
        self.session.rpc = rpc
        turn = asyncio.create_task(self.session.drive_turn(lambda: self.session.rpc("prompt", message="x")))
        async with asyncio.timeout(5):
            while "prompt" not in sent:
                await asyncio.sleep(0)
        await self.session.cancel()  # session/cancel while native has not answered the prompt yet
        accepted.set()
        self.assertEqual(await asyncio.wait_for(turn, 10), {"stopReason": "cancelled"})
        self.assertEqual((sent.count("abort"), sent.count("commands/cancel")), (1, 1), sent)

    async def test_poll_abort_during_native_stdin_backpressure_prevents_a_duplicate(self):
        self.session.controls.capabilities = {"methods": ["commands/cancel"]}
        self.server.sessions[self.session.id] = self.session
        release_drain = asyncio.Event()
        cancel = None

        class BackpressuredPipe(Pipe):
            async def drain(self):
                if asyncio.current_task() is cancel:
                    await release_drain.wait()

        pipe, stdout = BackpressuredPipe(), asyncio.StreamReader()
        self.session.proc = SimpleNamespace(stdin=pipe, stdout=stdout, returncode=None)
        self.session._close_task = asyncio.get_running_loop().create_future()  # The test owns the teardown.
        reader = asyncio.create_task(self.session.read_rpc())
        turn = asyncio.create_task(self.session._prompt([{"type": "text", "text": "hold"}]))
        try:
            prompt = await pipe.read()
            self.assertEqual(prompt["type"], "prompt")
            stdout.feed_data(answer_line(prompt, {}))
            poll = await pipe.read()
            self.assertEqual(poll["type"], "get_state")
            cancel = asyncio.create_task(self.server.dispatch("session/cancel", {"sessionId": "session"}))
            control = await pipe.read()
            self.assertEqual(control.get("method"), "commands/cancel")
            # The poll can dispatch its abort before the cancel's stdin drain finishes.
            stdout.feed_data(answer_line(poll, {"isStreaming": True}))
            first = await pipe.read()
            self.assertEqual(first["type"], "abort")
            self.assertFalse(cancel.done())
            release_drain.set()
            stdout.feed_data(answer_line(control, {"cancelled": False}) + answer_line(first, {}))
            following = await pipe.read()
            self.assertEqual(following["type"], "get_state", following)  # not a second abort
            self.assertEqual(await asyncio.wait_for(cancel, 5), {})
            stdout.feed_data(answer_line(following, {"isStreaming": False}))
            stats = await pipe.read()
            self.assertEqual(stats["type"], "get_session_stats")
            stdout.feed_data(answer_line(stats, {}))
            self.assertEqual(await asyncio.wait_for(turn, 5), {"stopReason": "cancelled"})
            self.assertEqual((self.session.aborts, pipe.lines.qsize()), (1, 0))
        finally:
            for task in (reader, turn, *([cancel] if cancel else [])):
                task.cancel()
            await asyncio.gather(reader, turn, *([cancel] if cancel else []), return_exceptions=True)

    async def test_old_cancel_cannot_abort_a_new_turn_after_native_stdin_drain(self):
        self.session.controls.capabilities = {"methods": ["commands/cancel"]}
        draining, release = asyncio.Event(), asyncio.Event()
        sent = []

        async def rpc(command, **params):
            method = params.get("method", command)
            sent.append(method)
            if method == "commands/cancel":
                draining.set()
                await release.wait()
            return {}

        self.session.rpc = rpc
        stop = asyncio.create_task(self.session.cancel())
        try:
            await asyncio.wait_for(draining.wait(), 2)
            self.session.reset_cancellation()
        finally:
            release.set()
            await asyncio.wait_for(stop, 2)
        self.assertEqual(sent, ["commands/cancel"])
        self.assertFalse(self.session.cancelled)

    async def test_a_second_client_cancel_is_obeyed_at_once(self):
        """Guard: each client cancel aborts at once, as on a78db59."""
        sent = []

        async def rpc(command, **kwargs):
            sent.append(kwargs.get("method", command))
            return {}
        self.session.rpc = rpc
        await self.session.cancel()
        await self.session.cancel()
        self.assertEqual(sent.count("abort"), 2)


class NoticeTurnTests(Base):
    async def test_a_notice_turn_resets_the_cancel_and_returns_natives_answer(self):
        self.session.cancelled = True
        self.session.tools["old"] = {}
        self.session.children.accept("state", state()["data"])
        sent = []

        async def rpc(command, **kwargs):
            sent.append(kwargs.get("method", command))
            if command == "get_state":
                return {"isStreaming": False}
            return {"started": True, "placed": 1, "reason": None, "remaining": 0}
        self.session.rpc = rpc
        result = await self.session.notice_turn()
        self.assertEqual(result, {"started": True, "placed": 1, "reason": None, "remaining": 0, "stopReason": "end_turn"})
        self.assertEqual(sent[0], "children/notices")
        self.assertFalse(self.session.cancelled)
        self.assertEqual(self.session.tools, {})
        self.assertIn((EPOCH, "sub-1"), self.session.children.parents)

    async def test_session_cancel_cancels_a_notice_turn(self):
        self.session.controls.capabilities = {"methods": ["commands/cancel"]}
        started = asyncio.Event()
        cancelled = asyncio.Event()
        sent = []

        async def rpc(command, **kwargs):
            method = kwargs.get("method", command)
            sent.append(method)
            if method == "children/notices":
                started.set()
                await cancelled.wait()
                from aelix_acp.wire.errors import BackendError
                raise BackendError("Command cancelled", "aelix_control")
            if method == "commands/cancel":
                cancelled.set()
            if command == "get_state":
                return {"isStreaming": False}
            return {}
        self.session.rpc = rpc
        turn = asyncio.create_task(self.session.notice_turn())
        await asyncio.wait_for(started.wait(), 5)
        await self.session.cancel()
        self.assertEqual(await asyncio.wait_for(turn, 5), {"stopReason": "cancelled"})
        self.assertEqual(sent.count("abort"), 1)

    async def test_a_native_failure_after_the_request_was_sent_is_not_retryable(self):
        from aelix_acp.wire.errors import BackendError

        async def rpc(command, **kwargs):
            raise BackendError("This RPC host has no job service", "aelix_control")
        self.session.rpc = rpc
        with self.assertRaises(ProtocolError) as failed:
            await self.session.notice_turn()
        self.assertEqual(failed.exception.code, TURN_FAILED_AFTER_DISPATCH)

    async def test_a_running_turn_refuses_a_notice_turn(self):
        self.session.done = asyncio.get_running_loop().create_future()
        with self.assertRaisesRegex(ProtocolError, "already running"):
            await asyncio.wait_for(self.session.notice_turn(), 5)


WORKER = """import json, signal, sys, time
def say(event, seq, **data):
    print(json.dumps({"type": "aelix_child", "event": event,
                      "data": {"id": "sub-1", "seq": seq, "epoch": "e1", **data}}), flush=True)
parent = json.loads(sys.argv[1])
say("state", 1, state="running", profile="reviewer", sessionId=None, jobId=None, parentToolCallId=parent,
    background=False, wrappingUp=False)
say("tool_start", 2, callId="c1", name="read", args="a")
def finish(*_):
    say("tool_end", 4, callId="c1", status="no_result")
    say("state", 5, state="stopped", profile="reviewer", sessionId=None, jobId=None, parentToolCallId=parent,
        background=False, wrappingUp=False, end="stopped", outcome="aborted", reason=None, note=None)
    sys.exit(0)
if sys.argv[2] == "frames":
    signal.signal(signal.SIGTERM, finish)
signal.signal(signal.SIGUSR1, lambda *_: say("text", 3, text="busy"))
time.sleep(60)
"""


class EndingNotificationTests(Base):
    """Fix round 2, N1: `_aelix/notice` and `_aelix/subagent` follow `session/update`'s rule while the worker is ended."""

    capabilities = {"aelix/notices": True}
    FRAMES = ({"type": "aelix_notice", "event": "queued", "data": {}},
              {"type": "aelix_subagent", "event": "subagent_start", "data": {"id": "s1", "profile": "p"}},
              {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "x"}})

    async def sent_while_ending(self):
        self.session.ending = True
        await self.feed(*self.FRAMES)
        return [event["method"] for event in self.events]

    async def test_a_session_replaced_by_a_load_sends_none(self):
        """Fails on 91c4ed1: the notice and the subagent notification were still sent."""
        self.assertEqual(await self.sent_while_ending(), [])  # session/load popped it before close()

    async def test_a_server_whose_client_is_gone_sends_none(self):
        """Fails on 91c4ed1: the notice and the subagent notification were still sent."""
        self.server.sessions[self.session.id] = self.session
        self.server.closing = True
        self.assertEqual(await self.sent_while_ending(), [])

    async def test_a_session_still_registered_sends_all_three(self):
        """Guard (passes on 91c4ed1): a close the client asked for still shows the worker's last frames."""
        self.server.sessions[self.session.id] = self.session
        self.assertEqual(await self.sent_while_ending(), ["_aelix/notice", "_aelix/subagent", "session/update"])


@unittest.skipIf(sys.platform == "win32", "a POSIX SIGTERM handler emits the worker's last frames")
class ShutdownTests(Base):
    """Fix round 1, Codex C-M3 and P-LOW-2: a child's cards close when its worker is ended."""

    capabilities = {"aelix/nestedToolCalls": True}

    async def worker(self, parent, mode):
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", WORKER, json.dumps(parent), mode, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True)
        self.addAsyncCleanup(self.reap, process)
        self.session.proc = process
        self.session.controls.capabilities = {"methods": ["children/list"]}
        self.server.sessions[self.session.id] = self.session
        self.session.tasks = {asyncio.create_task(self.session.read_rpc())}
        async with asyncio.timeout(10):
            while self.session.children.seqs.get((EPOCH, "sub-1")) != 2:  # its tool_start was read
                await asyncio.sleep(0.01)

    @staticmethod
    async def reap(process):
        if process.returncode is None:
            process.kill()
            await process.wait()

    def statuses(self, updates):
        return [(u["toolCallId"], u.get("status"), u["_meta"]["aelix/child"].get("end"),
                 u["_meta"]["aelix/child"].get("closedBy")) for u in updates if u.get("status") in ("failed", "completed")]

    async def test_the_workers_own_terminal_frames_at_close_reach_the_client_and_the_record(self):
        await self.worker(None, "frames")
        await asyncio.wait_for(self.server.dispatch("_aelix/manage", {"sessionId": "session",
                                                                      "action": "sessions/close"}), 10)
        expected = [("sub-1:c1", "failed", None, None), ("aelix-child:sub-1", "failed", "stopped", None)]
        self.assertEqual(self.statuses(self.updates()), expected)
        self.assertEqual(self.statuses(list(history(self.server.db, "session"))), expected)

    async def test_the_last_frames_are_read_after_the_worker_exited(self):
        """Its stdout is not drained away, and the reader gets time to finish once the process is gone."""
        await self.worker(None, "frames")
        send, busy = self.server.send, asyncio.Event()

        async def slow(event):
            busy.set()
            await asyncio.sleep(0.3)  # a slow client: the reader is busy when the close begins and when the worker exits
            await send(event)
        self.server.send = slow
        os.kill(self.session.proc.pid, signal.SIGUSR1)  # one more frame, so the reader is in event() at the close
        await asyncio.wait_for(busy.wait(), 5)
        await asyncio.wait_for(self.session.close(), 10)
        self.assertEqual(self.statuses(self.updates()),
                         [("sub-1:c1", "failed", None, None), ("aelix-child:sub-1", "failed", "stopped", None)])
        self.assertEqual(self.updates()[-1]["_meta"][META_KEY]["children"][0]["lastText"], "busy")

    async def test_a_top_level_card_a_worker_left_open_closes_failed(self):
        await self.worker(None, "silent")
        await asyncio.wait_for(self.session.close(), 10)
        self.assertEqual(self.statuses(self.updates()), [("sub-1:c1", "failed", "stopped", "adapter"),
                                                         ("aelix-child:sub-1", "failed", "stopped", "adapter")])
        self.assertEqual(self.session.child_calls, set())

    async def test_without_nesting_a_top_level_child_gets_nothing_and_a_parented_one_its_entry(self):
        self.server.client_capabilities = {"_meta": {}}
        await self.worker(None, "silent")
        await asyncio.wait_for(self.session.close(), 10)
        self.assertEqual((self.updates(), self.session.children.stale(EPOCH)), ([], True))
        self.session = Session(self.server, "session", self.tmp.name)
        await self.worker("call-1", "silent")
        await asyncio.wait_for(self.session.close(), 10)
        self.assertEqual([(u["toolCallId"], u.get("status"), u["_meta"]["aelix/child"]["closedBy"])
                          for u in self.updates() if "closedBy" in u["_meta"].get("aelix/child", {})],
                         [("call-1", None, "adapter")])

    async def test_the_cards_a_worker_left_open_are_closed_by_the_adapter(self):
        await self.worker("call-1", "silent")
        await asyncio.wait_for(self.session.close(), 10)
        self.assertEqual(self.statuses(self.updates()), [("sub-1:c1", "failed", "stopped", "adapter")])
        card = self.updates()[-1]
        self.assertEqual((card["toolCallId"], card.get("status"), card["_meta"]["aelix/child"]["closedBy"]),
                         ("call-1", None, "adapter"))
        entry = card["_meta"][META_KEY]["children"][0]
        self.assertEqual((entry["profile"], entry["state"], entry["end"]), ("reviewer", "stopped", "stopped"))
        self.assertEqual((self.session.children.parents, self.session.children.stale(EPOCH)), ({}, True))

    async def test_a_client_that_is_gone_or_was_told_of_a_replacement_gets_nothing_but_the_record_has_it(self):
        for gone in ("closing", "replaced"):
            with self.subTest(gone):
                self.events.clear()
                with self.server.db:
                    self.server.db.execute("DELETE FROM events")
                self.session = Session(self.server, "session", self.tmp.name)
                await self.worker(None, "frames")
                sent = len(self.events)
                if gone == "closing":
                    self.server.closing = True
                else:
                    self.server.sessions.pop("session")
                await asyncio.wait_for(self.session.close(), 10)
                self.server.closing = False
                self.assertEqual(len(self.events), sent)
                self.assertEqual(self.statuses(list(history(self.server.db, "session"))),
                                 [("sub-1:c1", "failed", None, None), ("aelix-child:sub-1", "failed", "stopped", None)])


class UiEnableTests(unittest.TestCase):
    def test_child_approvals_are_asked_for_only_from_a_children_capable_native(self):
        self.assertEqual(session_module.ui_enable_params(["ui/enable", "children/list"]),
                         {"enabled": True, "childApprovals": True})
        self.assertEqual(session_module.ui_enable_params(["ui/enable", "children/notices"]), {"enabled": True})


@unittest.skipIf(sys.platform == "win32", "POSIX process groups")
class CloseDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def slow_exit(self, after):
        code = ("import signal,sys,time\n"
                f"signal.signal(signal.SIGTERM, lambda *a: (time.sleep({after}), sys.exit(7)))\n"
                "print('ready', flush=True)\ntime.sleep(60)\n")
        process = await asyncio.create_subprocess_exec(sys.executable, "-c", code, stdout=asyncio.subprocess.PIPE,
                                                       start_new_session=True)
        await process.stdout.readline()
        return process

    async def test_the_grace_decides_whether_a_slow_exit_finishes(self):
        process = await self.slow_exit(1.0)
        await terminate_owned(process, grace=0.3)
        self.assertEqual(process.returncode, -signal.SIGKILL)
        process = await self.slow_exit(0.3)
        await terminate_owned(process, grace=5)
        self.assertEqual(process.returncode, 7)

    async def test_a_children_capable_worker_gets_the_long_deadline(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        server = Server(SimpleNamespace(state_dir=tmp.name, profile="coding"))
        self.addCleanup(server.db.close)
        graces = []

        async def terminate(process, *, grace=5):
            graces.append(grace)
            process.returncode = 0

        async def done():
            return b""
        for methods, expected in ((["children/list"], 45.0), (["children/notices"], 45.0), ([], 5)):
            session = Session(server, "s", tmp.name)
            session.controls.capabilities = {"methods": methods}
            session.proc = SimpleNamespace(returncode=None, stdout=SimpleNamespace(read=lambda n: done()),
                                           stderr=SimpleNamespace(read=lambda n: done()))
            with patch.object(session_module, "terminate_owned", terminate):
                await session.close()
            self.assertEqual(graces[-1], expected, methods)


if __name__ == "__main__":
    unittest.main()
