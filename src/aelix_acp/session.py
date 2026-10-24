"""Newline JSON-RPC ACP server backed by real `aelix --mode rpc` children."""
from __future__ import annotations

import base64
import binascii
import codecs
import asyncio
import contextlib
import json
import hmac
import hashlib
import secrets
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

from .wire.errors import ProtocolError, BackendError, require_object, require_string
from .wire.process import terminate_owned
from .wire.transport import StdoutWriter
from .runtime.controls import SessionControls, runtime_profile
from .runtime.policy import permission_decision
from .runtime.store import save_preferences, load_preferences, append_event, begin_turn, finish_turn
from .runtime.commands import available_commands, execute as execute_slash
from .runtime.extensions import extension_reference
from .runtime.leases import acquire as acquire_lease
from .runtime.compat import ExtensionControls
from .runtime.preview import profile_flags, resource_flags

PACKAGE = Path(__file__).resolve().parent
KINDS = {"read": "read", "grep": "search", "find": "search", "ls": "read",
         "write": "edit", "edit": "edit", "bash": "execute", "aelix_status": "read",
         "analytics_status": "read", "analytics_query": "read"}
UNSET = object()



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
        self.tasks = set()
        self.done = None
        self.failure = None
        self.rpc_closed = False
        self.cancelled = False
        self.tools = {}
        self.tool_arrived = asyncio.Event()
        self.recovery_warnings = []
        self.native_commands = set()
        self.preview_commands = []
        self.permissions = set()
        self.permission_connections = set()
        self.ui_tasks = set()
        self.management_task = None
        self._close_task = None
        self.lease = None
        self.index_snapshot = None
        self.client_mcp = []  # Runtime-only: server URLs, environment and headers may contain credentials.
        self.state = {}
        self.socket_dir = None
        self.socket_server = None
        self.permission_token = None
        self.compat = None
        self.controls = SessionControls(self)
        self.mutation_lock = asyncio.Lock()
        self.restore_launch_preferences()
        self.analytics_project = Path(cwd) if (Path(cwd) / "analytics.json").is_file() else None

    def restore_launch_preferences(self):
        preferences = load_preferences(self.server.db, self.id)
        self.preview_resources = preferences.get("previewResources", {})
        self.preview_profile = preferences.get("previewProfile")
        self.active_tools = preferences.get("activeTools")
        if self.active_tools is not None and (not isinstance(self.active_tools, list) or any(not isinstance(name, str) for name in self.active_tools)):
            raise ProtocolError("Invalid saved tool selection")
        self.extra_extensions = preferences.get("extensions", [])
        self.trust_project = preferences.get("trustProject", getattr(self.server.args, "trust_project", None))
        self.agents_enabled = preferences.get("agentsEnabled", getattr(self.server.args, "enable_agents", False))
        if self.trust_project is not None and type(self.trust_project) is not bool:
            raise ProtocolError("Invalid saved workspace trust")
        if type(self.agents_enabled) is not bool:
            raise ProtocolError("Invalid saved delegation setting")
        if not isinstance(self.extra_extensions, list) or any(not isinstance(path, str) for path in self.extra_extensions):
            raise ProtocolError("Invalid stored extension configuration")

    async def start(self, session_file=None):
        if self.lease is None:
            self.lease = acquire_lease(self.server.args.state_dir, self.id)
        if self.index_snapshot is not None:
            current = self.server.db.execute("SELECT cwd,profile,file FROM sessions WHERE id=?", (self.id,)).fetchone()
            if current != self.index_snapshot:
                raise ProtocolError("Saved session changed before it could be opened; load it again", -32000)
            self.index_snapshot = None
        self.restore_launch_preferences()  # Re-read under the acquired lease, not the constructor's stale snapshot.
        self.recovery_warnings = []
        if runtime_profile(self) == "control-v1" and (self.preview_profile or self.preview_resources):
            raise ProtocolError("Saved beta.2 resource/profile selections require explicit migration before control-v1", -32004)
        if runtime_profile(self) == "beta2":
            preferences = load_preferences(self.server.db, self.id)
            if preferences.get("agent"):
                raise ProtocolError("Saved tool/profile restrictions require the control-v1 runtime profile", -32004,
                                    {"kind": "runtime_capability_required", "capability": "tools/set_active"})
            if self.client_mcp:
                raise ProtocolError("Client-supplied MCP servers require runtime control v1", -32004,
                                    {"kind": "runtime_capability_required", "capability": "mcp/configure"})
            if getattr(self.server.args, "enable_agents", False):
                raise ProtocolError("ACP delegation requires the control-v1 runtime profile", -32004)
            if self.agents_enabled:
                self.agents_enabled = False
                self.recovery_warnings.append({"kind": "delegation_unavailable",
                    "message": "Saved delegation is disabled in the beta.2 preview."})
        self.socket_dir = tempfile.mkdtemp(prefix="aelix-acp-")
        os.chmod(self.socket_dir, 0o700)
        self.permission_token = secrets.token_urlsafe(32)
        self.socket_server = await asyncio.start_server(self.permission, host="127.0.0.1", port=0, limit=16 * 1024 * 1024)
        address = self.socket_server.sockets[0].getsockname()
        endpoint = json.dumps({"host": "127.0.0.1", "port": address[1], "token": self.permission_token})
        args = [self.server.args.aelix, *getattr(self.server.args, "aelix_arg", []), "--mode", "rpc", "--offline", "--no-extensions",
                "--append-system-prompt-file", str(PACKAGE / "profiles" / f"{self.server.args.profile}.md")]
        if runtime_profile(self) == "beta2":
            args += profile_flags(self.preview_profile)
            args += resource_flags(self.preview_resources, self.cwd)
        args += ["--agents" if self.agents_enabled else "--no-agents"]
        if self.trust_project is not None:
            args += ["--approve" if self.trust_project else "--deny"]
        if self.server.args.profile == "analytics":
            extension = self.server.args.analytics_extension
            if not extension or not Path(extension).is_file():
                raise ProtocolError("Analytics extension is missing; pass --analytics-extension PATH")
            args += ["-e", str(Path(extension).resolve())]
        for extension in [*getattr(self.server.args, "extension", []), *self.extra_extensions]:
            args += ["-e", extension_reference(extension, self.cwd)]
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
        env = dict(os.environ, AELIX_ACP_ENDPOINT=endpoint, PYTHONUNBUFFERED="1",
                   AELIX_ACP_READY_FILE=str(ready_file), AELIX_ACP_NONCE=nonce)
        env.pop("AELIX_ACP_CONTROLS_FILE", None)
        controls_file = Path(self.socket_dir) / "controls.json"
        if runtime_profile(self) == "beta2":
            env["AELIX_ACP_CONTROLS_FILE"] = str(controls_file)
            env["AELIX_ACP_PROJECT_TRUSTED"] = str(self.trust_project is True).lower()
            env["AELIX_ACP_SELECTED_RESOURCES"] = json.dumps({
                "skills": [args[i + 1] for i, value in enumerate(args[:-1]) if value == "--skill"],
                "promptTemplates": [args[i + 1] for i, value in enumerate(args[:-1]) if value == "--prompt-template"]})
        self.proc = await asyncio.create_subprocess_exec(
            *args, cwd=self.cwd, env=env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=16 * 1024 * 1024,
            **({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32"
               else {"start_new_session": True}))
        self.tasks = {asyncio.create_task(self.read_rpc()), asyncio.create_task(self.read_stderr())}
        self.state = await self.rpc("get_state")
        if not ready_file.is_file() or ready_file.read_text() != nonce:
            raise ProtocolError("The tool approval bridge did not load; refusing this session", -32603)
        self.compat = None
        if runtime_profile(self) == "beta2" and controls_file.is_file() and not controls_file.is_symlink():
            address = json.loads(controls_file.read_text())
            if (address.get("version") != 1 or address.get("host") != "127.0.0.1" or address.get("nonce") != nonce or
                    type(address.get("port")) is not int or not 0 < address["port"] < 65536):
                raise ProtocolError("Invalid extension controls endpoint", -32603)
            self.compat = ExtensionControls(address, self.permission_token)
        if runtime_profile(self) == "beta2" and self.compat is None and (self.active_tools is not None or self.preview_profile or self.preview_resources):
            raise ProtocolError("Saved restrictions require the beta.2 extension controls bridge", -32004)
        await self.controls.refresh()
        if self.client_mcp:
            if "mcp/configure" not in self.controls.capabilities.get("methods", []):
                raise ProtocolError("This Aelix runtime cannot accept client MCP servers; update its control API", -32004)
            await self.configure_mcp(self.client_mcp)
        preferences = load_preferences(self.server.db, self.id)
        saved_agent = preferences.get("agent")
        profile_failed = False
        if saved_agent and "agents/use" in self.controls.capabilities.get("methods", []):
            try:
                await self.rpc("aelix_control", method="agents/use", params={"name": saved_agent})
            except BackendError:
                await self.rpc("aelix_control", method="agents/use", params={"name": "--none"})
                profile_failed = True
                self.recovery_warnings.append({"kind": "agent_unavailable", "previousValue": saved_agent,
                    "message": f"Saved agent {saved_agent} could not be applied; resumed without that profile. Select an available profile before continuing."})
            await self.controls.refresh()
        config = preferences.get("config", {})
        require_object(config, "saved session config")
        for config_id in ("model", "thinking_level", "steeringMode", "followUpMode", "autoCompactionEnabled", "autoRetryEnabled"):
            option = next((entry for entry in self.controls.options() if entry["id"] == config_id), None)
            if config_id in config and (not option or config[config_id] != option["currentValue"]):
                if not option or config[config_id] not in [entry["value"] for entry in option["options"]]:
                    self.recovery_warnings.append({"kind": "config_unavailable", "configId": config_id,
                        "previousValue": config[config_id], "message": f"Saved {config_id} is unavailable; using the native runtime's current selection."})
                else:
                    await self.controls.set(config_id, config[config_id], publish=False)
        restored = preferences.get("permission_mode", "default")
        if profile_failed and restored != "plan":
            restored = "default"  # Never recover a missing restricted identity into silent writes.
        if restored not in {entry[0] for entry in self.controls.permission_modes()}:
            self.recovery_warnings.append({"kind": "permission_mode_unavailable", "previousValue": restored,
                "message": "The saved permission mode is unavailable; every tool now requires explicit approval."})
            restored = "default"
        if restored in {entry[0] for entry in self.controls.permission_modes()}:
            if self.controls.capabilities.get("permissionModes"):
                await self.rpc("aelix_control", method="permissions/set", params={"mode": restored})
            self.controls.permission_mode = restored
        await self.restore_tools()
        metadata = getattr(self.server, "client_capabilities", {}).get("_meta", {})
        if metadata.get("aelix/clientActions") is True and "ui/enable" in self.controls.capabilities.get("methods", []):
            await self.rpc("aelix_control", method="ui/enable", params={"enabled": True})
        self.save()

    async def restore_tools(self):
        if self.active_tools is not None and self.compat is not None:
            await self.compat.call("tools/set_active", {"names": self.active_tools})
            return
        if self.active_tools is not None and "tools/set_active" in self.controls.capabilities.get("methods", []):
            resources = await self.rpc("aelix_control", method="resources/list", params={})
            available = {tool["name"] for tool in resources.get("tools", [])}
            selected = [name for name in self.active_tools if name in available]
            await self.rpc("aelix_control", method="tools/set_active", params={"names": selected})

    async def configure_mcp(self, servers):
        before = await self.rpc("aelix_control", method="resources/list", params={})
        active = before.get("activeTools", [])
        available = {tool["name"] for tool in before.get("tools", [])}
        follow_all = self.active_tools is None and set(active) == available and await self.profile_uses_all_tools()
        desired = self.active_tools if self.active_tools is not None else active
        # Profile selection also installs native filters. Clear the actual
        # live filter for every idle reconfiguration, not just GUI selections.
        await self.rpc("aelix_control", method="tools/set_active", params={"names": []})
        try:
            result = await self.rpc("aelix_control", method="mcp/configure", params={"servers": servers}, _timeout=60)
        except ProtocolError:
            await self.rpc("aelix_control", method="tools/set_active", params={"names": active})
            raise
        self.client_mcp = servers
        after = await self.rpc("aelix_control", method="resources/list", params={})
        names = [tool["name"] for tool in after.get("tools", [])]
        selected = names if follow_all else [name for name in desired if name in names]
        await self.rpc("aelix_control", method="tools/set_active", params={"names": selected})
        return result

    async def profile_uses_all_tools(self):
        agent = self.controls.capabilities.get("activeAgent")
        if not agent:
            return True
        try:
            profile = (await self.rpc("aelix_control", method="agents/show", params={"name": agent}))["profile"]
            return "tools" in profile and profile["tools"] is None
        except (ProtocolError, KeyError, TypeError):
            return False

    async def sync_profile_tools(self):
        # Only explicit GUI/slash tool choices are persistent overrides.
        # A profile owns its live filter and must be re-evaluated after edits.
        self.active_tools = None

    def save(self):
        self.server.db.execute(
            "INSERT OR REPLACE INTO sessions VALUES(?,?,?,?,?,?)",
            (self.id, self.cwd, self.server.args.profile, self.state.get("sessionFile"),
             self.state.get("sessionName") or f"Aelix {self.server.args.profile}", int(time.time())))
        save_preferences(self.server.db, self.id, {"permission_mode": self.controls.permission_mode, "extensions": self.extra_extensions,
            "config": {option["id"]: option["currentValue"] for option in self.controls.options()},
            "agent": self.controls.capabilities.get("activeAgent"),
            "activeTools": self.active_tools,
            "previewResources": self.preview_resources, "previewProfile": self.preview_profile,
            "trustProject": self.trust_project, "agentsEnabled": self.agents_enabled,
            "backendMessageCount": self.state.get("messageCount", self.state.get("message_count", 0))})
        self.server.db.commit()

    def selectors(self):
        result = self.controls.selectors()
        if self.recovery_warnings:
            result["_meta"] = {"aelix/recoveryWarnings": self.recovery_warnings}
        return result

    async def publish_recovery_warnings(self):
        for warning in self.recovery_warnings:
            await self.update({"sessionUpdate": "agent_message_chunk", "content": {
                "type": "text", "text": "Session recovery: " + warning["message"]}})

    async def rpc(self, command, *, _timeout=45, **kwargs):
        request_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            if self.rpc_closed:
                raise ProtocolError(self.failure or "Aelix RPC stream is closed", -32603)
            if not self.proc or self.proc.returncode is not None:
                raise ProtocolError("Aelix child is not running", -32603)
            self.proc.stdin.write((json.dumps({"id": request_id, "type": command, **kwargs}) + "\n").encode())
            await self.proc.stdin.drain()
            try:
                return await asyncio.wait_for(future, _timeout)
            except TimeoutError:
                if command == "aelix_control" and kwargs.get("method") in {"commands/run", "auth/login", "resources/reload", "agents/run"}:
                    with contextlib.suppress(ProtocolError, TimeoutError):
                        await self.rpc("aelix_control", method="commands/cancel", params={}, _timeout=5)
                elif command == "compact":
                    with contextlib.suppress(ProtocolError, TimeoutError):
                        await self.rpc("abort", _timeout=5)
                raise ProtocolError(f"Aelix RPC {command} timed out", -32007, {"kind": "timeout", "command": command}) from None
        finally:
            self.pending.pop(request_id, None)

    async def read_stderr(self):
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        writer = StdoutWriter(sys.stderr.fileno())
        try:
            while chunk := await self.proc.stderr.read(4096):
                text = f"[aelix:{self.id[:8]}] " + decoder.decode(chunk)
                await writer.write(text.encode("utf-8"))
        finally:
            writer.close()

    async def read_rpc(self):
        try:
            while line := await self.proc.stdout.readline():
                try:
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise ValueError("RPC frame must be an object")
                except ValueError:
                    print("[aelix-acp] Ignored non-JSON child stdout", file=sys.stderr)
                    continue
                future = self.pending.get(event.get("id"))
                if event.get("type") == "response" and future is not None:
                    if not future.done():
                        if event.get("success"):
                            future.set_result(event.get("data") or {})
                        else:
                            future.set_exception(BackendError(str(event.get("error", "RPC failed")), event.get("command", "unknown")))
                else:
                    await self.event(event)
        except Exception as exc:
            self.failure = f"Aelix stream failed: {exc}"
        finally:
            self.rpc_closed = True
            self.tool_arrived.set()
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ProtocolError("Aelix process closed its RPC stream", -32603))
            if self.done and not self.done.done():
                self.failure = self.failure or "Aelix process closed during a turn"
                self.done.set_result(None)
            if self._close_task is None:
                task = asyncio.create_task(self.close())
                self.server.cleanup_tasks.add(task)
                task.add_done_callback(self.server.cleanup_tasks.discard)

    async def event(self, event):
        kind = event.get("type")
        if kind == "message_start" and (event.get("message") or {}).get("role") == "assistant":
            self.failure = None
        elif kind == "message_update":
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
                known = call_id in self.tools
                self.tools[call_id] = call
                self.tool_arrived.set()
                await self.update({"sessionUpdate": "tool_call_update" if known else "tool_call", **call})
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
        elif kind == "aelix_subagent":
            await self.server.send({"jsonrpc": "2.0", "method": "_aelix/subagent",
                                    "params": {"sessionId": self.id, "event": event.get("event"), "data": event.get("data", {})}})
        elif kind == "extension_ui_request":
            task = asyncio.create_task(self.handle_ui(event))
            self.tasks.add(task)
            self.ui_tasks.add(task)
            task.add_done_callback(self.tasks.discard)
            task.add_done_callback(self.ui_tasks.discard)

    async def update(self, update):
        append_event(self.server.db, self.id, update)
        await self.server.send({"jsonrpc": "2.0", "method": "session/update",
                                "params": {"sessionId": self.id, "update": update}})

    async def permission(self, reader, writer):
        if len(self.permission_connections) >= 32:
            writer.close()
            await writer.wait_closed()
            return
        owner = asyncio.current_task()
        self.permission_connections.add(owner)
        allowed = False
        channel_error = None
        try:
            async with asyncio.timeout(300):
                request = json.loads(await reader.readline())
                if self.permission_token is not None:
                    token = request.get("token")
                    if not isinstance(token, str) or not hmac.compare_digest(token, self.permission_token):
                        return
                    while request.get("toolCallId") not in self.tools:
                        self.tool_arrived.clear()
                        if self.cancelled or self.rpc_closed:
                            return
                        await self.tool_arrived.wait()
                    existing = self.tools[request["toolCallId"]]
                    if existing.get("title") != request.get("name"):
                        return
                decision = permission_decision(self.controls.permission_mode, request.get("provenance"),
                                               write_allowed=request.get("writeAllowed") is True)
                if self.controls.permission_mode == "auto" and request.get("provenance") == "bash":
                    if "permissions/evaluate" in self.controls.capabilities.get("methods", []):
                        evaluation = await self.rpc("aelix_control", method="permissions/evaluate", params={"args": request.get("args", {})})
                        decision = {"allow": "allow", "deny": "deny"}.get(evaluation.get("decision"), "ask")
                if self.cancelled or decision == "deny":
                    return
                if decision == "allow":
                    allowed = True
                    return
                call = tool_call(request["toolCallId"], request["name"], request["args"], self.cwd)
                known = call["toolCallId"] in self.tools
                self.tools[call["toolCallId"]] = call
                await self.update({"sessionUpdate": "tool_call_update" if known else "tool_call", **call})
                if not self.cancelled:
                    permission_task = asyncio.current_task()
                    self.permissions.add(permission_task)
                    try:
                        answer = await self.server.client_request("session/request_permission", {
                            "sessionId": self.id, "toolCall": call,
                            "options": [{"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
                                        {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"}]})
                        outcome = answer.get("outcome") or {}
                        allowed = outcome.get("outcome") == "selected" and outcome.get("optionId") == "allow-once"
                        if allowed and not self.cancelled:
                            await self.update({"sessionUpdate": "tool_call_update",
                                               "toolCallId": call["toolCallId"], "status": "in_progress"})
                    finally:
                        self.permissions.discard(permission_task)
        except (Exception, asyncio.CancelledError) as exc:
            allowed = False
            channel_error = None if self.cancelled else type(exc).__name__
        finally:
            self.permission_connections.discard(owner)
            with contextlib.suppress(ConnectionError):
                writer.write((json.dumps({"allow": allowed and not self.cancelled,
                                         **({"error": channel_error} if channel_error else {})}) + "\n").encode())
                await writer.drain()
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    async def commands(self):
        data = await self.rpc("get_commands")
        dynamic = data.get("commands", [])
        if self.server.args.profile == "analytics" and not any(c.get("name") == "analytics" for c in dynamic):
            raise ProtocolError("The Analytics extension did not register its command; refusing this profile", -32603)
        self.native_commands = {command["name"] for command in dynamic}
        self.preview_commands = (await self.compat.call("commands/list"))["commands"] if self.compat else []
        catalog = available_commands(self, dynamic)
        await self.update({"sessionUpdate": "available_commands_update", "availableCommands": catalog})

    async def handle_ui(self, event):
        method = event.get("method")
        if method == "authProgress":
            await self.server.send({"jsonrpc": "2.0", "method": "_aelix/notification",
                                    "params": {"sessionId": self.id, "event": event}})
            return
        if method in {"notify", "setStatus", "setWidget", "setTitle"}:
            text = event.get("message") or event.get("text")
            if text:
                await self.update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": str(text)}})
            return
        answer = {"cancelled": True}
        try:
            answer = await self.server.client_action(self, "dialog", event)
        except (ProtocolError, asyncio.CancelledError):
            pass
        if self.proc and self.proc.returncode is None:
            self.proc.stdin.write((json.dumps({"type": "extension_ui_response", "id": event.get("id"), **answer}) + "\n").encode())
            await self.proc.stdin.drain()

    async def restart(self, *, extensions=None, trust_project=None, agents_enabled=None,
                      preview_profile=UNSET, preview_resources=None, reset_config=False):
        if self.id in self.server.starting_ids:
            raise ProtocolError("Session is already starting", -32000)
        self.server.starting_ids.add(self.id)
        held_lease = self.lease
        self.lease = None
        try:
            filename = self.state.get("sessionFile")
            if filename and not Path(filename).is_file():
                filename = None
            await self.close()
            self.lease, held_lease = held_lease, None
            self.tasks = set()
            self._close_task = None
            self.failure = None
            self.rpc_closed = False
            self.cancelled = False
            if extensions is not None:
                self.extra_extensions = extensions
            if trust_project is not None:
                self.trust_project = trust_project
            if agents_enabled is not None:
                self.agents_enabled = agents_enabled
            if preview_profile is not UNSET:
                self.preview_profile = preview_profile
            if preview_resources is not None:
                self.preview_resources = preview_resources
            if reset_config:
                self.active_tools = None
            self.save()
            if reset_config:
                preferences = load_preferences(self.server.db, self.id)
                preferences["config"] = {}
                save_preferences(self.server.db, self.id, preferences)
                self.server.db.commit()
            await self.start(filename)
            await self.commands()
            await self.publish_recovery_warnings()
            self.server.sessions[self.id] = self
        except BaseException:
            if held_lease is not None:
                self.lease, held_lease = held_lease, None
            await self.close()
            raise
        finally:
            self.server.starting_ids.discard(self.id)

    async def prompt(self, blocks, request_key=None):
        key = request_key or uuid.uuid4().hex
        if not isinstance(key, str) or not key or len(key) > 256:
            raise ProtocolError("Request id must be a non-empty string of at most 256 characters")
        fingerprint = hashlib.sha256(json.dumps(blocks, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
        prior = begin_turn(self.server.db, self.id, key, fingerprint)
        if prior is not None:
            return prior
        result, error = None, None
        try:
            result = await self._prompt(blocks)
            return result
        except ProtocolError as failure:
            error = failure
            append_event(self.server.db, self.id, {"sessionUpdate": "agent_message_chunk",
                         "content": {"type": "text", "text": "Request failed: " + str(failure)}})
            raise
        finally:
            finish_turn(self.server.db, self.id, key, result, error)

    async def _prompt(self, blocks):
        if self.done and not self.done.done():
            raise ProtocolError("A prompt is already running in this session")
        self.cancelled = False
        if not isinstance(blocks, list) or not blocks:
            raise ProtocolError("prompt must be a non-empty content block list")
        parts = []
        images = []
        for block in blocks:
            require_object(block, "content block")
            if block.get("type") == "text":
                if not isinstance(block.get("text"), str):
                    raise ProtocolError("Text blocks require a string")
                parts.append(block["text"])
            elif block.get("type") == "image":
                if block.get("mimeType") not in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
                    raise ProtocolError("Unsupported image MIME type")
                data = block.get("data")
                if not isinstance(data, str) or len(data) > 12 * 1024 * 1024:
                    raise ProtocolError("Image data must be base64 and fit in 8 MiB decoded")
                try:
                    decoded = base64.b64decode(data, validate=True)
                except (ValueError, binascii.Error):
                    raise ProtocolError("Invalid image base64") from None
                if len(decoded) > 8 * 1024 * 1024:
                    raise ProtocolError("Image exceeds 8 MiB")
                images.append({"type": "image", "data": data, "mimeType": block["mimeType"]})
            elif block.get("type") == "resource":
                resource = require_object(block.get("resource"), "embedded resource")
                if not isinstance(resource.get("text"), str):
                    raise ProtocolError("Embedded resource text must be a string")
                parts.append(f"{resource.get('uri', 'resource')}:\n{resource['text']}")
            elif block.get("type") == "resource_link":
                uri = require_string(block.get("uri"), "resource URI")
                parts.append(f"{block.get('name', 'File')}: {uri}")
            else:
                raise ProtocolError(f"Unsupported prompt block: {block.get('type')}")
        text = "\n\n".join(parts)
        if not text.strip() and not images:
            raise ProtocolError("Prompt content is empty")
        for block in blocks:
            append_event(self.server.db, self.id, {"sessionUpdate": "user_message_chunk", "content": block})
        self.server.db.commit()
        if text.startswith("/") and all(block.get("type") == "text" for block in blocks):
            try:
                handled, prepared = await execute_slash(self, text)
            except ProtocolError:
                if self.cancelled:
                    return {"stopReason": "cancelled"}
                raise
            if handled and prepared is None:
                return {"stopReason": "end_turn"}
            if prepared is not None:
                text = prepared
        if self.cancelled:
            return {"stopReason": "cancelled"}
        self.done = asyncio.get_running_loop().create_future()
        self.failure = None
        self.tools.clear()
        try:
            timeout = getattr(self.server.args, "turn_timeout", 600)
            async with asyncio.timeout(timeout or None):
                await self.rpc("prompt", message=text, **({"images": images} if images else {}))
                if self.cancelled:
                    await self.cancel()
                await self.wait_until_idle()
            if self.failure and not self.cancelled:
                raise ProtocolError(self.failure, -32603)
            self.save()
            return {"stopReason": "cancelled" if self.cancelled else "end_turn"}
        except TimeoutError:
            await self.cancel()
            raise ProtocolError("Aelix turn timed out", -32603)
        finally:
            if self.done and not self.done.done():
                self.done.cancel()

    async def wait_until_idle(self):
        # agent_end closes one provider attempt, not the enclosing prompt's
        # retries, overflow recovery or threshold compaction (#334 in native).
        while True:
            self.state = await self.rpc("get_state")
            if not self.state.get("isStreaming", self.state.get("is_streaming", False)):
                return
            if self.cancelled:
                # Native acceptance precedes before_agent_start/build_context.
                # An earlier abort can precede the turn task's creation.
                with contextlib.suppress(ProtocolError, TimeoutError):
                    await self.rpc("abort", _timeout=5)
            await asyncio.sleep(0.05)

    async def cancel(self):
        self.cancelled = True
        self.tool_arrived.set()
        if self.compat is not None:
            self.compat.cancel_pending()
        if self.management_task and not self.management_task.done():
            self.management_task.cancel()
        for task in [*self.permission_connections, *self.permissions, *self.ui_tasks]:
            task.cancel()
        if "commands/cancel" in self.controls.capabilities.get("methods", []) and self.proc and self.proc.returncode is None:
            with contextlib.suppress(ProtocolError, TimeoutError):
                await self.rpc("aelix_control", method="commands/cancel", params={}, _timeout=5)
        if self.proc and self.proc.returncode is None:
            with contextlib.suppress(ProtocolError, TimeoutError):
                await self.rpc("abort", _timeout=5)

    async def close(self):
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
            self.server.cleanup_tasks.add(self._close_task)
            self._close_task.add_done_callback(self.server.cleanup_tasks.discard)
        await asyncio.shield(self._close_task)

    async def _close(self):
        if self.compat is not None:
            self.compat.cancel_pending()
        for task in list(self.permission_connections):
            task.cancel()
        await asyncio.gather(*self.permission_connections, return_exceptions=True)
        for task in list(self.permissions):
            task.cancel()
        owned_tasks = [task for task in self.tasks if task is not asyncio.current_task()]
        for task in owned_tasks:
            task.cancel()
        await asyncio.gather(*owned_tasks, return_exceptions=True)
        if self.proc and self.proc.returncode is None:
            async def discard(stream):
                while await stream.read(65536):
                    pass
            drains = [asyncio.create_task(discard(stream)) for stream in (self.proc.stdout, self.proc.stderr)]
            try:
                await terminate_owned(self.proc)
            finally:
                for task in drains:
                    task.cancel()
                await asyncio.gather(*drains, return_exceptions=True)
        if self.socket_server:
            self.socket_server.close()
            await self.socket_server.wait_closed()
        if self.lease:
            self.lease.close()
            self.lease = None
        if self.socket_dir:
            shutil.rmtree(self.socket_dir, ignore_errors=True)
        if self.server.sessions.get(self.id) is self:
            self.server.sessions.pop(self.id, None)
