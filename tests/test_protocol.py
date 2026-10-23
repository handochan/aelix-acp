import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from aelix_acp.wire.errors import ProtocolError
from aelix_acp.wire.transport import decode_frame
from aelix_acp.runtime.store import open_store, begin_turn, finish_turn, append_event, history


class FramingTests(unittest.TestCase):
    def test_unicode_line_separators_are_message_content(self):
        frame = {"jsonrpc": "2.0", "method": "test", "params": {"text": "한글\u2028one\u2029two"}}
        self.assertEqual(decode_frame(json.dumps(frame, ensure_ascii=False).encode()), frame)

    def test_bad_envelopes_are_rejected(self):
        for value in [[], None, 1, {}, {"jsonrpc": "1.0"}, {"jsonrpc": "2.0", "id": []},
                      {"jsonrpc": "2.0", "id": True, "method": "test"},
                      {"jsonrpc": "2.0", "id": None, "method": "test"},
                      {"jsonrpc": "2.0", "method": 1}, {"jsonrpc": "2.0", "method": "test", "params": []}]:
            with self.subTest(value=value), self.assertRaises(ProtocolError):
                decode_frame(json.dumps(value).encode())

    def test_invalid_json_and_nonfinite_numbers_are_parse_errors(self):
        for raw in [b'\xff', b'{', b'{"jsonrpc":"2.0","method":"x","params":{"n":NaN}}',
                    b'{"jsonrpc":"2.0","method":"x","params":{"n":1e999}}',
                    b'{"jsonrpc":"2.0","method":"x","params":{"n":"\\ud800"}}']:
            with self.subTest(raw=raw), self.assertRaises(ProtocolError) as caught:
                decode_frame(raw)
            self.assertEqual(caught.exception.code, -32700)

    def test_response_must_have_exactly_one_result_or_error(self):
        for frame in [{"id": "x"}, {"id": "x", "result": {}, "error": {}}]:
            with self.assertRaises(ProtocolError):
                decode_frame(json.dumps({"jsonrpc": "2.0", **frame}).encode())


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = open_store(self.temp.name)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_completed_request_is_not_executed_again(self):
        self.assertIsNone(begin_turn(self.db, "s", "request"))
        finish_turn(self.db, "s", "request", {"stopReason": "end_turn"})
        self.assertEqual(begin_turn(self.db, "s", "request"), {"stopReason": "end_turn"})

    def test_interrupted_request_is_not_blindly_retried(self):
        begin_turn(self.db, "s", "request")
        finish_turn(self.db, "s", "request", None)
        with self.assertRaisesRegex(ProtocolError, "unresolved"):
            begin_turn(self.db, "s", "request")

    def test_another_adapter_open_does_not_relabel_a_live_turn(self):
        begin_turn(self.db, "s", "request")
        other = open_store(self.temp.name)
        try:
            self.assertEqual(other.execute("SELECT state FROM turns").fetchone()[0], "running")
        finally:
            other.close()

    def test_control_and_tool_history_replays_in_order(self):
        events = [{"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": "/help"}},
                  {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Help"}},
                  {"sessionUpdate": "config_option_update", "configOptions": []}]
        for event in events:
            append_event(self.db, "s", event)
        self.db.commit()
        self.assertEqual(list(history(self.db, "s")), events[:2])

    def test_streamed_events_do_not_hold_the_writer_lock_for_another_adapter(self):
        other = open_store(self.temp.name)
        other.execute("PRAGMA busy_timeout=50")
        try:
            event = {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "stream"}}
            append_event(self.db, "a", event)
            self.assertIsNone(begin_turn(other, "b", "other-request"))
            self.assertEqual(list(history(other, "a")), [event])
        finally:
            other.close()
