"""Exercise ACP control and shutdown with real inherited pipes."""
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

from aelix_acp.wire.transport import encode_frame
from test_lifecycle import FAKE_CHILD


FIXTURE = """
from pathlib import Path
import asyncio
import os

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

readline = StdinReader.readline
async def observe_eof(self):
    line = await readline(self)
    if not line:
        Path('stdin-eof').touch()
    return line
StdinReader.readline = observe_eof

class FixtureServer(entry.Server):
    async def dispatch(self, method, params):
        if method == 'fixture/large':
            return {'text': 'x' * 2**20}
        if method == 'fixture/wait':
            await asyncio.Future()
        if method == 'fixture/diagnostic':
            write_diagnostic('diagnostic' + 'x' * 2**20)
            return {}
        if method == 'fixture/permission':
            worker = Session(self, 'fixture', os.getcwd())
            worker.tools['tool'] = tool_call('tool', 'write', {}, worker.cwd, 'pending')
            worker.socket_server = await asyncio.start_server(worker.permission, '127.0.0.1', 0)
            self.sessions[worker.id] = worker
            return {'port': worker.socket_server.sockets[0].getsockname()[1]}
        result = await super().dispatch(method, params)
        if method == 'session/cancel':
            Path('cancel-received').touch()
        return result

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
        if request["message"] == "stream":
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
        result = queue.Queue(maxsize=1)
        threading.Thread(target=lambda: result.put(self.proc.stdout.readline()), daemon=True).start()
        try:
            return json.loads(result.get(timeout=10))
        except queue.Empty:
            self.fail("ACP stdout did not produce a complete frame")

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

    def start_native_prompt(self, text):
        native = self.root / "prompt_native.py"
        native.write_text(NATIVE_PROMPT, encoding="utf-8")
        self.start("--aelix", sys.executable, f"--aelix-arg={native}")
        self.send("session/new", 2, {"cwd": str(self.root), "mcpServers": []})
        opened = self.reply_until(lambda frame: frame.get("id") == 2)
        session = opened["result"]["sessionId"]
        self.send("session/prompt", 4, {"sessionId": session, "prompt": [{"type": "text", "text": text}]})
        return session

    def reply_until(self, match, *, seen=None):
        for _ in range(32):
            frame = self.reply()
            if match(frame):
                return frame
            if seen is not None:
                seen.append(frame)
        self.fail("ACP stdout did not produce the expected frame")

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
