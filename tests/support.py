"""Independent stdio client used by black-box adapter tests."""
import asyncio
import json
import os
from pathlib import Path
import sys


class AcpClient:
    def __init__(self, process):
        self.process = process
        self.pending = {}
        self.updates = []
        self.requests = []
        self.stderr = bytearray()
        self.tasks = []
        self.counter = 0
        self.permission = "allow-once"
        self.dialog = None

    @classmethod
    async def start(cls, root, aelix, *, extensions=(), capabilities=None, prefix=()):
        source = str(Path(__file__).resolve().parents[1] / "src")
        env = {key: os.environ[key] for key in ["PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"] if key in os.environ}
        env.update(HOME=str(root / "home"), USERPROFILE=str(root / "home"), PYTHONPATH=source,
                   AELIX_CODING_AGENT_DIR=str(root / "agent"), AELIX_SETTINGS_PATH=str(root / "agent/settings.json"))
        argv = [sys.executable, "-m", "aelix_acp.server", "--aelix", str(aelix), "--state-dir", str(root / "state")]
        argv.extend("--aelix-arg=" + arg for arg in prefix)
        for extension in extensions:
            argv.extend(["--extension", str(extension)])
        process = await asyncio.create_subprocess_exec(*argv, cwd=str(root), env=env, stdin=-1, stdout=-1, stderr=-1)
        client = cls(process)
        client.tasks = [asyncio.create_task(client.read()), asyncio.create_task(client.read_errors())]
        await client.request("initialize", {"protocolVersion": 1, "clientCapabilities": capabilities or {}})
        return client

    async def send(self, value):
        self.process.stdin.write((json.dumps(value) + "\n").encode())
        await self.process.stdin.drain()

    async def request(self, method, params, timeout=30):
        self.counter += 1
        request_id = self.counter
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            response = await asyncio.wait_for(future, timeout)
            if "error" in response:
                raise RuntimeError(json.dumps(response["error"]))
            return response["result"]
        finally:
            self.pending.pop(request_id, None)

    async def read(self):
        try:
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                if "method" in message:
                    if "id" not in message:
                        self.updates.append(message)
                        continue
                    self.requests.append(message)
                    if message["method"] == "session/request_permission":
                        result = {"outcome": {"outcome": "selected", "optionId": self.permission}}
                    elif self.dialog:
                        result = self.dialog(message["params"])
                    else:
                        result = {"cancelled": True}
                    await self.send({"jsonrpc": "2.0", "id": message["id"], "result": result})
                else:
                    future = self.pending.get(message.get("id"))
                    if future is not None and not future.done():
                        future.set_result(message)
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(RuntimeError("Adapter stdout closed: " + self.stderr.decode(errors="replace")[-3000:]))

    async def read_errors(self):
        while chunk := await self.process.stderr.read(4096):
            self.stderr.extend(chunk)

    def text(self):
        return "".join(event["params"]["update"].get("content", {}).get("text", "") for event in self.updates
                       if event.get("method") == "session/update")

    async def close(self):
        if self.process.returncode is None:
            self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), 10)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


def process_alive(pid):
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
    import ctypes
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        return bool(ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))) and exit_code.value == 259
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)
