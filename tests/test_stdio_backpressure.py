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

    def wait_for(self, name):
        deadline = time.monotonic() + 10
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
