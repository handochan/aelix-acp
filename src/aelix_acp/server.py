"""Newline JSON-RPC ACP server backed by real `aelix --mode rpc` children."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import sys
import tempfile
import time
import uuid

from . import __version__

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
        self.analytics_project = Path(cwd) if (Path(cwd) / "analytics.json").is_file() else None

    async def start(self, session_file=None):
        self.socket_dir = tempfile.mkdtemp(prefix="aelix-acp-")
        os.chmod(self.socket_dir, 0o700)
        socket_path = str(Path(self.socket_dir) / "bridge.sock")
        self.socket_server = await asyncio.start_unix_server(self.permission, path=socket_path)
        os.chmod(socket_path, 0o600)
        args = [self.server.args.aelix, "--mode", "rpc", "--offline", "--no-extensions",
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
        env = dict(os.environ, AELIX_ACP_SOCKET=socket_path, PYTHONUNBUFFERED="1",
                   AELIX_ACP_READY_FILE=str(ready_file), AELIX_ACP_NONCE=nonce)
        self.proc = await asyncio.create_subprocess_exec(
            *args, cwd=self.cwd, env=env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=16 * 1024 * 1024)
        self.tasks = [asyncio.create_task(self.read_rpc()), asyncio.create_task(self.read_stderr())]
        self.state = await self.rpc("get_state")
        if not ready_file.is_file() or ready_file.read_text() != nonce:
            raise ProtocolError("The tool approval bridge did not load; refusing this session", -32603)
        await self.rpc("set_auto_retry", enabled=False)
        self.save(include_mode=False)

    def save(self, include_mode=True):
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
        while line := await self.proc.stderr.readline():
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
                            future.set_exception(ProtocolError(str(event.get("error", "RPC failed")), -32603))
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
                            print(f"[aelix-acp] Standalone preview unavailable: {exc}", file=sys.stderr)
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
        self.permissions.add(permission_task)
        try:
            async with asyncio.timeout(300):
                request = json.loads(await reader.readline())
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

    async def prompt(self, blocks):
        if self.id in self.server.mutations:
            raise ProtocolError("Session is changing configuration; retry after the operation completes", -32000)
        if self.prompt_active or (self.done and not self.done.done()):
            raise ProtocolError("A prompt is already running in this session")
        parts = []
        for block in blocks:
            if block.get("type") == "text":
                parts.append(block["text"])
            elif block.get("type") == "resource" and "text" in block.get("resource", {}):
                resource = block["resource"]
                parts.append(f"{resource.get('uri', 'resource')}:\n{resource['text']}")
            elif block.get("type") == "resource_link":
                parts.append(f"{block.get('name', 'File')}: {block['uri']}")
            else:
                raise ProtocolError(f"Unsupported prompt block: {block.get('type')}")
        self.done = asyncio.get_running_loop().create_future()
        self.prompt_active = True
        self.failure, self.cancelled = None, False
        self.tools.clear()
        try:
            await self.rpc("prompt", message="\n\n".join(parts))
            await asyncio.wait_for(asyncio.shield(self.done), 600)
            if self.failure:
                raise ProtocolError(self.failure, -32603)
            self.state = await self.rpc("get_state")
            self.save()
            return {"stopReason": "cancelled" if self.cancelled else "end_turn"}
        except TimeoutError:
            await self.cancel()
            raise ProtocolError("Aelix turn timed out", -32603)
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
        permissions = list(self.permissions)
        for task in permissions:
            task.cancel()
        await asyncio.gather(*permissions, return_exceptions=True)
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except TimeoutError:
                self.proc.kill()
                await self.proc.wait()
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


class Server:
    def __init__(self, args):
        self.args = args
        state_dir = Path(args.state_dir).expanduser()
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(state_dir / "sessions.sqlite3")
        self.db.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, cwd TEXT, profile TEXT, file TEXT, title TEXT, updated INTEGER)")
        self.db.execute("CREATE TABLE IF NOT EXISTS permission_modes (id TEXT PRIMARY KEY, mode TEXT NOT NULL)")
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
        if method == "initialize":
            if params.get("protocolVersion") != 1:
                raise ProtocolError("Only ACP protocol version 1 is supported")
            return {"protocolVersion": 1,
                    "agentInfo": {"name": "aelix-acp", "title": f"Aelix {self.args.profile.title()}", "version": __version__},
                    "agentCapabilities": {"loadSession": True, "promptCapabilities": {"embeddedContext": True},
                                          "mcpCapabilities": {"http": False, "sse": False},
                                          "sessionCapabilities": {"list": {}}}, "authMethods": []}
        if method in ("session/new", "session/load"):
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
            return await self.session(params).prompt(params["prompt"])
        if method == "session/cancel":
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
        raise ProtocolError(f"Method not supported: {method}", -32601)

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
                error = {"code": getattr(exc, "code", -32603), "message": str(exc)}
                if getattr(exc, "data", None) is not None:
                    error["data"] = exc.data
                await self.send({"jsonrpc": "2.0", "id": request_id,
                                 "error": error})
            else:
                print(f"[aelix-acp] {exc}", file=sys.stderr)

    async def run(self):
        reader = asyncio.StreamReader(limit=16 * 1024 * 1024)
        protocol = asyncio.StreamReaderProtocol(reader)
        loop = asyncio.get_running_loop()
        await loop.connect_read_pipe(lambda: protocol, sys.stdin.buffer)
        run_task = asyncio.current_task()
        installed_signals = []
        for sig in (signal.SIGTERM, signal.SIGHUP):
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
            for task in list(self.tasks):
                if not task.cancelling():
                    task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            await asyncio.gather(*(s.close() for s in self.sessions.values()), return_exceptions=True)
            self.db.close()
            for sig in installed_signals:
                loop.remove_signal_handler(sig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--profile", choices=["coding", "analytics"], default="coding")
    parser.add_argument("--aelix", default=shutil.which("aelix") or str(Path.home() / ".local/bin/aelix"))
    parser.add_argument("--analytics-extension", default=os.environ.get("AELIX_ANALYTICS_EXTENSION"))
    parser.add_argument("--state-dir", default=os.environ.get("AELIX_ACP_STATE_DIR", str(Path.home() / ".aelix/acp")))
    parser.add_argument("--provider")
    parser.add_argument("--model")
    parser.add_argument("--permission-mode", choices=sorted(MODE_IDS), default="default",
                        help="Initial mode for new sessions; saved sessions restore their recorded mode")
    args = parser.parse_args()
    if sys.platform == "win32":
        parser.error("The permission bridge requires Unix sockets; use macOS or Linux.")
    try:
        asyncio.run(Server(args).run())
    except (KeyboardInterrupt, BrokenPipeError, asyncio.CancelledError):
        pass


if __name__ == "__main__":
    main()
