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


FAKE_CHILD = '''import json,os,sys,subprocess
from pathlib import Path
if os.environ.get("TEST_LOCKED"):
    sys.stderr.write("Error: this session is already open in another terminal.\\n  /fixture/session.jsonl\\n")
    sys.exit(1)
Path("child.pid").write_text(str(os.getpid()))
if os.environ.get("TEST_STARTUP_DESCENDANT"):
    descendant=subprocess.Popen([sys.executable,"-c","import time;time.sleep(120)"],stdin=-3,stdout=-3,stderr=-3)
    Path("descendant.pid").write_text(str(descendant.pid))
if not os.environ.get("TEST_SKIP_BRIDGE_READY"):
    Path(os.environ["AELIX_ACP_READY_FILE"]).write_text(os.environ["AELIX_ACP_NONCE"])
mode=sys.argv[sys.argv.index("--permission-mode")+1]
METHODS=["permissions/get","permissions/set","tools/set_active","agents/use","resources/list"]
for line in sys.stdin:
    request=json.loads(line)
    data={}
    if request["type"]=="get_commands":
        data={"commands":[]}
    elif request["type"]=="aelix_control":
        method=request["method"]
        if method=="capabilities":
            data={"version":1,"methods":METHODS,"permissionModes":["default","yolo"]}
        elif method=="permissions/set":
            mode=request["params"]["mode"]
            data={"mode":mode}
        elif method=="permissions/get":
            data={"mode":mode}
    print(json.dumps({"type":"response","id":request["id"],"success":True,"data":data}),flush=True)
'''


class ProcessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # A space and non-ASCII text in the launch path stay one native argument.
        self.child = self.root / 'fake aelix 한글.py'
        self.child.write_text(FAKE_CHILD, encoding='utf-8')
        self.child.chmod(0o700)
        self.proc = None

    async def asyncTearDown(self):
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            await asyncio.wait_for(self.proc.wait(), 10)
        self.tmp.cleanup()

    async def start(self, skip_ready=False, startup=False, delayed_job=False, locked=False):
        source = str(Path(__file__).resolve().parents[1] / 'src')
        env = dict(os.environ, PYTHONPATH=source)
        if skip_ready:
            env['TEST_SKIP_BRIDGE_READY'] = '1'
        if startup:
            env['TEST_STARTUP_DESCENDANT'] = '1'
        if locked:
            env['TEST_LOCKED'] = '1'
        command = ['-m', 'aelix_acp.server']
        if delayed_job:
            env['TEST_ASSIGNMENT_MARKER'] = str(self.root / 'descendant.pid')
            command = ['-c', 'import os,time;from pathlib import Path\n'
                       'from aelix_acp.windows import WorkerJob\n'
                       'original=WorkerJob.assign\n'
                       'def delayed(self,pid):\n'
                       ' deadline=time.monotonic()+2\n'
                       ' while time.monotonic()<deadline and not Path(os.environ["TEST_ASSIGNMENT_MARKER"]).exists(): time.sleep(0.02)\n'
                       ' return original(self,pid)\n'
                       'WorkerJob.assign=delayed\n'
                       'from aelix_acp.server import main;main()']
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable, *command, '--aelix', sys.executable, '--aelix-arg=' + str(self.child),
            '--state-dir', str(self.root / 'state'), cwd=self.root, env=env, stdin=-1, stdout=-1, stderr=-1)

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
        self.child.write_text(FAKE_CHILD.replace('Path("child.pid")', 'Path("child.args").write_text(json.dumps(sys.argv))\nPath("child.pid")', 1), encoding='utf-8')
        server = Server(SimpleNamespace(state_dir=str(self.root / "state"), profile="coding",
            aelix=sys.executable, aelix_arg=[str(self.child)], provider=None, model=None))
        session = Session(server, "stale", str(self.root))
        try:
            with server.db:
                save_preferences(server.db, "stale", {"trustProject": False, "agentsEnabled": True, "permission_mode": "yolo"})
            await session.start()
            argv = json.loads((self.root / "child.args").read_text())
            self.assertIn("--deny", argv)
            self.assertIn("--agents", argv)
            self.assertNotIn("--no-agents", argv)
            self.assertEqual(argv[argv.index("--permission-mode") + 1], "yolo")
            self.assertEqual(session.controls.permission_mode, "yolo")
        finally:
            await session.close()
            server.db.close()

    @unittest.skipUnless(sys.platform == 'win32', 'Windows kill-on-close job boundary')
    async def test_forced_adapter_death_kills_owned_worker_and_descendant(self):
        await self.start(startup=True, delayed_job=True)
        await self.request('init', 'initialize', {'protocolVersion':1, 'clientCapabilities':{}})
        self.initialized = True
        opened = asyncio.create_task(self.request(1, 'session/new', {'cwd': str(self.root), 'mcpServers': []}))
        await asyncio.sleep(0.2)
        self.assertFalse((self.root / 'child.pid').exists(), 'Native bootstrap ran before assignment')
        self.assertFalse((self.root / 'descendant.pid').exists())
        result = await opened
        self.assertIn('sessionId', result['result'])
        pids = [int((self.root / name).read_text()) for name in ('child.pid', 'descendant.pid')]
        self.assertTrue(all(process_alive(pid) for pid in pids))
        self.proc.kill()  # Bypasses Python cleanup; OS closes the non-inherited job handle.
        await asyncio.wait_for(self.proc.wait(), 10)
        for _ in range(100):
            if not any(process_alive(pid) for pid in pids):
                break
            await asyncio.sleep(0.02)
        self.assertFalse(any(process_alive(pid) for pid in pids))

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
        self.child.write_text(FAKE_CHILD.replace('    data={}\n', flood + '    data={}\n', 1), encoding='utf-8')
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


if __name__ == "__main__":
    unittest.main()
