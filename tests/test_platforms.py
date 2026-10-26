"""Real pipe framing and authenticated loopback regressions, on every host OS."""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from aelix_acp.stdio import StdinReader
from aelix_acp.server import Server, Session


class StdioTests(unittest.IsolatedAsyncioTestCase):
    async def test_protocol_stdout_is_utf8_and_eof_shuts_down_cleanly(self):
        source = str(Path(__file__).resolve().parents[1] / 'src')
        with tempfile.TemporaryDirectory() as state:
            process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'aelix_acp.server',
                '--state-dir', state, env=dict(os.environ, PYTHONPATH=source, PYTHONIOENCODING='cp1252'),
                stdin=-1, stdout=-1, stderr=-1)
            try:
                process.stdin.write((json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': '한글'}) + '\n').encode('utf-8'))
                await process.stdin.drain()
                answer = json.loads((await asyncio.wait_for(process.stdout.readline(), 5)).decode('utf-8'))
                self.assertIn('한글', answer['error']['message'])
                process.stdin.close()
                self.assertEqual(await asyncio.wait_for(process.wait(), 5), 0)
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()

    async def test_split_utf8_frame_and_final_partial_line_are_not_lost(self):
        read, write = os.pipe()
        reader = StdinReader(limit=64, fd=read)
        try:
            packet = '한글\n'.encode('utf-8')
            os.write(write, packet[:2])
            os.write(write, packet[2:] + b'tail')
            os.close(write)
            write = None
            self.assertEqual(await asyncio.wait_for(reader.readline(), 2), packet)
            self.assertEqual(await asyncio.wait_for(reader.readline(), 2), b'tail')
            self.assertEqual(await asyncio.wait_for(reader.readline(), 2), b'')
        finally:
            reader.close()
            if write is not None:
                os.close(write)
            await asyncio.to_thread(reader.thread.join, 1)
            os.close(read)

    async def test_oversized_line_fails_before_a_delimiter_arrives(self):
        read, write = os.pipe()
        reader = StdinReader(limit=32, fd=read)
        try:
            os.write(write, b'x' * 33)
            with self.assertRaisesRegex(ValueError, 'exceeds limit'):
                await asyncio.wait_for(reader.readline(), 2)
        finally:
            reader.close()
            os.close(write)
            await asyncio.to_thread(reader.thread.join, 1)
            os.close(read)

    async def test_open_stdin_does_not_prevent_clean_interpreter_exit(self):
        source = str(Path(__file__).resolve().parents[1] / 'src')
        code = ('import asyncio;from aelix_acp.stdio import StdinReader\n'
                'async def main():\n r=StdinReader();await r.readline();r.close()\n'
                'asyncio.run(main())\n')
        process = await asyncio.create_subprocess_exec(sys.executable, '-c', code,
            env=dict(os.environ, PYTHONPATH=source), stdin=-1, stdout=-1, stderr=-1)
        try:
            process.stdin.write(b'quit\n')
            await process.stdin.drain()
            self.assertEqual(await asyncio.wait_for(process.wait(), 5), 0)
            self.assertNotIn(b'_enter_buffered_busy', await process.stderr.read())
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()


class LoopbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = Server(SimpleNamespace(state_dir=self.temp.name, profile='coding'))
        self.session = Session(self.server, 'owned', self.temp.name)
        self.session.permission_token = 'generation-one-private-token'
        self.events = []
        async def capture(event): self.events.append(event)
        self.server.send = capture
        self.listener = await asyncio.start_server(self.session.permission, '127.0.0.1', 0, limit=65536)
        self.port = self.listener.sockets[0].getsockname()[1]
        self.session.permission_endpoint = f'tcp://127.0.0.1:{self.port}'

    async def asyncTearDown(self):
        self.listener.close()
        await self.listener.wait_closed()
        await self.session.close()
        self.server.db.close()
        self.temp.cleanup()

    async def request(self, token):
        reader, writer = await asyncio.open_connection('127.0.0.1', self.port)
        packet = dict(toolCallId='tool-one', name='write', args={'path': 'owned.txt'})
        if token is not None: packet['token'] = token
        writer.write((json.dumps(packet) + '\n').encode('utf-8'))
        await writer.drain()
        try:
            return json.loads(await asyncio.wait_for(reader.readline(), 2))
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    async def test_missing_or_wrong_token_cannot_reach_yolo_or_ui(self):
        self.session.permission_mode = 'yolo'
        self.assertEqual(await self.request(None), {'allow': False})
        self.assertEqual(await self.request('wrong'), {'allow': False})
        self.assertEqual(self.events, [])

    async def test_valid_token_reaches_yolo_without_exposing_token(self):
        self.session.permission_mode = 'yolo'
        token = self.session.permission_token
        self.assertEqual(await self.request(token), {'allow': True})
        self.assertEqual(len(self.events), 2)
        self.assertNotIn(token, json.dumps(self.events))

    async def test_stale_worker_token_and_cancelled_session_are_denied_before_ui(self):
        old = self.session.permission_token
        self.session.permission_token = 'generation-two-private-token'
        self.assertEqual(await self.request(old), {'allow': False})
        self.session.cancelled = True
        self.assertEqual(await self.request(self.session.permission_token), {'allow': False})
        self.assertEqual(self.events, [])

    async def test_unauthenticated_connections_are_bounded_before_more_consent(self):
        connections = [await asyncio.open_connection('127.0.0.1', self.port) for _ in range(8)]
        try:
            for _ in range(100):
                if len(self.session.permissions) == 8: break
                await asyncio.sleep(0.01)
            self.assertEqual(len(self.session.permissions), 8)
            self.assertEqual(await self.request(self.session.permission_token), {'allow': False})
            self.assertEqual(self.events, [])
        finally:
            for _reader, writer in connections:
                writer.close()
                with contextlib.suppress(ConnectionError):
                    await writer.wait_closed()
