"""Real OS-process checks without provider calls or user credentials."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest


FAKE_CHILD = '''import json,os,sys
from pathlib import Path
Path("child.pid").write_text(str(os.getpid()))
if not os.environ.get("TEST_SKIP_BRIDGE_READY"):
    Path(os.environ["AELIX_ACP_READY_FILE"]).write_text(os.environ["AELIX_ACP_NONCE"])
for line in sys.stdin:
    request=json.loads(line)
    data={"commands":[]} if request["type"]=="get_commands" else {}
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
            sys.executable, '-m', 'aelix_acp.server', '--aelix', str(self.child),
            '--state-dir', str(self.root / 'state'), env=env, stdin=-1, stdout=-1, stderr=-1)

    async def request(self, request_id, method, params):
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
        with self.assertRaises(ProcessLookupError):
            os.kill(child_pid, 0)

    async def test_sigterm_closes_real_rpc_child(self):
        await self.start()
        result = await self.request(1, 'session/new', {'cwd':str(self.root),'mcpServers':[]})
        self.assertIn('sessionId', result['result'])
        child_pid = int((self.root / 'child.pid').read_text())
        os.kill(child_pid, 0)
        self.proc.terminate()
        await asyncio.wait_for(self.proc.wait(), 10)
        with self.assertRaises(ProcessLookupError):
            os.kill(child_pid, 0)


if __name__ == '__main__':
    unittest.main()
