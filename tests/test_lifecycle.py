"""Real OS-process checks without provider calls or user credentials."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest


def alive(pid):
    if sys.platform == 'win32':
        import ctypes
        from ctypes import wintypes
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.OpenProcess(0x100000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5  # Access denied cannot prove death.
        try:
            return api.WaitForSingleObject(handle, 0) == 258
        finally:
            api.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


FAKE_CHILD = '''import json,os,sys,subprocess
from pathlib import Path
Path("child.pid").write_text(str(os.getpid()))
if os.environ.get("TEST_STARTUP_DESCENDANT"):
    descendant=subprocess.Popen([sys.executable,"-c","import time;time.sleep(120)"],stdin=-3,stdout=-3,stderr=-3)
    Path("descendant.pid").write_text(str(descendant.pid))
if not os.environ.get("TEST_SKIP_BRIDGE_READY"):
    Path(os.environ["AELIX_ACP_READY_FILE"]).write_text(os.environ["AELIX_ACP_NONCE"])
for line in sys.stdin:
    request=json.loads(line)
    if request["type"]=="get_state" and os.environ.get("TEST_DESCENDANT"):
        descendant=subprocess.Popen([sys.executable,"-c","import time;time.sleep(120)"],stdin=-3,stdout=-3,stderr=-3)
        Path("descendant.pid").write_text(str(descendant.pid))
    data={"commands":[]} if request["type"]=="get_commands" else {}
    print(json.dumps({"type":"response","id":request["id"],"success":True,"data":data}),flush=True)
'''


class ProcessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.child = self.root / 'fake aelix 한글.py'
        self.child.write_text(FAKE_CHILD, encoding='utf-8')
        self.child.chmod(0o700)
        self.proc = None

    async def asyncTearDown(self):
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            await asyncio.wait_for(self.proc.wait(), 10)
        self.tmp.cleanup()

    async def start(self, skip_ready=False, descendant=False, startup=False, delayed_job=False):
        source = str(Path(__file__).resolve().parents[1] / 'src')
        env = dict(os.environ, PYTHONPATH=source)
        if skip_ready:
            env['TEST_SKIP_BRIDGE_READY'] = '1'
        if descendant:
            env['TEST_DESCENDANT'] = '1'
        if startup:
            env['TEST_STARTUP_DESCENDANT'] = '1'
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
        self.assertFalse(alive(child_pid))

    @unittest.skipUnless(sys.platform == 'win32', 'Windows kill-on-close job boundary')
    async def test_forced_adapter_death_kills_owned_worker_and_descendant(self):
        await self.start(startup=True, delayed_job=True)
        opened = asyncio.create_task(self.request(1, 'session/new', {'cwd': str(self.root), 'mcpServers': []}))
        await asyncio.sleep(0.2)
        self.assertFalse((self.root / 'child.pid').exists(), 'Native bootstrap ran before assignment')
        self.assertFalse((self.root / 'descendant.pid').exists())
        result = await opened
        self.assertIn('sessionId', result['result'])
        pids = [int((self.root / name).read_text()) for name in ('child.pid', 'descendant.pid')]
        self.assertTrue(all(alive(pid) for pid in pids))
        self.proc.kill()  # Bypasses Python cleanup; OS closes the non-inherited job handle.
        await asyncio.wait_for(self.proc.wait(), 10)
        for _ in range(100):
            if not any(alive(pid) for pid in pids):
                break
            await asyncio.sleep(0.02)
        self.assertFalse(any(alive(pid) for pid in pids))

    async def test_sigterm_closes_real_rpc_child(self):
        await self.start()
        result = await self.request(1, 'session/new', {'cwd':str(self.root),'mcpServers':[]})
        self.assertIn('sessionId', result['result'])
        child_pid = int((self.root / 'child.pid').read_text())
        self.assertTrue(alive(child_pid))
        self.proc.terminate()
        await asyncio.wait_for(self.proc.wait(), 10)
        for _ in range(100):
            if not alive(child_pid):
                break
            await asyncio.sleep(0.02)
        self.assertFalse(alive(child_pid))


if __name__ == '__main__':
    unittest.main()
