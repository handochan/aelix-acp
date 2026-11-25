"""Exercise ACP control and shutdown with real inherited pipes."""
from collections import Counter
import json
import os
from pathlib import Path
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from aelix_acp.server import MAX_CANCEL_RESPONSES, MAX_CANCEL_RESPONSE_ID_BYTES
from aelix_acp.wire.transport import encode_frame
from test_lifecycle import FAKE_CHILD


FIXTURE = """
from pathlib import Path
import asyncio
import json
import os
import sys

import aelix_acp.server as entry
from aelix_acp.session import Session, tool_call
from aelix_acp.stdio import StdinReader, write_diagnostic

write = os.write
def observe_write(fd, data):
    if fd == 1 and len(data) > 2**19 and not Path('stdout-active').exists():
        written = write(fd, data[:1024])
        Path('stdout-active').touch()
        return written
    if fd == 2 and len(data) > 2**19:
        Path('stderr-active').touch()
    return write(fd, data)
os.write = observe_write

rpc = Session.rpc
async def hold_abort_wait(self, command, **kwargs):
    if command == "abort" and Path("extend-abort-wait").exists():
        kwargs["_timeout"] = 60
    return await rpc(self, command, **kwargs)
Session.rpc = hold_abort_wait

def snapshot(name):
    server = FixtureServer.instance
    metrics = {
        "tasks": len(asyncio.all_tasks()),
        "requests": len(server.tasks),
        "cancelTasks": len(getattr(server, "cancel_tasks", {})),
        "cancelResponses": len(getattr(server, "cancel_reply_ids", ())),
        "errorBytes": getattr(getattr(server, "error_replies", None), "bytes", 0),
        "errorFrames": getattr(getattr(server, "error_replies", None), "count", 0),
        "nativePending": sum(len(session.pending) for session in server.sessions.values()),
    }
    try:
        import resource
    except ImportError:
        pass  # Windows still asserts the task and native RPC counts.
    else:
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        metrics["rssBytes"] = rss if sys.platform == "darwin" else rss * 1024
    path = Path(name)
    pending = path.with_suffix(".tmp")
    pending.write_text(json.dumps(metrics), encoding="utf-8")
    pending.replace(path)

readline = StdinReader.readline
async def observe_eof(self):
    line = await readline(self)
    if b'"method": "fixture/prequeue"' in line:
        async with asyncio.timeout(5):
            while self.queue.qsize() < 2:
                await asyncio.sleep(0.001)
        Path("pair-queued").touch()
        line = await readline(self)
    if not line:
        Path('stdin-eof').touch()
    elif b'"method": "fixture/snapshot"' in line:
        snapshot(json.loads(line)["params"]["name"])
    return line
StdinReader.readline = observe_eof

class FixtureServer(entry.Server):
    def __init__(self, args):
        super().__init__(args)
        type(self).instance = self
        if Path("small-error-budget").exists():
            self.error_replies.byte_limit = 256
            wait = self.error_replies.space.wait
            async def observe_backpressure():
                snapshot("error-lane-full")
                await wait()
            self.error_replies.space.wait = observe_backpressure

    async def dispatch(self, method, params):
        if method == 'fixture/recover':
            worker = self.session(params)
            worker.compaction_recovery_needed = True
            start = worker.start
            async def paused_start(*args, **kwargs):
                Path("recovering").touch()
                async with asyncio.timeout(10):
                    while not Path("resume-recovery").exists():
                        await asyncio.sleep(0.01)
                return await start(*args, **kwargs)
            worker.start = paused_start
            return {}
        if method == 'fixture/snapshot':
            return {}
        if method == 'fixture/large':
            return {'text': 'x' * 2**20}
        if method == 'fixture/wait':
            await asyncio.Future()
        if method == 'fixture/diagnostic':
            write_diagnostic('diagnostic' + 'x' * 2**20)
            return {}
        if method == 'fixture/permission':
            worker = Session(self, 'fixture', os.getcwd())
            cancel = worker.cancel_native

            async def observe_cancel():
                await cancel()
                Path('cancel-received').touch()

            worker.cancel_native = observe_cancel
            worker.tools['tool'] = tool_call('tool', 'write', {}, worker.cwd, 'pending')
            worker.socket_server = await asyncio.start_server(worker.permission, '127.0.0.1', 0)
            self.sessions[worker.id] = worker
            return {'port': worker.socket_server.sockets[0].getsockname()[1]}
        return await super().dispatch(method, params)

entry.Server = FixtureServer
entry.main()
"""


NATIVE_PROMPT = FAKE_CHILD.replace(
    'for line in sys.stdin:',
    'METHODS.append("commands/cancel")\nstreaming = False\nfor line in sys.stdin:',
).replace(
    '    if request["type"]=="get_commands":',
    '''    if request["type"] == "prompt":
        streaming = True
        print(json.dumps({"type": "response", "id": request["id"], "success": True}), flush=True)
        if request["message"] == "finish":
            streaming = False
        elif request["message"] == "stream":
            print(json.dumps({"type": "message_update", "assistantMessageEvent": {
                "type": "text_delta", "delta": "x" * 2**20,
            }}), flush=True)
        else:
            print(json.dumps({"type": "extension_ui_request", "id": "child-question",
                "method": "childApproval", "child": {"id": "child", "epoch": "epoch"},
                "tool": {"callId": "call", "name": "write", "args": {}},
                "choices": [{"id": "yes", "kind": "allow_once"}],
            }), flush=True)
        continue
    if request["type"] == "extension_ui_response":
        answer = Path("native-approval-answer.tmp")
        answer.write_text(json.dumps(request), encoding="utf-8")
        answer.replace("native-approval-answer")
        continue
    if request["type"] == "abort":
        streaming = False
        Path("native-abort").touch()
        if Path("hold-first-abort-reply").exists():
            marker = Path("first-abort-reply-held")
            if not marker.exists():
                marker.touch()
                continue
            Path("native-second-abort").touch()
    if request.get("method") == "commands/cancel":
        Path("native-commands-cancel").touch()
    if request["type"] == "get_state":
        data = {"isStreaming": streaming}
    elif request["type"]=="get_commands":'''
)


class BackpressureTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.source = str(Path(__file__).resolve().parents[1] / "src")

    def start(self, *args):
        proc = subprocess.Popen(
            [sys.executable, "-c", FIXTURE, "--state-dir", str(self.root / "state"), *args],
            cwd=self.root, env=dict(os.environ, PYTHONPATH=self.source),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.addCleanup(self.stop, proc)
        self.proc = proc
        self.send("initialize", 1, {"protocolVersion": 1})
        self.assertIn("result", self.reply())

    @staticmethod
    def stop(proc):
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            pipe.close()

    def send(self, method, request_id=None, params=None):
        message = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if request_id is not None:
            message["id"] = request_id
        self.write(encode_frame(message))

    def write(self, raw):
        self.proc.stdin.write(raw)
        self.proc.stdin.flush()

    def reply(self):
        return self.reply_until(lambda frame: True, limit=1)

    def wait_for(self, name, timeout=10):
        deadline = time.monotonic() + timeout
        marker = self.root / name
        while not marker.exists() and self.proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(marker.exists(), f"Missing {name}, ACP exit {self.proc.poll()}")

    def large_reply(self):
        self.send("fixture/large", 3)
        self.wait_for("stdout-active")

    def fill_stderr(self):
        self.send("fixture/diagnostic", 2)
        self.assertEqual(self.reply()["id"], 2)
        self.wait_for("stderr-active")

    def test_permission_and_cancel_survive_validation_errors_under_stdout_backpressure(self):
        self.start()
        self.send("fixture/permission", 2)
        port = self.reply()["result"]["port"]
        with socket.create_connection(("127.0.0.1", port), timeout=5) as native, native.makefile("rb") as response:
            native.sendall(b'{"toolCallId":"tool","name":"write","args":{}}\n')
            self.assertEqual(self.reply()["params"]["update"]["status"], "pending")
            permission = self.reply()
            self.assertEqual(permission["method"], "session/request_permission")
            self.large_reply()
            self.write(b"{invalid JSON\n")
            self.send("fixture/wait", 3)  # Duplicate id.
            for n in range(70):
                self.send("fixture/wait", 100 + n)  # Request limit plus overflow of validation replies.
            self.write(encode_frame({"jsonrpc": "2.0", "id": permission["id"], "result": {
                "outcome": {"outcome": "selected", "optionId": "allow-once"},
            }}))
            self.assertEqual(json.loads(response.readline()), {"allow": True})
            self.send("session/cancel", params={"sessionId": "fixture"})
            self.wait_for("cancel-received")
            self.assertEqual(len(self.reply()["result"]["text"]), 2**20)
            self.assertEqual(self.reply()["error"]["code"], -32700)
            self.assertEqual(self.reply()["error"]["message"], "Duplicate active request id")
            self.assertEqual(self.reply()["error"]["message"], "Too many concurrent requests")
        self.proc.stdin.close()
        self.assertEqual(self.proc.wait(timeout=5), 0)

    def open_native(self):
        native = self.root / "prompt_native.py"
        native.write_text(NATIVE_PROMPT, encoding="utf-8")
        self.start("--aelix", sys.executable, f"--aelix-arg={native}")
        self.send("session/new", 2, {"cwd": str(self.root), "mcpServers": []})
        opened = self.reply_until(lambda frame: frame.get("id") == 2)
        return opened["result"]["sessionId"]

    def start_native_prompt(self, text):
        session = self.open_native()
        self.send("session/prompt", 4, {"sessionId": session, "prompt": [{"type": "text", "text": text}]})
        return session

    def reply_until(self, match, *, seen=None, limit=32, timeout=10):
        result = queue.Queue(maxsize=1)

        def read():
            try:
                for _ in range(limit):
                    line = self.proc.stdout.readline()
                    if not line:
                        raise EOFError("ACP stdout closed before the expected frame")
                    frame = json.loads(line)
                    if match(frame):
                        result.put(frame)
                        return
                    if seen is not None:
                        seen.append(frame)
                raise AssertionError("ACP stdout exceeded the expected frame count")
            except Exception as error:
                result.put(error)

        threading.Thread(target=read, daemon=True).start()
        try:
            answer = result.get(timeout=timeout)
        except queue.Empty:
            self.fail("ACP stdout did not produce the expected frame")
        if isinstance(answer, Exception):
            raise answer
        return answer

    def collect_responses(self, ids):
        remaining = Counter(ids)
        frames = []

        def complete(frame):
            key = frame.get("id")
            if key in remaining:
                remaining[key] -= 1
                if not remaining[key]:
                    del remaining[key]
            return not remaining

        frames.append(self.reply_until(complete, seen=frames, limit=len(ids) + 16, timeout=20))
        self.assertEqual(Counter(f["id"] for f in frames if "id" in f), Counter(ids))
        return {f["id"]: f for f in frames if "id" in f}, frames

    def test_cancel_sends_abort_while_a_native_prompt_update_fills_stdout(self):
        session = self.start_native_prompt("stream")
        self.wait_for("stdout-active")
        self.send("session/cancel", params={"sessionId": session})
        self.wait_for("native-abort", timeout=2)
        self.assertTrue((self.root / "native-commands-cancel").exists())
        update = self.reply_until(lambda frame: frame.get("params", {}).get("update", {}).get("sessionUpdate")
                                  == "agent_message_chunk")
        self.assertEqual(update["params"]["update"]["content"]["text"], "x" * 2**20)
        self.assertEqual(self.reply_until(lambda frame: frame.get("id") == 4)["result"], {"stopReason": "cancelled"})
        self.proc.stdin.close()
        self.assertEqual(self.proc.wait(timeout=10), 0)

    def test_child_approval_withdrawal_survives_a_full_validation_backlog(self):
        session = self.start_native_prompt("ask")
        permission = self.reply_until(lambda frame: frame.get("method") == "session/request_permission")
        self.large_reply()
        self.write(b"{invalid JSON\n" * 16)
        self.send("session/cancel", params={"sessionId": session})
        self.wait_for("native-approval-answer")
        native = json.loads((self.root / "native-approval-answer").read_text(encoding="utf-8"))
        self.assertEqual(native, {"type": "extension_ui_response", "id": "child-question", "cancelled": True})
        self.assertEqual(len(self.reply()["result"]["text"]), 2**20)
        self.send("session/list", 5)
        seen = []
        self.reply_until(lambda frame: frame.get("id") == 5, seen=seen)
        self.assertEqual([frame["error"]["code"] for frame in seen if "error" in frame], [-32700] * 16)
        self.assertIn({"jsonrpc": "2.0", "method": "$/cancel_request",
                       "params": {"requestId": permission["id"]}}, seen)
        self.proc.stdin.close()
        self.assertEqual(self.proc.wait(timeout=10), 0)

    def snapshot(self, name):
        self.send("fixture/snapshot", params={"name": name})
        self.wait_for(name)
        return json.loads((self.root / name).read_text(encoding="utf-8"))

    def exercise_cancel_flood(self, count, *, with_ids):
        (self.root / "extend-abort-wait").touch()
        session = self.start_native_prompt("stream")
        self.wait_for("stdout-active")
        before = self.snapshot("before-flood")
        params = {"sessionId": session}
        if with_ids:
            params["padding"] = "x" * 4096
        for index in range(count):
            self.send("session/cancel", 1000 + index if with_ids else None, params)
        after = self.snapshot("after-flood")
        self.wait_for("native-abort", timeout=2)
        self.assertLessEqual(after["tasks"] - before["tasks"], 2, (before, after))
        self.assertLessEqual(after["nativePending"] - before["nativePending"], 1, (before, after))
        self.assertEqual(after["cancelTasks"], 1)
        reserved = min(count, MAX_CANCEL_RESPONSES) if with_ids else 0
        self.assertEqual(after["cancelResponses"], reserved)
        self.assertEqual(after["errorFrames"], count - reserved if with_ids else 0)
        self.assertLessEqual(after["errorBytes"], 512 * 1024)
        if "rssBytes" in after:
            # The old ID flood retained about 23–33 MiB. Allow allocator noise.
            self.assertLess(after["rssBytes"] - before["rssBytes"], 12 * 1024**2, (before, after))
        requests = (self.root / "child.requests").read_text(encoding="utf-8").splitlines()
        self.assertEqual(requests.count("abort None"), 1)
        self.assertEqual(requests.count("aelix_control commands/cancel"), 1)

        expected = list(range(1000, 1000 + count)) if with_ids else []
        answered, _ = self.collect_responses([4, *expected])
        self.assertEqual(answered[4]["result"], {"stopReason": "cancelled"})
        for index, request_id in enumerate(expected):
            if index < MAX_CANCEL_RESPONSES:
                self.assertEqual(answered[request_id]["result"], {})
            else:
                self.assertEqual(answered[request_id]["error"]["code"], -32000)
        self.proc.stdin.close()
        self.assertEqual(self.proc.wait(timeout=10), 0)
        self.assertEqual(self.proc.stdout.read(), b"")

    def test_cancel_notification_flood_is_coalesced_under_backpressure(self):
        self.exercise_cancel_flood(1536, with_ids=False)

    def test_ided_cancel_flood_bounds_replies_tasks_and_memory(self):
        self.exercise_cancel_flood(4096, with_ids=True)

    def test_65_stops_get_65_correlated_responses(self):
        self.exercise_cancel_flood(MAX_CANCEL_RESPONSES + 1, with_ids=True)

    def test_large_id_producer_resumes_after_error_backpressure(self):
        (self.root / "small-error-budget").touch()
        session = self.start_native_prompt("stream")
        self.wait_for("stdout-active")
        ids = [f"{i}-" + "x" * 8192 for i in range(160)]
        sent = queue.Queue(maxsize=1)

        def produce():
            try:
                for key in ids:
                    self.send("session/cancel", key, {"sessionId": session})
            except Exception as error:
                sent.put(error)
            else:
                sent.put(None)

        producer = threading.Thread(target=produce, daemon=True)
        producer.start()
        try:
            self.wait_for("native-abort", timeout=2)
            self.wait_for("error-lane-full")
            peak = json.loads((self.root / "error-lane-full").read_text(encoding="utf-8"))
            self.assertEqual(peak["errorFrames"], 1)
            self.assertLess(peak["errorBytes"], 9 * 1024)
            self.assertTrue(producer.is_alive(), "Unread stdout must eventually backpressure stdin")
            answered, _ = self.collect_responses([4, *ids])
            self.assertEqual(answered[4]["result"], {"stopReason": "cancelled"})
            self.assertTrue(all(answered[key]["error"]["code"] == -32000 for key in ids))
            self.assertIsNone(sent.get(timeout=5))
        except BaseException:
            self.proc.kill()
            raise
        finally:
            producer.join(timeout=5)
        self.proc.stdin.close()
        self.assertEqual(self.proc.wait(timeout=10), 0)
        self.assertEqual(self.proc.stdout.read(), b"")

    def test_all_addressable_validation_errors_echo_the_id(self):
        self.start()
        self.large_reply()
        # A second ordinary request and a Stop both reuse the active large frame's ID.
        self.send("unknown", 3)
        self.send("session/cancel", 3, {"sessionId": "unknown"})
        self.write(encode_frame({"jsonrpc": "2.0", "id": "bad-params", "method": "session/cancel", "params": []}))
        for key in range(100, 163):
            self.send("fixture/wait", key)
        excess = list(range(1000, 1040))
        for key in excess:
            self.send("session/list", key)
        self.assertEqual(self.snapshot("addressed-errors-queued")["requests"], 64)
        answered, frames = self.collect_responses([3, 3, 3, "bad-params", *excess])
        duplicate = [frame for frame in frames if frame.get("id") == 3]
        self.assertEqual(sum("result" in frame for frame in duplicate), 1)
        self.assertEqual([frame["error"]["code"] for frame in duplicate if "error" in frame], [-32600] * 2)
        self.assertEqual(answered["bad-params"]["error"]["code"], -32602)
        self.assertTrue(all(answered[key]["error"]["code"] == -32000 for key in excess))
        self.proc.stdin.close()
        self.assertEqual(self.proc.wait(timeout=10), 0)

    def test_eof_interrupts_a_full_error_lane(self):
        (self.root / "small-error-budget").touch()
        self.start()
        self.large_reply()
        for key in ("a" * 8192, "b" * 8192):
            self.send("session/cancel", key, {"sessionId": "missing"})
        self.wait_for("error-lane-full")
        self.proc.stdin.close()
        self.assertEqual(self.proc.wait(timeout=5), 2)
        self.assertIn(b"ACP stdout drain timed out", self.proc.stderr.read())

    def test_first_stop_at_both_limits_preserves_every_reply_and_withdrawal(self):
        (self.root / "small-error-budget").touch()
        session = self.start_native_prompt("ask")
        permission = self.reply_until(lambda frame: frame.get("method") == "session/request_permission")
        self.large_reply()
        for request_id in range(100, 162):  # Prompt + large frame + 62 held requests.
            self.send("fixture/wait", request_id)
        expected = list(range(1000, 1000 + MAX_CANCEL_RESPONSES))
        for request_id in expected:
            self.send("session/cancel", request_id, {"sessionId": "missing"})
        saturated = self.snapshot("saturated")
        self.assertEqual(saturated["requests"], 64)
        self.assertEqual(saturated["cancelResponses"], MAX_CANCEL_RESPONSES)

        # One oversized error occupies the empty error lane. The first Stop
        # for the live session must take effect before waiting to queue its error.
        oversized = "x" * 8192
        self.send("session/cancel", oversized, {"sessionId": "missing"})
        self.send("session/cancel", 8000, {"sessionId": session})
        self.wait_for("error-lane-full")
        self.wait_for("native-abort", timeout=2)
        self.wait_for("native-approval-answer")
        denial = json.loads((self.root / "native-approval-answer").read_text(encoding="utf-8"))
        self.assertTrue(denial["cancelled"])
        answered, frames = self.collect_responses([3, 4, *expected, oversized, 8000])
        self.assertEqual(answered[4]["result"], {"stopReason": "cancelled"})
        self.assertEqual(len(answered[3]["result"]["text"]), 2**20)
        self.assertTrue(all(answered[key]["error"]["code"] == -32602 for key in expected))
        self.assertEqual(answered[8000]["error"]["code"], -32000)
        self.assertEqual(answered[oversized]["error"]["code"], -32000)
        withdrawal = {"jsonrpc": "2.0", "method": "$/cancel_request", "params": {"requestId": permission["id"]}}
        if withdrawal not in frames:
            self.assertEqual(self.reply(), withdrawal)
        self.send("session/cancel", 8001, {"sessionId": session})
        self.assertEqual(self.reply()["result"], {})

        (self.root / "native-abort").unlink()
        self.send("session/cancel", oversized, {"sessionId": session})
        self.wait_for("native-abort", timeout=2)
        self.assertEqual(self.reply()["id"], oversized)
        self.proc.stdin.close()
        self.assertEqual(self.proc.wait(timeout=10), 0)

    def check_prequeued_order(self, *, stop_first=False, identified=True):
        session = self.open_native()
        prompt = {"jsonrpc": "2.0", "id": 4, "method": "session/prompt", "params": {
            "sessionId": session, "prompt": [{"type": "text", "text": "finish"}]}}
        stop = {"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": session}}
        if identified:
            stop["id"] = 5
        pair = (stop, prompt) if stop_first else (prompt, stop)
        self.write(encode_frame({"jsonrpc": "2.0", "method": "fixture/prequeue"})
                   + b"".join(map(encode_frame, pair)))
        self.wait_for("pair-queued")
        answered, _ = self.collect_responses([4, 5] if identified else [4])
        self.assertEqual(answered[4]["result"]["stopReason"], "end_turn" if stop_first else "cancelled")
        if identified:
            self.assertEqual(answered[5]["result"], {})
        self.wait_for("native-abort", timeout=2)
        requests = (self.root / "child.requests").read_text(encoding="utf-8").splitlines()
        self.assertEqual(requests.count("prompt None"), int(stop_first))

    def test_prequeued_prompt_then_ided_stop(self):
        self.check_prequeued_order()

    def test_prequeued_prompt_then_stop_notification(self):
        self.check_prequeued_order(identified=False)

    def test_prequeued_stop_then_prompt_starts_new_work(self):
        self.check_prequeued_order(stop_first=True)

    def test_stop_during_prompt_recovery_survives_worker_replacement(self):
        session = self.open_native()
        self.send("fixture/recover", 3, {"sessionId": session})
        self.reply_until(lambda frame: frame.get("id") == 3)
        self.send("session/prompt", 4, {"sessionId": session, "prompt": [{"type": "text", "text": "finish"}]})
        self.wait_for("recovering")
        self.send("session/cancel", 5, {"sessionId": session})
        self.assertEqual(self.reply_until(lambda frame: frame.get("id") == 5)["result"], {})
        (self.root / "resume-recovery").touch()
        self.assertEqual(self.reply_until(lambda frame: frame.get("id") == 4)["result"], {"stopReason": "cancelled"})
        self.assertNotIn("prompt None", (self.root / "child.requests").read_text(encoding="utf-8").splitlines())

    def test_first_stop_for_a_new_prompt_replaces_an_old_native_wait(self):
        (self.root / "hold-first-abort-reply").touch()
        (self.root / "extend-abort-wait").touch()
        session = self.start_native_prompt("stream")
        self.wait_for("stdout-active")
        self.send("session/cancel", 5, {"sessionId": session})
        self.wait_for("first-abort-reply-held")
        self.assertEqual(self.reply_until(lambda frame: frame.get("id") == 4)["result"], {"stopReason": "cancelled"})

        (self.root / "stdout-active").unlink()
        self.send("session/prompt", 6, {"sessionId": session, "prompt": [{"type": "text", "text": "stream"}]})
        self.wait_for("stdout-active")
        self.send("session/cancel", 7, {"sessionId": session})
        self.wait_for("native-second-abort", timeout=2)
        seen = []
        result = self.reply_until(lambda frame: frame.get("id") == 6, seen=seen)
        self.assertEqual(result["result"], {"stopReason": "cancelled"})
        replies = {frame.get("id"): frame for frame in seen}
        while not {5, 7} <= replies.keys():
            frame = self.reply()
            replies[frame.get("id")] = frame
        self.assertEqual(replies[5]["error"]["code"], -32000)
        self.assertEqual(replies[7]["result"], {})
        self.proc.stdin.close()
        self.assertEqual(self.proc.wait(timeout=10), 0)

    def test_eof_drains_a_started_frame_without_starting_the_next(self):
        self.start()
        self.large_reply()
        self.send("unknown", 4)
        self.proc.stdin.close()
        self.wait_for("stdin-eof")
        self.assertEqual(self.reply(), {"jsonrpc": "2.0", "id": 3, "result": {"text": "x" * 2**20}})
        self.assertEqual(self.proc.wait(timeout=5), 0)
        self.assertEqual(self.proc.stdout.read(), b"")

    def test_eof_exits_with_an_error_when_the_reader_never_drains(self):
        self.start()
        self.large_reply()
        self.proc.stdin.close()
        self.assertEqual(self.proc.wait(timeout=5), 2)
        self.assertIn(b"ACP stdout drain timed out", self.proc.stderr.read())
        prefix = self.proc.stdout.read()
        self.assertTrue(prefix.startswith(b'{"jsonrpc": "2.0", "id": 3,'))
        self.assertNotIn(b"\n", prefix)

    def test_broken_stdout_shuts_down_even_with_stdin_open_and_stderr_full(self):
        self.start()
        self.fill_stderr()
        self.large_reply()
        self.proc.stdout.close()
        self.assertEqual(self.proc.wait(timeout=5), 0)

    def test_native_stderr_flood_does_not_wait_on_inherited_stderr(self):
        native = self.root / "native.py"
        native.write_text(FAKE_CHILD.replace(
            'for line in sys.stdin:',
            'sys.stderr.buffer.write(b"x" * (1024 * 1024))\n'
            'sys.stderr.flush()\n'
            'Path("native-stderr-finished").touch()\n'
            'for line in sys.stdin:',
        ), encoding="utf-8")
        self.start("--aelix", sys.executable, f"--aelix-arg={native}")
        # On Windows the first diagnostic is inside a blocking CRT write
        # before native forwarding starts. Startup must still dispatch RPCs.
        self.fill_stderr()
        self.send("session/new", 3, {"cwd": str(self.root), "mcpServers": []})
        while (reply := self.reply()).get("id") != 3:
            self.assertNotIn("error", reply)
        self.assertIn("result", reply, reply)
        self.wait_for("native-stderr-finished")
        self.send("session/list", 4)
        while (reply := self.reply()).get("id") != 4:
            self.assertNotIn("error", reply)
        self.assertIn("result", reply, reply)
        self.proc.stdin.close()
        self.assertEqual(self.proc.wait(timeout=10), 0)
