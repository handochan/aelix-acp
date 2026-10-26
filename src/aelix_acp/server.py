"""Newline JSON-RPC ACP server backed by real `aelix --mode rpc` children."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hmac
import json
import os
from pathlib import Path
import shutil
import secrets
import signal
import sqlite3
import sys
import tempfile
import time
import uuid

from . import __version__
from .stdio import StdinReader
from .operations.bindings import NativeBindings
from .operations.ledger import LedgerError, META_KEY, OperationContext, OperationLedger, SCHEMA
from .operations.lease import LeaseBusy, OwnershipError

PACKAGE = Path(__file__).resolve().parent
KINDS = {"read": "read", "grep": "search", "find": "search", "ls": "read",
         "write": "edit", "edit": "edit", "bash": "execute", "aelix_status": "read",
         "analytics_status": "read", "analytics_query": "read"}
PERMISSION_MODES = (
    {"id": "default", "name": "Ask", "description": "Ask before every tool; native guardrails remain active"},
    {"id": "yolo", "name": "Yolo", "description": "Skip client tool approval; native guardrails and tool restrictions remain active"},
)
MODE_IDS = frozenset(mode["id"] for mode in PERMISSION_MODES)


class ProtocolError(Exception):
    def __init__(self, message, code=-32602, data=None):
        super().__init__(message)
        self.code = code
        self.data = data


def tool_call(call_id, name, args, cwd, status="pending"):
    result = {"toolCallId": call_id, "title": name, "kind": KINDS.get(name, "other"),
              "status": status, "rawInput": args}
    path = args.get("path") or args.get("file_path")
    if isinstance(path, str):
        p = Path(path).expanduser()
        result["locations"] = [{"path": str(p if p.is_absolute() else Path(cwd) / p)}]
    return result


def text_content(content):
    if isinstance(content, str):
        return content
    return "\n".join(c.get("text", "") for c in content or [] if c.get("type") == "text")


def prompt_message(blocks):
    """The accepted native text, shared by dispatch and its keyed fingerprint."""
    if not isinstance(blocks, list):
        raise ProtocolError('Prompt content must be an array')
    parts = []
    for block in blocks:
        if not isinstance(block, dict):
            raise ProtocolError('Invalid prompt block')
        if block.get('type') == 'text' and isinstance(block.get('text'), str):
            parts.append(block['text'])
        elif block.get('type') == 'resource' and isinstance(block.get('resource'), dict):
            resource = block['resource']
            if not isinstance(resource.get('text'), str) or not isinstance(resource.get('uri', 'resource'), str):
                raise ProtocolError('Unsupported embedded prompt resource')
            parts.append(f"{resource.get('uri', 'resource')}:\n{resource['text']}")
        elif block.get('type') == 'resource_link' and isinstance(block.get('uri'), str):
            if not isinstance(block.get('name', 'File'), str):
                raise ProtocolError('Invalid prompt resource name')
            parts.append(f"{block.get('name', 'File')}: {block['uri']}")
        else:
            raise ProtocolError('Unsupported prompt block')
    return '\n\n'.join(parts)


class Session:
    def __init__(self, server, session_id, cwd):
        self.server, self.id, self.cwd = server, session_id, cwd
        self.proc = None
        self.pending = {}
        self.tasks = []
        self.done = None
        self.prompt_active = False
        self.failure = None
        self.cancelled = False
        self.permission_mode = "default"
        self.tools = {}
        self.permissions = set()
        self.state = {}
        self.socket_dir = None
        self.socket_server = None
        self.permission_token = None
        self.permission_endpoint = None
        self.worker_job = None
        self.writer_lease = None
        self.worker_generation = uuid.uuid4().hex
        self.operation = None
        self.analytics_project = Path(cwd) if (Path(cwd) / "analytics.json").is_file() else None

    async def start(self, session_file=None):
        # Public beta.2 --help lists these history selectors. Launch prefixes
        # must not bypass the DB-only load boundary, including flag-off new.
        for argument in getattr(self.server.args, 'aelix_arg', []):
            if argument.split('=', 1)[0] in ('--session', '--resume', '-r', '--continue', '-c', '--fork', '--no-session'):
                raise ProtocolError('Adapter launch cannot override native session selection')
        self.server.validate_session_store(self.id)
        if session_file and self.server.operations is None:
            NativeBindings.require_unmanaged(session_file)
        if self.server.operations is not None:
            self.writer_lease = self.server.bindings.prepare_writer(
                self.id, self.server.args.profile, registered_file=session_file)
        self.socket_dir = tempfile.mkdtemp(prefix="aelix-acp-")
        os.chmod(self.socket_dir, 0o700)
        socket_path = str(Path(self.socket_dir) / "bridge.sock")
        self.permission_endpoint = None
        self.permission_token = None
        if sys.platform == "win32":
            self.permission_token = secrets.token_urlsafe(32)
            self.socket_server = await asyncio.start_server(self.permission, "127.0.0.1", 0, limit=65536, backlog=8)
            port = self.socket_server.sockets[0].getsockname()[1]
            self.permission_endpoint = f"tcp://127.0.0.1:{port}"
        else:
            self.socket_server = await asyncio.start_unix_server(self.permission, path=socket_path)
            os.chmod(socket_path, 0o600)
        args = [self.server.args.aelix, *getattr(self.server.args, "aelix_arg", []), "--mode", "rpc", "--offline", "--no-extensions",
                "--no-agents", "--permission-mode", self.permission_mode,
                "--append-system-prompt-file", str(PACKAGE / "profiles" / f"{self.server.args.profile}.md")]
        if self.server.args.profile == "analytics":
            extension = self.server.args.analytics_extension
            if not extension or not Path(extension).is_file():
                raise ProtocolError("Analytics extension is missing; pass --analytics-extension PATH")
            args += ["-e", str(Path(extension).resolve())]
        # Consent sees arguments after other trusted extension hooks.
        args += ["-e", str(PACKAGE / "bridge.py")]
        if self.server.args.provider:
            args += ["--provider", self.server.args.provider]
        if self.server.args.model:
            args += ["--model", self.server.args.model]
        if session_file:
            if not Path(session_file).is_file():
                raise ProtocolError("The Aelix session file no longer exists")
            args += ["--session", session_file]
        nonce = uuid.uuid4().hex
        ready_file = Path(self.socket_dir) / "ready"
        env = dict(os.environ, AELIX_ACP_SOCKET=socket_path, PYTHONUNBUFFERED="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
                   AELIX_ACP_READY_FILE=str(ready_file), AELIX_ACP_NONCE=nonce)
        for name in ("AELIX_ACP_ENDPOINT", "AELIX_ACP_PERMISSION_TOKEN"):
            env.pop(name, None)
        for name in ('AELIX_ACP_LEASE_REFERENCE', 'AELIX_ACP_LEASE_ID', 'AELIX_ACP_WORKER_GENERATION'):
            env.pop(name, None)
        if self.permission_endpoint:
            env.pop("AELIX_ACP_SOCKET", None)
            env.update(AELIX_ACP_ENDPOINT=self.permission_endpoint, AELIX_ACP_PERMISSION_TOKEN=self.permission_token)
        if sys.platform == "win32":
            from .windows import WorkerJob
            self.worker_job = WorkerJob()
            # The trusted gate consumes one byte before starting ANY native
            # bootstrap (including configured MCP startup subprocesses).
            args = [sys.executable, "-I", "-S", "-B", str(PACKAGE / "windows.py"), *args]
        async def spawn(options):
            return await asyncio.create_subprocess_exec(
                *args, cwd=self.cwd, env=env, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                limit=16 * 1024 * 1024, **options)
        if self.writer_lease is not None:
            with self.writer_lease.inheritance() as (reference, options):
                env.update(AELIX_ACP_LEASE_REFERENCE=str(reference), AELIX_ACP_LEASE_ID=self.writer_lease.identity,
                           AELIX_ACP_WORKER_GENERATION=self.worker_generation)
                if os.name != 'nt':
                    options['start_new_session'] = True
                # Do not lose ownership of a successfully created child if a
                # cancellation arrives between CreateProcess/fork and return.
                task = asyncio.create_task(spawn(options))
                cancelled = False
                while True:
                    try:
                        self.proc = await asyncio.shield(task)
                        break
                    except asyncio.CancelledError:
                        cancelled = True
                if cancelled:
                    raise asyncio.CancelledError
        else:
            self.proc = await spawn({})
        if self.worker_job:
            # The known native RPC child receives no commands before it joins
            # the job. Later tool/MCP descendants inherit this owned lifetime.
            try:
                self.worker_job.assign(self.proc.pid)
                self.proc.stdin.write(b"\0")
                await self.proc.stdin.drain()
            except BaseException:
                self.proc.kill()
                await self.proc.wait()
                raise
        self.tasks = [asyncio.create_task(self.read_rpc()), asyncio.create_task(self.read_stderr())]
        self.state = await self.rpc("get_state")
        if self.writer_lease is not None:
            try:
                with ready_file.open('rb') as source:
                    data = source.read(4097)
                if len(data) > 4096:
                    raise ValueError
                ready = json.loads(data)
                pid = ready['nativePid']
                owned_pid = (isinstance(pid, int) and not isinstance(pid, bool)
                             and (self.worker_job.contains(pid) if self.worker_job else pid == self.proc.pid))
                if (ready['nonce'] != nonce or ready['leaseId'] != self.writer_lease.identity
                        or ready['generation'] != self.worker_generation or not owned_pid):
                    raise ValueError
                self.writer_lease.verify()
                native_file = self.state['sessionFile']
                if session_file and not os.path.samefile(native_file, session_file):
                    raise ValueError
                self.server.bindings.bind_native(self.id, self.server.args.profile, native_file,
                                                 self.writer_lease, native_id=self.state['sessionId'], allow_create=True)
            except (OSError, ValueError, KeyError, TypeError):
                raise ProtocolError('Managed native ownership handshake failed', -32603) from None
        elif not ready_file.is_file() or ready_file.read_text() != nonce:
            raise ProtocolError("The tool approval bridge did not load; refusing this session", -32603)
        await self.rpc("set_auto_retry", enabled=False)
        self.save(include_mode=False)

    def save(self, include_mode=True):
        if self.server.operations is not None:
            self.server.db.execute('INSERT OR IGNORE INTO managed_stores VALUES(?,?)',
                                   (self.id, self.server.operations.store_id))
            self.server.validate_session_store(self.id)
        self.server.db.execute(
            "INSERT OR REPLACE INTO sessions VALUES(?,?,?,?,?,?)",
            (self.id, self.cwd, self.server.args.profile, self.state.get("sessionFile"),
             self.state.get("sessionName") or f"Aelix {self.server.args.profile}", int(time.time())))
        if include_mode:
            self.server.db.execute("INSERT OR REPLACE INTO permission_modes VALUES(?,?)", (self.id, self.permission_mode))
        self.server.db.commit()

    def selectors(self):
        result = {"modes": {"currentModeId": self.permission_mode, "availableModes": list(PERMISSION_MODES)},
                  "_meta": {"aelix/permission": {"nativeLaunchMode": self.permission_mode,
                            "approvalPolicy": "ask" if self.permission_mode == "default" else "yolo",
                            "appliesAt": "worker_restart", "nativePostureObservation": "launch_argument"}}}
        if self.server.operations is not None:
            result['_meta']['aelix/operationStore'] = dict(schema=SCHEMA, storeId=self.server.operations.store_id,
                workerGeneration=self.worker_generation, nativeSessionId=self.state.get('sessionId'))
        model = self.state.get("model") or {}
        if model.get("id"):
            model_id = f"{model.get('provider', '')}/{model['id']}"
            result["models"] = {"currentModelId": model_id, "availableModels": [
                {"modelId": model_id, "name": model.get("name") or model["id"],
                 "description": "Selected at launch; configure --provider and --model to change"}]}
        return result

    async def rpc(self, command, **kwargs):
        request_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            if not self.proc or self.proc.returncode is not None:
                raise ProtocolError("Aelix child is not running", -32603)
            self.proc.stdin.write((json.dumps({"id": request_id, "type": command, **kwargs}) + "\n").encode())
            await self.proc.stdin.drain()
            return await asyncio.wait_for(future, 45)
        finally:
            self.pending.pop(request_id, None)

    async def read_stderr(self):
        omitted = False
        while line := await self.proc.stderr.readline():
            if self.server.operations is not None:
                if not omitted:
                    print('[aelix-acp] Native diagnostic output omitted for managed operations', file=sys.stderr)
                    omitted = True
                continue
            # Provider errors may contain user data: keep diagnostics local on stderr.
            sys.stderr.write(f"[aelix:{self.id[:8]}] {line.decode(errors='replace')}")
            sys.stderr.flush()

    async def read_rpc(self):
        try:
            while line := await self.proc.stdout.readline():
                try:
                    event = json.loads(line)
                except ValueError:
                    print("[aelix-acp] Ignored non-JSON child stdout", file=sys.stderr)
                    continue
                future = self.pending.get(event.get("id"))
                if event.get("type") == "response" and future is not None:
                    if not future.done():
                        if event.get("success"):
                            future.set_result(event.get("data") or {})
                        else:
                            message = ('Native RPC rejected the request' if self.server.operations is not None
                                       else str(event.get('error', 'RPC failed')))
                            future.set_exception(ProtocolError(message, -32603))
                else:
                    await self.event(event)
        except Exception as exc:
            self.failure = f"Aelix stream failed: {exc}"
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ProtocolError("Aelix process closed its RPC stream", -32603))
            if self.done and not self.done.done():
                self.failure = self.failure or "Aelix process closed during a turn"
                self.done.set_result(None)

    async def event(self, event):
        kind = event.get("type")
        if kind == "message_update":
            update = event.get("assistantMessageEvent") or event.get("assistant_message_event") or {}
            if update.get("type") in ("text_delta", "thinking_delta") and update.get("delta"):
                await self.update({"sessionUpdate": "agent_thought_chunk" if update["type"] == "thinking_delta"
                                   else "agent_message_chunk",
                                   "content": {"type": "text", "text": update["delta"]}})
        elif kind in ("tool_execution_start", "tool_execution_update", "tool_execution_end"):
            call_id = event.get("toolCallId") or event.get("tool_call_id")
            name = event.get("toolName") or event.get("tool_name") or "tool"
            if kind == "tool_execution_start":
                # Aelix emits execution_start before the extension consent hook.
                call = tool_call(call_id, name, event.get("args") or {}, self.cwd, "pending")
                if call_id not in self.tools:
                    await self.update({"sessionUpdate": "tool_call", **call})
                else:
                    await self.update({"sessionUpdate": "tool_call_update", **call})
                self.tools[call_id] = call
            else:
                result = event.get("result") or event.get("partialResult") or event.get("partial_result") or {}
                failed = event.get("isError", event.get("is_error", False)) or result.get("is_error", result.get("isError", False))
                update = {"sessionUpdate": "tool_call_update", "toolCallId": call_id,
                          "status": ("failed" if failed else "completed") if kind.endswith("end") else "in_progress",
                          "rawOutput": result,
                          "content": [{"type": "content", "content": {"type": "text", "text": text_content(result.get("content"))}}]}
                await self.update(update)
                if kind.endswith("end") and not failed:
                    details = result.get("details") or {}
                    if name == "analytics_init" and details.get("project"):
                        self.analytics_project = Path(details["project"])
                    if name == "analytics_run" and self.analytics_project and details.get("run", {}).get("status") == "ok":
                        from .preview import bundle_run
                        try:
                            preview = await asyncio.to_thread(bundle_run, self.analytics_project, details["run"]["id"])
                            await self.update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text",
                                "text": f"\n[AionUi 분석 미리보기]({preview})\n"}})
                        except (OSError, ValueError) as exc:
                            print('[aelix-acp] Standalone preview unavailable' if self.server.operations is not None
                                  else f"[aelix-acp] Standalone preview unavailable: {exc}", file=sys.stderr)
        elif kind == "message_end":
            message = event.get("message") or {}
            error = message.get("errorMessage") or message.get("error_message")
            if error:
                self.failure = error
        elif kind == "agent_end" and self.done and not self.done.done():
            self.done.set_result(None)

    async def update(self, update):
        await self.server.send({"jsonrpc": "2.0", "method": "session/update",
                                "params": {"sessionId": self.id, "update": update}})

    async def permission(self, reader, writer):
        allowed = False
        permission_task = asyncio.current_task()
        try:
            if self.permission_endpoint and len(self.permissions) >= 8:
                return
            self.permissions.add(permission_task)
            async with asyncio.timeout(5 if self.permission_endpoint else 300):
                request = json.loads(await reader.readline())
            if self.permission_endpoint:
                supplied = request.pop("token", None) if isinstance(request, dict) else None
                if not self.permission_token or not isinstance(supplied, str) or not hmac.compare_digest(supplied, self.permission_token):
                    return
            if self.cancelled:
                return
            async with asyncio.timeout(300):
                call = tool_call(request["toolCallId"], request["name"], request["args"], self.cwd)
                known = call["toolCallId"] in self.tools
                self.tools[call["toolCallId"]] = call
                await self.update({"sessionUpdate": "tool_call_update" if known else "tool_call", **call})
                if not self.cancelled:
                    if self.permission_mode == "yolo":
                        allowed = True
                    else:
                        answer = await self.server.client_request("session/request_permission", {
                            "sessionId": self.id, "toolCall": call,
                            "options": [{"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
                                        {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"}]})
                        outcome = answer.get("outcome") or {}
                        allowed = outcome.get("outcome") == "selected" and outcome.get("optionId") == "allow-once"
                    if allowed and not self.cancelled:
                        await self.update({"sessionUpdate": "tool_call_update",
                                           "toolCallId": call["toolCallId"], "status": "in_progress"})
        except (Exception, asyncio.CancelledError):
            allowed = False
        finally:
            self.permissions.discard(permission_task)
            with contextlib.suppress(ConnectionError):
                writer.write((json.dumps({"allow": allowed and not self.cancelled}) + "\n").encode())
                await writer.drain()
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    async def commands(self):
        data = await self.rpc("get_commands")
        if self.server.args.profile == "analytics" and not any(c.get("name") == "analytics" for c in data.get("commands", [])):
            raise ProtocolError("The Analytics extension did not register its command; refusing this profile", -32603)
        await self.update({"sessionUpdate": "available_commands_update", "availableCommands": [
            {"name": c["name"], "description": c.get("description") or c["name"]}
            for c in data.get("commands", [])]})

    def require_prompt_slot(self):
        if self.id in self.server.mutations:
            raise ProtocolError("Session is changing configuration; retry after the operation completes", -32000)
        if self.prompt_active or (self.done and not self.done.done()):
            raise ProtocolError("A prompt is already running in this session")

    async def prompt(self, blocks, *, accepted_message=None):
        self.require_prompt_slot()
        message = prompt_message(blocks) if accepted_message is None else accepted_message
        self.done = asyncio.get_running_loop().create_future()
        self.prompt_active = True
        self.failure, self.cancelled = None, False
        self.tools.clear()
        try:
            await self.rpc("prompt", message=message)
            await asyncio.wait_for(asyncio.shield(self.done), 600)
            if self.failure:
                raise ProtocolError('Native turn failed' if self.server.operations is not None else self.failure, -32603)
            self.state = await self.rpc("get_state")
            if self.server.operations is not None and (self.state.get('isStreaming') is not False
                                                        or self.state.get('isCompacting') is not False):
                raise ProtocolError('Managed native terminal state cannot be verified', -32603)
            self.save()
            return {"stopReason": "cancelled" if self.cancelled else "end_turn"}
        except TimeoutError:
            await self.cancel()
            raise ProtocolError("Aelix turn timed out", -32603, {'aelix/stop': 'timeout'})
        finally:
            self.prompt_active = False
            if self.done and not self.done.done():
                self.done.cancel()

    async def cancel(self):
        self.cancelled = True
        for task in list(self.permissions):
            task.cancel()
        await self.rpc("abort")

    async def close(self):
        self.cancelled = True
        self.permission_token = None
        permissions = list(self.permissions)
        for task in permissions:
            task.cancel()
        await asyncio.gather(*permissions, return_exceptions=True)
        if self.proc and self.proc.returncode is None:
            if self.worker_job:
                self.worker_job.close()
                self.worker_job = None
            else:
                if self.writer_lease is not None and os.name != 'nt':
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(self.proc.pid, signal.SIGTERM)
                else:
                    self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except TimeoutError:
                if self.writer_lease is not None and os.name != 'nt':
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(self.proc.pid, signal.SIGKILL)
                else:
                    self.proc.kill()
                await self.proc.wait()
        if self.worker_job:
            self.worker_job.close()
            self.worker_job = None
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.socket_server:
            self.socket_server.close()
            await self.socket_server.wait_closed()
            self.socket_server = None
        if self.socket_dir:
            with contextlib.suppress(FileNotFoundError):
                shutil.rmtree(self.socket_dir)
            self.socket_dir = None
        if self.writer_lease is not None:
            self.writer_lease.close()
            self.writer_lease = None


class Server:
    def __init__(self, args):
        self.args = args
        self.status_only = getattr(args, 'operations_status_only', False)
        self.instance_id = uuid.uuid4().hex
        self.operations = None
        self.bindings = None
        state_dir = Path(args.state_dir).expanduser()
        if self.status_only:
            self.db = None
        else:
            state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.db = sqlite3.connect(state_dir / "sessions.sqlite3")
            self.db.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, cwd TEXT, profile TEXT, file TEXT, title TEXT, updated INTEGER)")
            self.db.execute("CREATE TABLE IF NOT EXISTS permission_modes (id TEXT PRIMARY KEY, mode TEXT NOT NULL)")
            self.db.execute('CREATE TABLE IF NOT EXISTS managed_stores (session_id TEXT PRIMARY KEY, store_id TEXT NOT NULL)')
        if self.status_only or getattr(args, 'managed_operations', False):
            previously_managed = self.db is not None and self.db.execute('SELECT 1 FROM managed_stores LIMIT 1').fetchone()
            try:
                self.operations = OperationLedger(state_dir, create=not self.status_only and not previously_managed)
            except BaseException:
                if self.db is not None:
                    self.db.close()
                raise
            self.bindings = NativeBindings(self.operations)
        self.sessions = {}
        self.mutations = {}
        self.pending = {}
        self.tasks = set()
        self.output_lock = asyncio.Lock()

    async def send(self, message):
        async with self.output_lock:
            sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
            sys.stdout.flush()

    async def client_request(self, method, params):
        request_id = "acp-" + uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            return await asyncio.wait_for(future, 290)
        finally:
            self.pending.pop(request_id, None)

    def session(self, params):
        try:
            return self.sessions[params["sessionId"]]
        except KeyError:
            raise ProtocolError("Unknown session; use session/load before prompting") from None

    def reserve(self, session_id):
        if session_id in self.mutations:
            raise ProtocolError("Session configuration is busy", -32000)
        self.mutations[session_id] = asyncio.current_task()

    def validate_session_store(self, session_id):
        row = self.db.execute('SELECT store_id FROM managed_stores WHERE session_id=?', (session_id,)).fetchone()
        if row and (self.operations is None or row[0] != self.operations.store_id):
            raise ProtocolError('Saved managed session requires its original operation store', -32042)

    def store_mode(self, session_id, mode):
        self.db.execute("INSERT OR REPLACE INTO permission_modes VALUES(?,?)", (session_id, mode))
        self.db.commit()

    def stored_mode(self, session_id):
        row = self.db.execute("SELECT mode FROM permission_modes WHERE id=?", (session_id,)).fetchone()
        mode = row[0] if row else "default"  # Existing pre-mode sessions stay Ask.
        if not isinstance(mode, str) or mode not in MODE_IDS:
            raise ProtocolError("Saved permission mode is unsupported; refusing to resume")
        return mode

    async def require_idle(self, session):
        if session.prompt_active or (session.done and not session.done.done()) or session.permissions:
            raise ProtocolError("Session is busy; cancel the active turn before changing configuration", -32000)
        if (await session.rpc("get_state")).get("isStreaming"):
            raise ProtocolError("Native session is still streaming or compacting", -32000)

    async def finish_cleanup(self, *sessions, propagate_cancel=False):
        async def cleanup():
            for session in sessions:
                if session:
                    await session.close()
        task = asyncio.create_task(cleanup())
        cancelled = False
        while True:
            try:
                await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                cancelled = True
                continue  # Repeated Stop/EOF must not interrupt cleanup.
        if cancelled and propagate_cancel:
            raise asyncio.CancelledError

    def operation_context(self, params):
        meta = params.get('_meta', {})
        if not isinstance(meta, dict):
            raise ProtocolError('Invalid operation metadata')
        if META_KEY not in meta:
            if self.operations is not None and 'aelix/requestId' in meta:
                raise ProtocolError('Legacy request metadata does not negotiate managed operations')
            return None
        if self.operations is None:
            raise ProtocolError('Managed operations are not negotiated', -32601)
        return OperationContext.parse(meta[META_KEY])

    @staticmethod
    def operation_result(receipt):
        if receipt['state'] in ('completed', 'cancelled'):
            return {'stopReason': receipt['stopReason'], '_meta': {META_KEY: receipt}}
        if receipt['state'] == 'pending':
            raise ProtocolError('Operation is pending; query its original status', -32045, {META_KEY: receipt})
        raise ProtocolError('Operation is interrupted or failed; automatic replay is disabled', -32046,
                            {META_KEY: receipt})

    async def prompt(self, params):
        operation = self.operation_context(params)
        if operation is None:
            return await self.session(params).prompt(params['prompt'])
        message = prompt_message(params['prompt'])
        # Bound the ACTUAL current RPC encoding before reserving an operation.
        try:
            if len(json.dumps({'type': 'prompt', 'message': message}).encode('utf-8')) > 16 * 1024 * 1024 - 1024:
                raise ProtocolError('Prompt exceeds the managed native frame budget')
        except UnicodeError:
            raise ProtocolError('Prompt encoding is invalid') from None
        session_id = params['sessionId']
        existing = self.operations.lookup(session_id, self.args.profile, operation)
        if existing is not None:
            _, receipt = self.operations.claim(session_id, self.args.profile, operation,
                                                message, self.instance_id, None)
            return self.operation_result(receipt)
        session = self.session(params)
        session.require_prompt_slot()
        if operation.generation != session.worker_generation:
            raise ProtocolError('Managed native generation is stale', -32044)
        self.bindings.bind_native(session.id, self.args.profile, session.state['sessionFile'],
                                  session.writer_lease, native_id=session.state['sessionId'])
        claimed, receipt = self.operations.claim(session_id, self.args.profile, operation,
                                                  message, self.instance_id, session.writer_lease)
        if not claimed:
            return self.operation_result(receipt)
        session.operation = operation
        try:
            result = await session.prompt(params['prompt'], accepted_message=message)
            cancelled = result['stopReason'] == 'cancelled'
            receipt = self.operations.finish(session.id, operation.operation_id, self.instance_id,
                                               'cancelled' if cancelled else 'completed',
                                               'cancelled' if cancelled else 'end_turn')
        except BaseException as error:
            # Abort acknowledgement is not a settled native terminal. Close the
            # owned worker, then require a FRESH OS barrier before settlement.
            self.sessions.pop(session.id, None)
            await self.finish_cleanup(session)
            reason = 'timeout' if getattr(error, 'data', None) == {'aelix/stop': 'timeout'} else 'transport_closed'
            try:
                receipt = self.operations.recover_abandoned(session.id, self.args.profile, operation, code=reason)
            except LeaseBusy:
                receipt = self.operations.lookup(session.id, self.args.profile, operation)
            if isinstance(error, asyncio.CancelledError):
                raise
            return self.operation_result(receipt)
        finally:
            session.operation = None
        return self.operation_result(receipt)

    def operation_status(self, params):
        if self.operations is None:
            raise ProtocolError('Managed operations are not negotiated', -32601)
        if set(params) != {'sessionId', 'operation'}:
            raise ProtocolError('Invalid operation status request')
        operation = OperationContext.parse(params['operation'])
        receipt = self.operations.lookup(params['sessionId'], self.args.profile, operation)
        if receipt is None:
            return {'operation': dict(schema=SCHEMA, id=operation.operation_id, storeId=self.operations.store_id,
                                      conversationId=operation.conversation_id, turnId=operation.turn_id,
                                      generation=operation.generation, state='not_found',
                                      effectsUnknown=True, automaticRetry=False)}
        current = self.sessions.get(params['sessionId'])
        if receipt['state'] == 'pending' and not (current and current.operation == operation and current.prompt_active):
            try:
                receipt = self.operations.recover_abandoned(params['sessionId'], self.args.profile, operation)
            except LeaseBusy:
                receipt['executionObservation'] = 'recovery_waiting'
        return {'operation': receipt}

    async def set_mode(self, params):
        mode = params.get("modeId")
        if not isinstance(mode, str) or mode not in MODE_IDS:
            raise ProtocolError("Only Ask (default) and Yolo are supported by this adapter")
        session = self.session(params)
        self.reserve(session.id)
        candidate = None
        restarting = False
        try:
            await self.require_idle(session)
            if mode == session.permission_mode:
                await session.update({"sessionUpdate": "current_mode_update", "currentModeId": mode})
                return {}
            restarting = True
            session_file = session.state.get("sessionFile")
            if not session_file or not Path(session_file).is_file():
                raise ProtocolError("No persisted native transcript; refusing permission restart", -32603)
            # Commit revocation before stopping a Yolo worker. A failed or
            # interrupted downgrade must not resurrect Yolo on session/load.
            if mode == "default":
                self.store_mode(session.id, "default")
            await session.close()
            candidate = Session(self, session.id, session.cwd)
            candidate.permission_mode = mode
            await candidate.start(session_file)
            await candidate.commands()
            candidate.save()
            self.sessions[session.id] = candidate
            await candidate.update({"sessionUpdate": "current_mode_update", "currentModeId": mode})
            return {}
        except BaseException as exc:
            downgrade_error = mode == "default" and session.permission_mode == "yolo" and getattr(exc, "code", None) != -32000
            if restarting or downgrade_error or isinstance(exc, asyncio.CancelledError):
                self.store_mode(session.id, "default")
                self.sessions.pop(session.id, None)
                await self.finish_cleanup(candidate, session)
                recovery = {"state": "stopped", "savedMode": "default", "recoveryMethod": "session/load"}
                if isinstance(exc, asyncio.CancelledError):
                    raise ProtocolError("Permission change cancelled; worker stopped and saved mode is Ask", -32800, recovery) from None
                raise ProtocolError("Permission restart failed; worker stopped and saved mode is Ask; use session/load", -32603, recovery) from None
            raise
        finally:
            self.mutations.pop(session.id, None)

    async def dispatch(self, method, params):
        if self.status_only and method not in ('initialize', '_aelix/operation_status'):
            raise ProtocolError('Status-only transport cannot start or control a native session', -32601)
        if method == "initialize":
            if params.get("protocolVersion") != 1:
                raise ProtocolError("Only ACP protocol version 1 is supported")
            capabilities = {"loadSession": True, "promptCapabilities": {"embeddedContext": True},
                            "mcpCapabilities": {"http": False, "sse": False},
                            "sessionCapabilities": {"list": {}}}
            if self.status_only:
                capabilities.update(loadSession=False, promptCapabilities={}, sessionCapabilities={})
            if self.operations is not None:
                self.operations._validate_storage_identity()
                capabilities['_meta'] = {'aelix/operations': dict(schema=SCHEMA, storeId=self.operations.store_id,
                    statusMethod='_aelix/operation_status', statusOnlyArgument='--operations-status-only',
                    statusOnly=self.status_only)}
            return {"protocolVersion": 1,
                    "agentInfo": {"name": "aelix-acp", "title": f"Aelix {self.args.profile.title()}", "version": __version__},
                    "agentCapabilities": capabilities, "authMethods": []}
        if method == '_aelix/operation_status':
            return self.operation_status(params)
        if method in ("session/new", "session/load"):
            meta = params.get('_meta', {})
            if not isinstance(meta, dict):
                raise ProtocolError('Invalid session operation metadata')
            expected_store = meta.get('aelix/operationStore')
            if expected_store is not None:
                if (self.operations is None or not isinstance(expected_store, dict)
                        or expected_store != {'schema': SCHEMA, 'storeId': self.operations.store_id}):
                    raise ProtocolError('Managed operation store identity changed', -32042)
            cwd = str(Path(params["cwd"]).expanduser().resolve())
            if not Path(cwd).is_dir():
                raise ProtocolError("Session cwd must be an existing directory")
            session_file = None
            if method == "session/load":
                session_id = params["sessionId"]
                row = self.db.execute("SELECT cwd,profile,file FROM sessions WHERE id=?", (session_id,)).fetchone()
                if not row or row[0] != cwd or row[1] != self.args.profile:
                    raise ProtocolError("Saved session does not match this workspace and profile")
                session_file = row[2]
                if not session_file:
                    raise ProtocolError("No persisted Aelix session file is available")
            else:
                session_id = str(uuid.uuid4())
            self.reserve(session_id)
            session = Session(self, session_id, cwd)
            old_session = None
            replacing = False
            try:
                session.permission_mode = self.stored_mode(session_id) if session_file else getattr(self.args, "permission_mode", "default")
                if session_id in self.sessions:
                    old_session = self.sessions[session_id]
                    await self.require_idle(old_session)
                    replacing = True
                    await self.finish_cleanup(old_session, propagate_cancel=True)
                    self.sessions.pop(session_id, None)
                await session.start(session_file)
                self.sessions[session_id] = session
                await session.commands()
                session.save()
                if params.get("mcpServers"):
                    await session.update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text",
                        "text": "[aelix-acp PoC] Client MCP servers are not forwarded. Aelix's own configured MCP servers remain available.\n"}})
                if method == "session/load":
                    history = await session.rpc("get_messages")
                    for message in history.get("messages", []):
                        if message.get("role") in ("user", "assistant"):
                            text = text_content(message.get("content"))
                            if text:
                                await session.update({"sessionUpdate": "user_message_chunk" if message["role"] == "user"
                                    else "agent_message_chunk", "content": {"type": "text", "text": text}})
            except BaseException as exc:
                if self.sessions.get(session_id) is session or (replacing and self.sessions.get(session_id) is old_session):
                    self.sessions.pop(session_id, None)
                await self.finish_cleanup(session, old_session if replacing else None)
                if isinstance(exc, asyncio.CancelledError):
                    raise ProtocolError("Session start cancelled; worker stopped", -32800) from None
                raise
            finally:
                self.mutations.pop(session_id, None)
            return {**session.selectors(), **({"sessionId": session_id} if method.endswith("new") else {})}
        if method == "session/list":
            rows = self.db.execute("SELECT id,cwd,title,updated FROM sessions WHERE profile=? ORDER BY updated DESC", (self.args.profile,))
            from datetime import datetime, timezone
            return {"sessions": [{"sessionId": r[0], "cwd": r[1], "title": r[2],
                                  "updatedAt": datetime.fromtimestamp(r[3], timezone.utc).isoformat()} for r in rows
                                 if not params.get("cwd") or r[1] == params["cwd"]]}
        if method == "session/prompt":
            return await self.prompt(params)
        if method == "session/cancel":
            operation = self.operation_context(params)
            if operation is not None:
                current = self.sessions.get(params.get('sessionId'))
                if current is None or current.operation != operation:
                    return {'cancelled': False, 'reason': 'stale_operation'}
            if task := self.mutations.get(params.get("sessionId")):
                if not task.cancelling():
                    task.cancel()
                await asyncio.gather(asyncio.shield(task), return_exceptions=True)
                return {}
            await self.session(params).cancel()
            return {}
        if method == "session/set_mode":
            return await self.set_mode(params)
        if method == "session/set_model":
            if params.get("sessionId") in self.mutations:
                raise ProtocolError("Session configuration is busy", -32000)
            models = self.session(params).selectors().get("models") or {}
            if params.get("modelId") not in ("default", models.get("currentModelId")):
                raise ProtocolError("This PoC selects the model at launch; use --provider and --model")
            return {}
        if method == "authenticate":
            raise ProtocolError("Configure provider authentication using the existing Aelix CLI first")
        raise ProtocolError('Method not supported' if self.operations is not None else f"Method not supported: {method}", -32601)

    async def handle(self, message):
        request_id = message.get("id")
        replied = False
        try:
            result = await self.dispatch(message["method"], message.get("params") or {})
            if request_id is not None:
                await self.send({"jsonrpc": "2.0", "id": request_id, "result": result})
                replied = True
                if message["method"] in ("session/new", "session/load"):
                    session_id = result.get("sessionId") or message.get("params", {}).get("sessionId")
                    session = self.sessions.get(session_id)
                    if session:
                        # State notification follows the open response. Hosts
                        # can apply their load snapshot first, then observe the
                        # actual restored policy instead of an old preference.
                        await session.update({"sessionUpdate": "current_mode_update", "currentModeId": session.permission_mode})
        except Exception as exc:
            if request_id is not None and not replied:
                if isinstance(exc, LedgerError):
                    codes = {'invalid_operation': -32602, 'operation_conflict': -32041, 'store_changed': -32042,
                             'store_unavailable': -32043, 'ownership_changed': -32044}
                    error = {'code': codes[exc.code], 'message': str(exc), 'data': {'kind': exc.code, 'automaticRetry': False}}
                elif isinstance(exc, OwnershipError):
                    error = {'code': -32044, 'message': 'Managed native ownership is unavailable',
                             'data': {'kind': 'ownership_unavailable', 'automaticRetry': False}}
                elif self.operations is not None and not isinstance(exc, ProtocolError):
                    error = {'code': -32603, 'message': 'Managed request failed',
                             'data': {'kind': 'internal_error', 'automaticRetry': False}}
                else:
                    error = {"code": getattr(exc, "code", -32603), "message": str(exc)}
                if getattr(exc, "data", None) is not None:
                    error["data"] = exc.data
                await self.send({"jsonrpc": "2.0", "id": request_id,
                                 "error": error})
            else:
                print('[aelix-acp] Managed notification failed' if self.operations is not None else f"[aelix-acp] {exc}", file=sys.stderr)

    async def run(self):
        reader = StdinReader()
        loop = asyncio.get_running_loop()
        run_task = asyncio.current_task()
        installed_signals = []
        for sig in (getattr(signal, name) for name in ("SIGTERM", "SIGHUP") if hasattr(signal, name)):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, run_task.cancel)
                installed_signals.append(sig)
        try:
            while line := await reader.readline():
                try:
                    message = json.loads(line)
                except ValueError:
                    await self.send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid JSON"}})
                    continue
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    await self.send({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid JSON-RPC request"}})
                    continue
                if "method" not in message:
                    future = self.pending.get(message.get("id"))
                    if future and not future.done():
                        if "error" in message:
                            future.set_exception(ProtocolError(str(message["error"])))
                        else:
                            future.set_result(message.get("result") or {})
                    continue
                task = asyncio.create_task(self.handle(message))
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)
        finally:
            reader.close()
            for task in list(self.tasks):
                if not task.cancelling():
                    task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            await asyncio.gather(*(s.close() for s in self.sessions.values()), return_exceptions=True)
            if self.db is not None:
                self.db.close()
            if self.operations is not None:
                self.operations.close()
            for sig in installed_signals:
                loop.remove_signal_handler(sig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--profile", choices=["coding", "analytics"], default="coding")
    parser.add_argument("--aelix", default=shutil.which("aelix") or str(Path.home() / ".local/bin/aelix"))
    parser.add_argument("--aelix-arg", action="append", default=[], help="Literal native command prefix argument; repeat as --aelix-arg=VALUE")
    parser.add_argument("--analytics-extension", default=os.environ.get("AELIX_ANALYTICS_EXTENSION"))
    parser.add_argument("--state-dir", default=os.environ.get("AELIX_ACP_STATE_DIR", str(Path.home() / ".aelix/acp")))
    parser.add_argument("--provider")
    parser.add_argument("--model")
    parser.add_argument("--permission-mode", choices=sorted(MODE_IDS), default="default",
                        help="Initial mode for new sessions; saved sessions restore their recorded mode")
    parser.add_argument('--managed-operations', action='store_true', help='Negotiate private managed operation/ownership contracts')
    parser.add_argument('--operations-status-only', action='store_true', help='Read existing managed outcomes without native startup')
    args = parser.parse_args()
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="strict")
    try:
        asyncio.run(Server(args).run())
    except (KeyboardInterrupt, BrokenPipeError, asyncio.CancelledError):
        pass


if __name__ == "__main__":
    main()
