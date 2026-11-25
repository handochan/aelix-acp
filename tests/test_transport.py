"""Backpressure on inherited ACP pipes."""
import asyncio
from contextlib import ExitStack
import errno
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from aelix_acp.stdio import write_all
from aelix_acp.wire.transport import StdoutWriter, encode_frame
from test_lifecycle import FAKE_CHILD


def set_pipe_nonblocking(fd):
    if os.name != "nt":
        os.set_blocking(fd, False)
        return
    # Windows Python 3.11 cannot set pipe mode through os.set_blocking.
    import ctypes
    from ctypes import wintypes
    import msvcrt
    set_mode = ctypes.WinDLL("kernel32", use_last_error=True).SetNamedPipeHandleState
    set_mode.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p, ctypes.c_void_p)
    set_mode.restype = wintypes.BOOL
    nowait = wintypes.DWORD(1)  # PIPE_NOWAIT
    if not set_mode(msvcrt.get_osfhandle(fd), ctypes.byref(nowait), None, None):
        raise ctypes.WinError(ctypes.get_last_error())


class NonBlockingWriterTests(unittest.IsolatedAsyncioTestCase):
    def observe_backpressure(self, writer):
        write = os.write
        backpressure = asyncio.Event()
        errnos = set()

        def observe(fd, data):
            try:
                return write(fd, data)
            except OSError as error:
                if fd == writer.fd and error.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.ENOSPC):
                    writer.loop.call_soon_threadsafe(errnos.add, error.errno)
                    writer.loop.call_soon_threadsafe(backpressure.set)
                raise

        return backpressure, errnos, patch("aelix_acp.stdio.os.write", new=observe)

    async def test_large_frames_are_complete_and_ordered_with_a_slow_reader(self):
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        self.addCleanup(os.close, write_fd)
        set_pipe_nonblocking(write_fd)
        writer = StdoutWriter(write_fd)
        self.addCleanup(writer.close)
        large = {"jsonrpc": "2.0", "id": 1, "result": {"text": "한글" * (1024 * 1024 // 6)}}
        following = {"jsonrpc": "2.0", "id": 2, "error": {"code": -32601, "message": "unknown method"}}
        first, second = map(encode_frame, (large, following))
        backpressure, errnos, observing = self.observe_backpressure(writer)

        async def read_slowly():
            received = bytearray()
            while len(received) < len(first) + len(second):
                chunk = await asyncio.to_thread(os.read, read_fd, 4096)
                if not chunk:
                    raise EOFError("stdout closed before the queued frames completed")
                received.extend(chunk)
                await asyncio.sleep(0.001)
            return bytes(received)

        with observing:
            write_first = asyncio.create_task(writer.write(first))
            write_second = asyncio.create_task(writer.write(second))
            reading = None
            try:
                await asyncio.wait_for(backpressure.wait(), 5)
                self.assertFalse(write_first.done())
                self.assertFalse(write_second.done())
                reading = asyncio.create_task(read_slowly())
                received, _, _ = await asyncio.wait_for(
                    asyncio.gather(reading, write_first, write_second), 10)
            finally:
                tasks = (write_first, write_second) if reading is None else (write_first, write_second, reading)
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        if os.name != "nt":
            self.assertFalse(os.get_blocking(write_fd))
        elif sys.version_info[:2] == (3, 11):
            self.assertIn(errno.ENOSPC, errnos)
        self.assertEqual(received, first + second)
        self.assertEqual([json.loads(line) for line in received.splitlines()], [large, following])

    async def test_closed_pipe_wakes_writer_waiting_on_backpressure(self):
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, write_fd)
        set_pipe_nonblocking(write_fd)
        writer = StdoutWriter(write_fd)
        self.addCleanup(writer.close)
        backpressure, _, observing = self.observe_backpressure(writer)
        with observing:
            pending = asyncio.create_task(writer.write(b"x" * (1024 * 1024)))
            try:
                await asyncio.wait_for(backpressure.wait(), 5)
            finally:
                os.close(read_fd)
            with self.assertRaises(BrokenPipeError) as raised:
                await asyncio.wait_for(pending, 5)
        self.assertEqual(raised.exception.errno, errno.EPIPE)

    async def test_fatal_error_after_partial_write_prevents_later_frames(self):
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, write_fd)
        self.addCleanup(os.close, read_fd)
        writer = StdoutWriter(write_fd)
        self.addCleanup(writer.close)
        write = os.write
        attempts = 0

        def fail_after_prefix(fd, data):
            nonlocal attempts
            if fd == write_fd:
                attempts += 1
                if attempts == 1:
                    return write(fd, data[:3])
                if attempts == 2:
                    raise OSError(errno.EIO, "injected output failure")
            return write(fd, data)

        with patch("aelix_acp.stdio.os.write", new=fail_after_prefix):
            for frame in (b"first frame\n", b"error frame\n"):
                with self.assertRaises(OSError) as raised:
                    await asyncio.wait_for(writer.write(frame), 5)
                self.assertEqual(raised.exception.errno, errno.EIO)
        self.assertEqual(os.read(read_fd, 4096), b"fir")
        self.assertEqual(attempts, 2)

    def test_regular_file_errors_remain_permanent(self):
        with tempfile.TemporaryFile() as output:
            for code in (errno.ENOSPC, errno.EINVAL):
                with self.subTest(code=code):
                    failure = OSError(code, "injected file write error")
                    with patch("aelix_acp.stdio.os.write", side_effect=failure) as write:
                        with self.assertRaises(OSError) as raised:
                            write_all(output.fileno(), b"frame")
                        self.assertIs(raised.exception, failure)
                        write.assert_called_once()


@unittest.skipUnless(os.name == "posix", "Requires POSIX non-blocking pipes")
class StderrTests(unittest.TestCase):
    def test_full_inherited_stderr_does_not_stall_stdout_or_exit(self):
        with tempfile.TemporaryDirectory() as root, ExitStack() as cleanup:
            read_fd, write_fd = os.pipe()
            cleanup.callback(os.close, read_fd)
            cleanup.callback(os.close, write_fd)
            os.set_blocking(write_fd, False)
            with self.assertRaises(BlockingIOError):
                while True:
                    os.write(write_fd, b"x" * 4096)
            with self.assertRaises(BlockingIOError):
                while True:
                    os.write(write_fd, b"x")

            source = str(Path(__file__).resolve().parents[1] / "src")
            with subprocess.Popen(
                [sys.executable, "-m", "aelix_acp.server", "--state-dir", root],
                env=dict(os.environ, PYTHONPATH=source),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=write_fd,
            ) as proc:
                def send(*messages):
                    proc.stdin.write(b"".join(map(encode_frame, messages)))
                    proc.stdin.flush()

                def reply():
                    self.assertTrue(select.select([proc.stdout], [], [], 5)[0], "ACP stdout stalled")
                    return json.loads(proc.stdout.readline())

                try:
                    send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}})
                    self.assertIn("result", reply())
                    send({"jsonrpc": "2.0", "method": "unknown_notification"},
                         {"jsonrpc": "2.0", "id": 2, "method": "unknown_request"})
                    response = reply()
                    self.assertEqual(response["id"], 2)
                    self.assertEqual(response["error"]["code"], -32601)
                    proc.stdin.close()
                    self.assertEqual(proc.wait(timeout=5), 0)
                    self.assertFalse(os.get_blocking(write_fd))
                finally:
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait(timeout=5)


NODE = shutil.which("node")
NODE_LAUNCHER = """
const { spawn } = require('node:child_process');
const [python, ...args] = process.argv.slice(1);
const child = spawn(python, args, { stdio: ['pipe', 'inherit', 'inherit'] });
process.stdin.pipe(child.stdin);
child.stdin.on('error', () => {});
child.on('exit', code => process.exit(code ?? 1));
process.on('SIGTERM', () => child.kill('SIGTERM'));
"""
OBSERVE_BACKPRESSURE = """
import os
from pathlib import Path
from aelix_acp.server import main

assert not os.get_blocking(1), 'Node must pass non-blocking stdout to ACP'
write = os.write
def observe_write(fd, data):
    try:
        return write(fd, data)
    except BlockingIOError:
        if fd == 1:
            Path('stdout-backpressure').touch()
        raise
os.write = observe_write
main()
assert not os.get_blocking(1), 'ACP must preserve the inherited fd flags'
"""


@unittest.skipUnless(os.name == "posix" and NODE, "Requires Node and POSIX pipes")
class NodeLauncherTests(unittest.TestCase):
    def test_session_new_catalog_larger_than_pipe_buffer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            child = root / "fake_native.py"
            child.write_text(FAKE_CHILD.replace(
                '    if request["type"]=="get_commands":',
                '    models = [{"provider": "provider", "id": f"model-{i:03}", "name": f"Model {i:03}"}'
                ' for i in range(580)]\n'
                '    if request["type"]=="get_state":\n'
                '        data={"model": models[0]}\n'
                '    elif request["type"]=="get_available_models":\n'
                '        data={"models": models}\n'
                '    elif request["type"]=="get_commands":',
            ), encoding="utf-8")
            source = str(Path(__file__).resolve().parents[1] / "src")
            with (root / "stderr").open("w+b") as stderr, subprocess.Popen(
                [NODE, "-e", NODE_LAUNCHER, sys.executable, "-c", OBSERVE_BACKPRESSURE,
                 "--aelix", sys.executable, f"--aelix-arg={child}", "--state-dir", str(root / "state")],
                cwd=root, env=dict(os.environ, PYTHONPATH=source),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
            ) as proc:
                def request(request_id, method, params):
                    proc.stdin.write(encode_frame({
                        "jsonrpc": "2.0", "id": request_id, "method": method, "params": params}))
                    proc.stdin.flush()

                output = bytearray()

                def response(request_id):
                    deadline = time.monotonic() + 10
                    while select.select([proc.stdout], [], [], max(0, deadline - time.monotonic()))[0]:
                        chunk = os.read(proc.stdout.fileno(), 4096)
                        if not chunk:
                            break
                        output.extend(chunk)
                        while b"\n" in output:
                            line, _, remaining = output.partition(b"\n")
                            output[:] = remaining
                            message = json.loads(line)
                            if message.get("id") == request_id:
                                return message, len(line)
                        if time.monotonic() >= deadline:
                            break
                    stderr.seek(0)
                    self.fail(f"No response for {request_id}: {stderr.read().decode(errors='replace')}")

                try:
                    request(1, "initialize", {"protocolVersion": 1})
                    self.assertIn("result", response(1)[0])
                    request(2, "session/new", {"cwd": str(root), "mcpServers": []})
                    deadline = time.monotonic() + 10
                    while not (root / "stdout-backpressure").exists() and proc.poll() is None:
                        if time.monotonic() >= deadline:
                            self.fail("ACP did not hit backpressure with stdout undrained")
                        time.sleep(0.01)
                    opened, size = response(2)
                    self.assertGreater(size, 64 * 1024)
                    self.assertNotIn("error", opened)
                    model = next(option for option in opened["result"]["configOptions"] if option["id"] == "model")
                    self.assertEqual([item["value"] for item in model["options"]],
                                     [f"provider/model-{i:03}" for i in range(580)])
                    request(3, "unknown_method", {})
                    self.assertEqual(response(3)[0]["error"]["code"], -32601)
                    proc.stdin.close()
                    self.assertEqual(proc.wait(timeout=10), 0)
                finally:
                    if proc.poll() is None:
                        proc.terminate()
                        try:
                            proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            proc.wait(timeout=5)
