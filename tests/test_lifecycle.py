"""Real OS-process checks without provider calls or user credentials."""
import asyncio
import json
import os
from support import process_alive
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from aelix_acp.server import Server, Session
from aelix_acp.runtime.store import save_preferences


FAKE_CHILD = '''import json,os,sys
from pathlib import Path
Path("child.pid").write_text(str(os.getpid()))
if not os.environ.get("TEST_SKIP_BRIDGE_READY"):
    Path(os.environ["AELIX_ACP_READY_FILE"]).write_text(os.environ["AELIX_ACP_NONCE"])
for line in sys.stdin:
    request=json.loads(line)
    data={"commands":[]} if request["type"]=="get_commands" else ({"version":1} if request["type"]=="aelix_control" else {})
    print(json.dumps({"type":"response","id":request["id"],"success":True,"data":data}),flush=True)
'''


class ProcessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.child = self.root / 'fake-aelix'
        self.child.write_text(f'#!{sys.executable}\n' + FAKE_CHILD)
        self.child.chmod(0o700)
        self.proc = None

    async def asyncTearDown(self):
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            await asyncio.wait_for(self.proc.wait(), 10)
        self.tmp.cleanup()

    async def start(self, skip_ready=False):
        source = str(Path(__file__).resolve().parents[1] / 'src')
        env = dict(os.environ, PYTHONPATH=source)
        if skip_ready:
            env['TEST_SKIP_BRIDGE_READY'] = '1'
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable, '-m', 'aelix_acp.server', '--aelix', sys.executable, '--aelix-arg', str(self.child),
            '--state-dir', str(self.root / 'state'), env=env, stdin=-1, stdout=-1, stderr=-1)

    async def request(self, request_id, method, params):
        if method != 'initialize' and not getattr(self, 'initialized', False):
            await self.request('init', 'initialize', {'protocolVersion':1, 'clientCapabilities':{}})
            self.initialized = True
        self.proc.stdin.write((json.dumps({'jsonrpc':'2.0','id':request_id,'method':method,'params':params})+'\n').encode())
        await self.proc.stdin.drain()
        while True:
            line = await asyncio.wait_for(self.proc.stdout.readline(), 10)
            self.assertTrue(line, 'Unexpected adapter EOF')
            message = json.loads(line)
            if message.get('id') == request_id:
                return message

    async def test_missing_approval_extension_refuses_session_and_stops_child(self):
        await self.start(skip_ready=True)
        result = await self.request(1, 'session/new', {'cwd':str(self.root),'mcpServers':[]})
        self.assertIn('approval bridge did not load', result['error']['message'])
        child_pid = int((self.root / 'child.pid').read_text())
        self.assertFalse(process_alive(child_pid))

    async def test_launch_preferences_are_read_after_session_lease_admission(self):
        self.child.write_text(FAKE_CHILD.replace('Path("child.pid")', 'Path("child.args").write_text(json.dumps(sys.argv))\nPath("child.pid")'))
        server = Server(SimpleNamespace(state_dir=str(self.root / "state"), profile="coding",
            aelix=sys.executable, aelix_arg=[str(self.child)], provider=None, model=None))
        session = Session(server, "stale", str(self.root))
        try:
            with server.db:
                save_preferences(server.db, "stale", {"trustProject": False, "agentsEnabled": True})
            await session.start()
            argv = json.loads((self.root / "child.args").read_text())
            self.assertIn("--deny", argv)
            self.assertIn("--agents", argv)
            self.assertNotIn("--no-agents", argv)
        finally:
            await session.close()
            server.db.close()

    async def test_sigterm_closes_real_rpc_child(self):
        await self.start()
        result = await self.request(1, 'session/new', {'cwd':str(self.root),'mcpServers':[]})
        self.assertIn('sessionId', result['result'])
        child_pid = int((self.root / 'child.pid').read_text())
        self.assertTrue(process_alive(child_pid))
        self.proc.terminate()
        await asyncio.wait_for(self.proc.wait(), 10)
        for _ in range(100):
            if not process_alive(child_pid):
                break
            await asyncio.sleep(0.05)
        self.assertFalse(process_alive(child_pid))

    async def test_unread_stdout_does_not_block_disconnect_shutdown(self):
        await self.start()
        await self.request('init', 'initialize', {'protocolVersion':1, 'clientCapabilities':{}})
        payload = {'jsonrpc':'2.0','id':1,'method':'x' * 1024 * 1024,'params':{}}
        self.proc.stdin.write((json.dumps(payload)+'\n').encode())
        await self.proc.stdin.drain()
        await asyncio.sleep(0.1)
        self.proc.stdin.close()
        async with asyncio.timeout(10):
            while self.proc.returncode is None:
                await asyncio.sleep(0.02)
        self.assertEqual(self.proc.returncode, 0)
        await self.proc.stdout.read()
        await self.proc.wait()

    async def test_disconnect_drains_a_chatty_child_before_waiting_for_exit(self):
        flood = '''    if request["type"] == "prompt":
        print(json.dumps({"type":"response","id":request["id"],"success":True,"data":{}}),flush=True)
        for _ in range(32):
            print(json.dumps({"type":"message_update","assistantMessageEvent":{"type":"text_delta","delta":"x"*1024*1024}}),flush=True)
            sys.stderr.write("x"*1024*1024); sys.stderr.flush()
        continue
'''
        self.child.write_text(FAKE_CHILD.replace('    data=', flood + '    data='))
        await self.start()
        result = await self.request(1, 'session/new', {'cwd':str(self.root),'mcpServers':[]})
        payload = {'jsonrpc':'2.0','id':2,'method':'session/prompt','params':{
            'sessionId':result['result']['sessionId'],'prompt':[{'type':'text','text':'Flood'}]}}
        self.proc.stdin.write((json.dumps(payload)+'\n').encode())
        await self.proc.stdin.drain()
        await asyncio.sleep(0.2)
        self.proc.stdin.close()
        async with asyncio.timeout(10):
            while self.proc.returncode is None:
                await asyncio.sleep(0.02)
        self.assertEqual(self.proc.returncode, 0)
        await asyncio.gather(self.proc.stdout.read(), self.proc.stderr.read())
        await self.proc.wait()
        self.assertFalse(process_alive(int((self.root/'child.pid').read_text())))


if __name__ == '__main__':
    unittest.main()
