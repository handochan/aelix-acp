"""Delegated child progress becomes standard ACP updates on its parent tool call."""
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from aelix_acp.server import Server, Session
from aelix_acp.runtime.subagents import META_KEY, MAX_CHILDREN, SubagentTracker


def progress(child="sub-1", state="running", **extra):
    return {"id": child, "profile": "reviewer", "state": state, "current_tool": None, "elapsed_ms": 10,
            "tokens": 5, "cost": 0.0, "model": "one", "provider": "fixture", **extra}


class TrackerTests(unittest.TestCase):
    def test_child_belongs_to_the_open_agent_call_and_keeps_it_after_the_call_ends(self):
        tracker = SubagentTracker()
        tracker.tool_started("call-1", "agent")
        parent, meta = tracker.record("subagent_start", progress(state="starting"))
        self.assertEqual(parent, "call-1")
        self.assertEqual(meta[META_KEY]["children"][0]["state"], "starting")
        tracker.tool_ended("call-1")
        parent, meta = tracker.record("subagent_end", progress(state="done"))
        self.assertEqual(parent, "call-1")
        self.assertEqual([child["state"] for child in meta[META_KEY]["children"]], ["done"])

    def test_other_tools_do_not_adopt_children(self):
        tracker = SubagentTracker()
        tracker.tool_started("call-1", "read")
        self.assertEqual(tracker.record("subagent_start", progress()), (None, None))

    def test_a_resumed_child_moves_to_its_new_parent_call(self):
        tracker = SubagentTracker()
        tracker.tool_started("first", "agent")
        tracker.record("subagent_end", progress(state="done"))
        tracker.tool_ended("first")
        tracker.tool_started("second", "agent")
        parent, _meta = tracker.record("subagent_start", progress())
        self.assertEqual(parent, "second")

    def test_child_fields_are_bounded_and_malformed_events_are_ignored(self):
        tracker = SubagentTracker()
        tracker.tool_started("call-1", "agent")
        _parent, meta = tracker.record("subagent_tool", progress(current_tool="read\x1b[2J" + "x" * 1000,
                                                                  elapsed_ms=float("nan"), tokens=-4, cost="free"))
        child = meta[META_KEY]["children"][0]
        self.assertNotIn("\x1b", child["currentTool"])
        self.assertEqual(len(child["currentTool"]), 256)
        self.assertEqual((child["elapsedMs"], child["tokens"], child["cost"]), (0, 0, 0.0))
        self.assertEqual(tracker.record("subagent_start", {"profile": "no id"}), (None, None))
        self.assertEqual(tracker.record("unknown_event", progress()), (None, None))

    def test_children_per_call_are_capped(self):
        tracker = SubagentTracker()
        tracker.tool_started("call-1", "agent")
        for index in range(MAX_CHILDREN + 3):
            _parent, meta = tracker.record("subagent_start", progress(child=f"sub-{index}"))
        self.assertEqual(len(meta[META_KEY]["children"]), MAX_CHILDREN)


class SessionProgressTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = Server(SimpleNamespace(state_dir=self.temp.name, profile="coding"))
        self.events = []
        async def send(event):
            self.events.append(event)
        self.server.send = send
        self.session = Session(self.server, "session", self.temp.name)

    async def asyncTearDown(self):
        self.server.db.close()
        self.temp.cleanup()

    def updates(self):
        return [event["params"]["update"] for event in self.events if event["method"] == "session/update"]

    async def test_progress_rides_tool_call_updates_of_the_parent(self):
        await self.session.event({"type": "tool_execution_start", "toolCallId": "call-1", "toolName": "agent", "args": {}})
        await self.session.event({"type": "aelix_subagent", "event": "subagent_start", "data": progress(state="starting")})
        await self.session.event({"type": "tool_execution_update", "toolCallId": "call-1", "toolName": "agent",
                                  "partialResult": {"content": [{"type": "text", "text": "agent reviewer [running]"}]}})
        await self.session.event({"type": "aelix_subagent", "event": "subagent_end", "data": progress(state="done")})
        await self.session.event({"type": "tool_execution_end", "toolCallId": "call-1", "toolName": "agent",
                                  "result": {"content": [{"type": "text", "text": "CHILD_OK"}]}})
        await self.session.event({"type": "aelix_subagent", "event": "subagent_end", "data": progress(state="done")})
        updates = self.updates()
        self.assertEqual(updates[0]["sessionUpdate"], "tool_call")
        self.assertNotIn("_meta", updates[0])
        self.assertTrue(all(update["sessionUpdate"] == "tool_call_update" for update in updates[1:]))
        self.assertTrue(all(update["toolCallId"] == "call-1" and META_KEY in update["_meta"] for update in updates[1:]))
        self.assertEqual([update.get("status") for update in updates[1:]], ["in_progress", "in_progress", "in_progress", "completed", None])
        self.assertEqual(updates[2]["content"][0]["content"]["text"], "agent reviewer [running]")
        self.assertEqual(updates[4]["_meta"][META_KEY]["children"][0]["state"], "done")
        self.assertFalse([event for event in self.events if event["method"] == "_aelix/subagent"])

    async def test_a_child_without_a_parent_call_keeps_the_extension_notification(self):
        await self.session.event({"type": "aelix_subagent", "event": "subagent_start", "data": progress()})
        self.assertEqual([event["method"] for event in self.events], ["_aelix/subagent"])
        self.assertEqual(self.events[0]["params"]["event"], "subagent_start")


if __name__ == "__main__":
    unittest.main()
