"""Newline JSON-RPC ACP server backed by real `aelix --mode rpc` children."""
from __future__ import annotations

import argparse
import base64
import sqlite3
import asyncio
import contextlib
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import uuid
import time
import math

from . import __version__
from .wire.errors import ProtocolError, require_object, require_string
from .runtime.store import open_store, load_preferences, history
from .wire.transport import decode_frame, stdin_reader, StdoutWriter
from .runtime.extensions import run_extension_action, require_consent

from .session import Session, text_content, tool_call

__all__ = ["Server", "Session", "ProtocolError", "main", "tool_call", "text_content"]


class Server:
    def __init__(self, args):
        self.args = args
        self.db = open_store(args.state_dir)
        self.initialized = False
        self.active_request_ids = set()
        self.sessions = {}
        self.starting_ids = set()
        self.pending = {}
        self.tasks = set()
        self.cleanup_tasks = set()
        self.output_lock = asyncio.Lock()
        self.output = None

    async def send(self, message):
        async with self.output_lock:
            raw = (json.dumps(message, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
            if self.output is None:
                self.output = StdoutWriter()
            await self.output.write(raw)

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
            return self.sessions[require_string(params.get("sessionId"), "sessionId")]
        except KeyError:
            raise ProtocolError("Unknown session; use session/load before prompting") from None

    async def client_action(self, session, action, arguments):
        metadata = getattr(self, "client_capabilities", {}).get("_meta") or {}
        if metadata.get("aelix/clientActions") is not True:
            raise ProtocolError(f"/{action} requires Aelix Studio's client-action integration", -32004,
                                {"kind": "client_action_required", "action": action})
        return await self.client_request("_aelix/client_action", {"sessionId": session.id, "action": action, "arguments": arguments})

    async def extension_action(self, session, arguments):
        return await run_extension_action(self, session, arguments)

    async def branch_session(self, session, arguments):
        if "sessions/branch" not in session.controls.capabilities.get("methods", []):
            raise ProtocolError("Aelix runtime does not support non-destructive branching", -32004)
        result = await session.rpc("aelix_control", method="sessions/branch", params=arguments,
                                  _timeout=getattr(self.args, "command_timeout", 600) or None)
        if result.get("cancelled"):
            return result
        filename = result.get("sessionFile")
        if not isinstance(filename, str) or not Path(filename).is_file():
            raise ProtocolError("Aelix did not persist the new branch", -32603)
        identifier = str(uuid.uuid4())
        preferences = load_preferences(self.db, session.id)
        preferences.pop("config", None)
        preferences["backendMessageCount"] = 1
        from .runtime.store import save_preferences
        with self.db:
            self.db.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?)", (identifier, session.cwd, self.args.profile,
                            filename, result.get("title") or "Aelix branch", int(time.time())))
            save_preferences(self.db, identifier, preferences)
        await session.controls.refresh()
        session.save()
        return {"sessionId": identifier, "parentSessionId": session.id}

    async def dispatch(self, method, params):
        require_object(params)
        if method == "initialize":
            if self.initialized:
                raise ProtocolError("Already initialized", -32600)
            if type(params.get("protocolVersion")) is not int or params.get("protocolVersion") < 1:
                raise ProtocolError("protocolVersion must be a positive integer")
            capabilities = params.get("clientCapabilities", {})
            require_object(capabilities, "clientCapabilities")
            if capabilities.get("_meta") is not None:
                require_object(capabilities["_meta"], "clientCapabilities._meta")
            self.initialized = True
            self.client_capabilities = capabilities
            return {"protocolVersion": 1,
                    "agentInfo": {"name": "aelix-acp", "title": f"Aelix {self.args.profile.title()}", "version": __version__},
                    "agentCapabilities": {"loadSession": True, "promptCapabilities": {"embeddedContext": True, "image": True},
                                          "mcpCapabilities": {"http": True, "sse": True},
                                          "sessionCapabilities": {"list": {}, "delete": {}}}, "_meta": {"aelix/control": {"version": 1, "methods": ["_aelix/capabilities", "_aelix/manage"],
                                "clientActionCapability": "aelix/clientActions"}}, "authMethods": []}
        if method in ("session/new", "session/load"):
            cwd = str(Path(require_string(params.get("cwd"), "cwd")).expanduser().resolve())
            if not isinstance(params.get("mcpServers", []), list):
                raise ProtocolError("mcpServers must be an array")
            if not Path(cwd).is_dir():
                raise ProtocolError("Session cwd must be an existing directory")
            session_file = None
            if method == "session/load":
                session_id = require_string(params.get("sessionId"), "sessionId")
                row = self.db.execute("SELECT cwd,profile,file FROM sessions WHERE id=?", (session_id,)).fetchone()
                if not row or row[0] != cwd or row[1] != self.args.profile:
                    raise ProtocolError("Saved session does not match this workspace and profile")
                session_file = row[2]
                if not session_file or not Path(session_file).is_file():
                    preferences = load_preferences(self.db, session_id)
                    if preferences.get("backendMessageCount") == 0:
                        session_file = None
                    else:
                        raise ProtocolError("No persisted Aelix session file is available")
            else:
                session_id = str(uuid.uuid4())
            if session_id in self.starting_ids:
                raise ProtocolError("Session is already starting", -32000)
            if session_id not in self.sessions and len(self.sessions) + len(self.starting_ids) >= getattr(self.args, "max_sessions", 16):
                raise ProtocolError("Session limit reached; close a session first", -32000)
            previous = self.sessions.get(session_id)
            if previous and previous.mutation_lock.locked():
                raise ProtocolError("Session is busy; cancel its turn before loading", -32000)
            self.starting_ids.add(session_id)
            session = None
            try:
                if previous:
                    self.sessions.pop(session_id, None)
                    await previous.close()
                session = Session(self, session_id, cwd)
                session.client_mcp = params.get("mcpServers", [])
                await session.start(session_file)
                self.sessions[session_id] = session
                await session.commands()
                if method == "session/load":
                    recorded = list(history(self.db, session_id))
                    for update in recorded:
                        await self.send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": session_id, "update": update}})
                    backend_history = {} if recorded else await session.rpc("get_messages")
                    for message in backend_history.get("messages", []):
                        if message.get("role") in ("user", "assistant"):
                            text = text_content(message.get("content"))
                            if text:
                                await session.update({"sessionUpdate": "user_message_chunk" if message["role"] == "user"
                                    else "agent_message_chunk", "content": {"type": "text", "text": text}})
            except BaseException:
                self.sessions.pop(session_id, None)
                if session:
                    await session.close()
                raise
            finally:
                self.starting_ids.discard(session_id)
            return {**session.selectors(), **({"sessionId": session_id} if method.endswith("new") else {})}
        if method == "session/delete":
            session_id = require_string(params.get("sessionId"), "sessionId")
            if session_id in self.starting_ids:
                raise ProtocolError("Session is starting", -32000)
            row = self.db.execute("SELECT profile FROM sessions WHERE id=?", (session_id,)).fetchone()
            if row is None or row[0] != self.args.profile:
                raise ProtocolError("Unknown session")
            live = self.sessions.get(session_id)
            if live and live.mutation_lock.locked():
                raise ProtocolError("Session is busy; cancel its turn before deleting", -32000)
            from .runtime.leases import acquire as acquire_lease
            lease = None if live else acquire_lease(self.args.state_dir, session_id)
            try:
                if live:
                    await live.close()
                with self.db:
                    self.db.execute("DELETE FROM preferences WHERE session_id=?", (session_id,))
                    self.db.execute("DELETE FROM sessions WHERE id=?", (session_id,))
                    self.db.execute("DELETE FROM events WHERE session_id=?", (session_id,))
                    self.db.execute("DELETE FROM turns WHERE session_id=?", (session_id,))
            finally:
                if lease:
                    lease.close()
            return {}
        if method == "session/list":
            clauses, values = ["profile=?"], [self.args.profile]
            if params.get("cwd"):
                clauses.append("cwd=?")
                values.append(str(Path(require_string(params["cwd"], "cwd")).expanduser().resolve()))
            if params.get("cursor"):
                try:
                    cursor = require_string(params["cursor"], "cursor")
                    if len(cursor) > 512:
                        raise ValueError()
                    updated, identifier = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
                    if type(updated) is not int or not isinstance(identifier, str):
                        raise ValueError()
                except (ValueError, TypeError):
                    raise ProtocolError("Invalid session cursor") from None
                clauses.append("(updated < ? OR (updated = ? AND id < ?))")
                values.extend([updated, updated, identifier])
            rows = self.db.execute("SELECT id,cwd,title,updated FROM sessions WHERE " + " AND ".join(clauses) +
                                   " ORDER BY updated DESC,id DESC LIMIT 101", values).fetchall()
            from datetime import datetime, timezone
            result = {"sessions": [{"sessionId": r[0], "cwd": r[1], "title": r[2],
                       "updatedAt": datetime.fromtimestamp(r[3], timezone.utc).isoformat()} for r in rows[:100]]}
            if len(rows) > 100:
                row = rows[99]
                result["nextCursor"] = base64.urlsafe_b64encode(json.dumps([row[3], row[0]]).encode()).decode().rstrip("=")
            return result
        if method == "session/prompt":
            session = self.session(params)
            if session.mutation_lock.locked():
                raise ProtocolError("Session is busy", -32000)
            async with session.mutation_lock:
                metadata = params.get("_meta") or {}
                require_object(metadata, "_meta")
                return await session.prompt(params.get("prompt"), metadata.get("aelix/requestId"))
        if method == "session/cancel":
            await self.session(params).cancel()
            return {}
        if method in ("session/set_mode", "session/set_model", "session/set_config_option"):
            session = self.session(params)
            config_id, value = {
                "session/set_mode": ("permission_mode", params.get("modeId")),
                "session/set_model": ("model", params.get("modelId")),
                "session/set_config_option": (params.get("configId"), params.get("value")),
            }[method]
            if session.mutation_lock.locked():
                raise ProtocolError("Session is busy", -32000)
            async with session.mutation_lock:
                result = await session.controls.set(config_id, value)
            return result if method == "session/set_config_option" else {}
        if method == "_aelix/capabilities":
            session = self.session(params)
            return {"version": 1, "runtime": session.controls.capabilities,
                    "configOptions": session.controls.options(), "clientActionMethod": "_aelix/client_action"}
        if method == "_aelix/manage":
            session = self.session(params)
            action = require_string(params.get("action"), "action")
            arguments = require_object(params.get("arguments", {}), "arguments")
            if session.mutation_lock.locked():
                raise ProtocolError("Session is busy", -32000)
            async with session.mutation_lock:
                if action == "sessions/close":
                    await session.close()
                    return {}
                if action == "sessions/branch":
                    return await self.branch_session(session, arguments)
                if action == "workspace/trust":
                    trusted = arguments.get("trusted")
                    if type(trusted) is not bool:
                        raise ProtocolError("trusted must be a boolean")
                    await require_consent(self, session, "Change project trust", {"cwd": session.cwd, "trusted": trusted})
                    await session.restart(trust_project=trusted)
                    return {"trusted": trusted}
                if action == "agents/enable":
                    enabled = arguments.get("enabled")
                    if type(enabled) is not bool:
                        raise ProtocolError("enabled must be a boolean")
                    await require_consent(self, session, "Change agent delegation", {"enabled": enabled})
                    await session.restart(agents_enabled=enabled)
                    return {"enabled": enabled}
                if action == "extensions/action":
                    return await self.extension_action(session, require_string(arguments["command"], "command") if "command" in arguments else arguments)
                allowed = {"resources/list", "resources/reload", "settings/get", "settings/set", "tools/set_active", "mcp/status", "mcp/configure", "auth/status", "auth/set_api_key", "auth/login", "auth/logout", "extensions/list", "agents/list", "agents/show", "agents/use", "agents/run"}
                if action not in allowed or action not in session.controls.capabilities.get("methods", []):
                    raise ProtocolError("Unsupported management action")
                result = await session.rpc("aelix_control", method=action, params=arguments,
                    _timeout=(getattr(self.args, "command_timeout", 600) or None) if action in {"auth/login", "resources/reload", "agents/run"} else 60)
                if action == "mcp/configure":
                    session.client_mcp = arguments["servers"]
                    await session.restore_tools()
                if action == "tools/set_active":
                    session.active_tools = result["activeTools"]
                    session.save()
                if action == "resources/reload":
                    await session.restore_tools()
                    await session.commands()
                if action == "agents/use":
                    await session.commands()
                if action in {"auth/set_api_key", "auth/login", "auth/logout", "resources/reload", "settings/set", "agents/use"}:
                    await session.controls.refresh()
                    session.save()
                    await session.update({"sessionUpdate": "config_option_update", "configOptions": session.controls.options()})
                return result
        if method == "authenticate":
            raise ProtocolError("Configure provider authentication using the existing Aelix CLI first")
        raise ProtocolError(f"Method not supported: {method}", -32601)

    async def handle(self, message):
        request_id = message.get("id")
        try:
            result = await self.dispatch(message["method"], message.get("params") or {})
            if request_id is not None:
                await self.send({"jsonrpc": "2.0", "id": request_id, "result": result})
        except Exception as exc:
            if request_id is not None:
                error = {"code": getattr(exc, "code", -32603), "message": str(exc)}
                if isinstance(exc, ProtocolError) and exc.data is not None:
                    error["data"] = exc.data
                await self.send({"jsonrpc": "2.0", "id": request_id, "error": error})
            else:
                print(f"[aelix-acp] {exc}", file=sys.stderr)
        finally:
            self.active_request_ids.discard(request_id)

    async def run(self):
        reader, input_transport = await stdin_reader()
        loop = asyncio.get_running_loop()
        run_task = asyncio.current_task()
        installed_signals = []
        for sig in [signal.SIGTERM, *([signal.SIGHUP] if hasattr(signal, "SIGHUP") else [])]:
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, run_task.cancel)
                installed_signals.append(sig)
        try:
            while line := await reader.readline():
                try:
                    message = decode_frame(line)
                except ProtocolError as error:
                    await self.send({"jsonrpc": "2.0", "id": None,
                                     "error": {"code": error.code, "message": str(error)}})
                    continue
                if "method" not in message:
                    future = self.pending.get(message.get("id"))
                    if future and not future.done():
                        if "error" in message:
                            future.set_exception(ProtocolError(str(message["error"])))
                        else:
                            result = message.get("result") or {}
                            if isinstance(result, dict):
                                future.set_result(result)
                            else:
                                future.set_exception(ProtocolError("Client response result must be an object"))
                    continue
                if not self.initialized and message["method"] not in ("initialize",):
                    if message.get("id") is not None:
                        await self.send({"jsonrpc": "2.0", "id": message["id"],
                                         "error": {"code": -32002, "message": "Call initialize first"}})
                    continue
                request_id = message.get("id")
                if request_id is not None and request_id in self.active_request_ids:
                    await self.send({"jsonrpc": "2.0", "id": request_id,
                                     "error": {"code": -32600, "message": "Duplicate active request id"}})
                    continue
                if len(self.tasks) >= 64 and message["method"] != "session/cancel":
                    if request_id is not None:
                        await self.send({"jsonrpc": "2.0", "id": request_id,
                                         "error": {"code": -32000, "message": "Too many concurrent requests"}})
                    continue
                if request_id is not None:
                    self.active_request_ids.add(request_id)
                task = asyncio.create_task(self.handle(message))
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)
        finally:
            if input_transport is not None:
                input_transport.close()
            for task in list(self.tasks):
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            await asyncio.gather(*(s.close() for s in self.sessions.values()), return_exceptions=True)
            await asyncio.gather(*list(self.cleanup_tasks), return_exceptions=True)
            self.db.close()
            if self.output:
                self.output.close()
            for sig in installed_signals:
                loop.remove_signal_handler(sig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--profile", choices=["coding", "analytics"], default="coding")
    parser.add_argument("--aelix", default=shutil.which("aelix") or str(Path.home() / ".local/bin/aelix"))
    parser.add_argument("--aelix-arg", action="append", default=[], help="Prefix argument for the Aelix executable (repeatable)")
    parser.add_argument("--analytics-extension", default=os.environ.get("AELIX_ANALYTICS_EXTENSION"))
    parser.add_argument("--state-dir", default=os.environ.get("AELIX_ACP_STATE_DIR", str(Path.home() / ".aelix/acp")))
    parser.add_argument("--max-sessions", type=int, default=16)
    parser.add_argument("--trust-project", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--enable-agents", action="store_true", help="Enable Aelix's bounded native delegation")
    parser.add_argument("--command-timeout", type=float, default=600, help="Seconds per extension command; 0 disables the deadline")
    parser.add_argument("--offline", action="store_true", help="Disable extension-manager catalog/package network access")
    parser.add_argument("--turn-timeout", type=float, default=600, help="Seconds per model turn; 0 disables this deadline")
    parser.add_argument("--allow-legacy-runtime", action="store_true", help="Allow Aelix without runtime control v1, with reduced functionality")
    parser.add_argument("--extension-manager", help="Writable Aelix CLI used for extension package management")
    parser.add_argument("--extension", action="append", default=[], help="Explicit trusted Aelix extension path (repeatable)")
    parser.add_argument("--provider")
    parser.add_argument("--model")
    args = parser.parse_args()
    if args.max_sessions < 1 or args.max_sessions > 256 or any(not math.isfinite(value) or value < 0 for value in (args.turn_timeout, args.command_timeout)):
        parser.error("max-sessions must be 1..256 and timeouts must be finite and nonnegative")
    try:
        asyncio.run(Server(args).run())
    except (KeyboardInterrupt, BrokenPipeError, asyncio.CancelledError):
        pass
    except (sqlite3.DatabaseError, ValueError, OSError) as error:
        print(f"[aelix-acp] Startup or transport failed: {error}", file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
