"""Newline JSON-RPC ACP server backed by real `aelix --mode rpc` children."""
from __future__ import annotations

import argparse
import base64
import sqlite3
import asyncio
import contextlib
import json
from itertools import chain
import os
from pathlib import Path
import shutil
import signal
import sys
import uuid
import time
import math

from . import __version__
from .wire.errors import (ProtocolError, SESSION_LOCKED, TURN_FAILED_AFTER_DISPATCH,
                          fork_head_changed, require_object, require_string)
from .runtime.store import (open_store, load_preferences, history, legacy_permission_mode,
                            save_preferences, cache_recovered_history, write_store)
from .runtime.controls import MODE_IDS
from .runtime import managed_extensions
from .runtime.subagents import META_KEY as SUBAGENTS_META
from .stdio import StdinReader
from .wire.transport import decode_frame, encode_frame, StdoutWriter
from .runtime.extensions import run_extension_action, require_consent
from .runtime.leases import acquire as acquire_lease
from .runtime.leases import lease_path
from .runtime import forks, host_bytes, native_history, session_relay
from .runtime.children import NESTED_TOOL_CALLS, nested_only

from .session import CONCURRENT_CONTROLS, Session, text_content, tool_call
from .wire.errors import session_changing

__all__ = ["Server", "Session", "ProtocolError", "main", "tool_call", "text_content"]
SESSION_MUTATIONS = {"session/load", "session/fork", "session/delete", "session/close", "session/prompt",
                     "session/set_mode", "session/set_model", "session/set_config_option", "_aelix/manage"}
# `_aelix/manage` actions forwarded to native control when native lists them.
MANAGE_ACTIONS = frozenset({"resources/list", "resources/reload", "settings/get", "settings/set", "tools/set_active",
                            "mcp/status", "mcp/configure", "auth/status", "auth/set_api_key", "auth/login",
                            "auth/logout", "extensions/list", "agents/list", "agents/show", "agents/use",
                            "agents/run", "permissions/get", "children/notices", *CONCURRENT_CONTROLS})
# How long the adapter waits for the client's answer to a request it sent, unless the caller says.
CLIENT_REQUEST_SECONDS = 290


class Server:
    def __init__(self, args):
        self.args = args
        self.db = open_store(args.state_dir)
        self.initialized = False
        self.relay_enabled = False
        self.host_bytes = None
        # Populate only with exact provider/model/API/endpoint routes that pass
        # final HTTP, Stop and log qualification with the pinned distribution.
        self.image_policies = ()
        self.active_request_ids = set()
        self.sessions = {}
        self.restarting_sessions = {}
        self.starting_ids = set()
        self.pending = {}
        self.response_limits = {}
        self.tasks = set()
        self.cleanup_tasks = set()
        self.closing = False  # The client is gone: sessions end without sending.
        self.output_lock = asyncio.Lock()
        self.output = None
        self.managed_extensions = getattr(args, "managed_extensions", None)

    async def send(self, message, *, before_send=None):
        async with self.output_lock:
            raw = encode_frame(message)
            if self.output is None:
                self.output = StdoutWriter()
            if before_send is None:
                await self.output.write(raw)
            else:
                await self.output.write(raw, before_queue=before_send)

    async def client_request(self, method, params, *, withdraw=False, timeout=None, max_response_bytes=None, before_send=None):
        """The client's result for one request, waited for up to `timeout` (CLIENT_REQUEST_SECONDS) seconds.

        `withdraw` (a delegated child's question, aelix-ai #612 fix round 1,
        P-LOW-1): a request the adapter stops waiting for before the client
        answered is taken back with ACP's `$/cancel_request`, which a client may
        ignore; its later answer is then dropped.
        """
        request_id = "acp-" + uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        if max_response_bytes is not None:
            self.response_limits[request_id] = max_response_bytes
        try:
            frame = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            if before_send is None:
                await self.send(frame)
            else:
                await self.send(frame, before_send=before_send)
            return await asyncio.wait_for(future, timeout or CLIENT_REQUEST_SECONDS)
        except (asyncio.CancelledError, TimeoutError):
            if withdraw and not self.closing and (future.cancelled() or not future.done()):
                with contextlib.suppress(Exception):
                    await self.send({"jsonrpc": "2.0", "method": "$/cancel_request", "params": {"requestId": request_id}})
            raise
        finally:
            self.pending.pop(request_id, None)
            self.response_limits.pop(request_id, None)

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
        preferences.pop("forkProjection", None)
        preferences.pop("forkHead", None)
        preferences["backendMessageCount"] = 1
        def save_branch(db):
            db.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?)", (identifier, session.cwd, self.args.profile,
                       filename, result.get("title") or "Aelix branch", int(time.time())))
            save_preferences(db, identifier, preferences)

        await write_store(self.args.state_dir, save_branch)
        await session.controls.refresh()
        await session.restore_tools()
        await session.save()
        return {"sessionId": identifier, "parentSessionId": session.id}

    async def native_history(self, session):
        messages = await session.read_native_history()
        updates = []
        try:
            for message in messages:
                for update in self.native_message_updates(session, message):
                    if len(updates) >= native_history.MAX_UPDATES:
                        raise native_history.InvalidHistory
                    updates.append(update)
                await asyncio.sleep(0)
        except (native_history.InvalidHistory, UnicodeError):
            raise native_history.unavailable() from None
        if updates:
            try:
                await cache_recovered_history(self.args.state_dir, session.id, updates)
            except (OSError, sqlite3.Error, ValueError):
                raise native_history.unavailable() from None
        return updates

    def native_message_updates(self, session, message):
        role = message.get("role")
        if role not in ("user", "assistant", "toolResult"):
            return  # Harness-only messages aren't transcript rows.
        if role == "toolResult":
            call_id = native_history.tool_identity(message.get("tool_call_id") or message.get("toolCallId"))
            yield {"sessionUpdate": "tool_call_update", "toolCallId": call_id,
                   "status": "failed" if message.get("is_error") or message.get("isError") else "completed",
                   "content": native_history.tool_content(message)}
            return
        kind = "user_message_chunk" if role == "user" else "agent_message_chunk"
        has_text = False
        for block in native_history.content_blocks(message):
            block_type = block.get("type")
            if role == "assistant" and block_type == "toolCall":
                call_id = native_history.tool_identity(block.get("tool_call_id") or block.get("id"))
                name = native_history.tool_identity(block.get("tool_name") or block.get("name"))
                update = {"sessionUpdate": "tool_call", **tool_call(call_id, name, {}, session.cwd, "in_progress")}
                if block.get("argumentsOmitted") is True:
                    update["content"] = native_history.omitted_tool_content(native_history.TOOL_ARGUMENTS_OMITTED)
                yield update
                has_text = False
            elif parts := native_history.display_parts(block):
                if has_text and block_type == "text":
                    yield {"sessionUpdate": kind, "content": native_history.text_block("\n")}
                has_text = block_type == "text"
                # JSON escapes can take six bytes. Keep outbound frames small
                # even when a single native text block spans many MiB.
                for text in parts:
                    for offset in range(0, len(text), native_history.TEXT_CHUNK):
                        yield {"sessionUpdate": kind,
                               "content": native_history.text_block(text[offset:offset + native_history.TEXT_CHUNK])}

    async def fork_head(self, params):
        parent = require_string(params.get("sessionId"), "sessionId")
        cwd = str(Path(require_string(params.get("cwd"), "cwd")).expanduser().resolve())
        mcp = params.get("mcpServers", [])
        if not isinstance(mcp, list):
            raise ProtocolError("mcpServers must be an array")
        if any(key in params for key in ("entryId", "lastTurnId", "anchor")):
            raise ProtocolError("Only HEAD forks are supported")
        metadata = require_object(params.get("_meta") or {}, "_meta")
        binding_id = session_relay.open_binding(metadata) if self.relay_enabled else None
        expected_head = metadata.get("aelix/expectedHead")
        if not isinstance(expected_head, str) or not expected_head or len(expected_head) > 128:
            raise fork_head_changed()
        row = self.db.execute("SELECT cwd,profile,file FROM sessions WHERE id=?", (parent,)).fetchone()
        if not row or row[:2] != (cwd, self.args.profile) or not Path(cwd).is_dir():
            raise ProtocolError("Saved session does not match this workspace and profile")
        filename = row[2]
        if not filename or not Path(filename).is_file():
            raise ProtocolError("No persisted Aelix session file is available to fork")
        await write_store(self.args.state_dir, forks.recover, self.args.state_dir, parent)
        if len(self.sessions) + len(self.starting_ids) >= getattr(self.args, "max_sessions", 16):
            raise ProtocolError("Session limit reached; close a session first", -32000)

        identifier = str(uuid.uuid4())
        session = Session(self, identifier, cwd)
        session.client_mcp = mcp
        preferences = load_preferences(self.db, parent)
        head = preferences.pop("forkHead", None)
        if not isinstance(head, dict) or head.get("id") != expected_head or not isinstance(head.get("sha256"), str):
            raise fork_head_changed()
        saved_mode = legacy_permission_mode(self.db, parent)
        if saved_mode is not None:
            preferences.setdefault("permission_mode", saved_mode)
        with contextlib.ExitStack() as stack:
            peer = None
            try:
                stack.enter_context(acquire_lease(self.args.state_dir, parent))
            except ProtocolError as error:
                if error.code != SESSION_LOCKED:
                    raise
                # A sibling ACP worker can retain the writer lease while idle.
                # Native checks the compatible marker at the snapshot boundary.
                peer = str(lease_path(self.args.state_dir, parent, "fork-peer-v1").resolve())
            self.starting_ids.add(identifier)
            try:
                session.lease = acquire_lease(self.args.state_dir, identifier)
                draft = await write_store(self.args.state_dir, forks.reserve, self.args.state_dir,
                                          identifier, parent, filename)
                async with session.publication_lock:
                    await session.start(fork_source={**draft, "file": filename, "peer": peer, "sessionId": parent,
                                                     "expectedSha256": head["sha256"]},
                                        fork_preferences=preferences)
                    await session.commands()
                    result = {"sessionId": identifier, **session.selectors()}
                    await session.save(session.fork_history(), live=True)
                    self.sessions[identifier] = session
                try:
                    forks.receipt_path(self.args.state_dir, identifier).unlink(missing_ok=True)
                except OSError:
                    print("[aelix-acp] Could not remove committed fork receipt", file=sys.stderr)
            except BaseException:
                # Retain the draft's lifetime lease through shutdown and rollback.
                lease, session.lease = session.lease, None
                try:
                    await session.close()
                    if lease is not None:
                        await write_store(self.args.state_dir, forks.discard, self.args.state_dir, identifier)
                except (OSError, ValueError, sqlite3.Error, ProtocolError):
                    raise forks.cleanup_pending() from None
                finally:
                    if lease is not None:
                        lease.close()
                raise
            finally:
                self.starting_ids.discard(identifier)
        return session_relay.bind_session(session, result, binding_id)

    async def close_session(self, session):
        if session.mutation_lock.locked():
            raise ProtocolError("Session is busy", -32000)
        async with session.mutation_lock:
            await session.close()
        return {}

    async def dispatch(self, method, params):
        require_object(params)
        if handler := session_relay.REQUESTS.get(method):
            return await self.relay_request(handler, params)
        if self.host_bytes is not None and (handler := session_relay.IMAGE_REQUESTS.get(method)):
            return await self.relay_request(handler, params)
        if method.startswith(session_relay.METHOD_PREFIX):
            raise ProtocolError("Unsupported session relay method.", -32601)
        if method == "_aelix/manage" and isinstance(params.get("action"), str) and params["action"] in CONCURRENT_CONTROLS:
            # aelix-ai #612: neither the activity lease nor mutation_lock, which a
            # running session/prompt or agents/run holds (Session.intervene).
            return await self.intervene(params)
        if method in SESSION_MUTATIONS:
            session_id = require_string(params.get("sessionId"), "sessionId")
            if method in ("session/load", "session/delete", "session/close") and session_id in self.starting_ids:
                raise ProtocolError("Session is already starting", -32000)
            registered = session_id in self.sessions or self.db.execute(
                "SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone()
            if registered:
                with acquire_lease(self.args.state_dir, session_id, purpose="activity"):
                    return await self.dispatch_request(method, params)
            if method == "session/fork":
                raise ProtocolError("Saved session does not match this workspace and profile")
        return await self.dispatch_request(method, params)

    async def relay_request(self, handler, params):
        if not self.relay_enabled:
            raise ProtocolError("Session relay was not negotiated.", -32601)
        session_relay.require_fields(params, {"sessionId", "bindingId", "payload"})
        session_id = require_string(params["sessionId"], "sessionId")
        binding_id = require_string(params["bindingId"], "bindingId")
        session = self.sessions.get(session_id)
        relay = session.input_relay if session is not None else None
        if relay is None or relay.binding_id != binding_id or session_id in self.starting_ids:
            raise ProtocolError("Session relay binding is unavailable.", session_relay.SCOPE_DENIED)
        return await handler(relay, params["payload"])

    async def intervene(self, params):
        session_id = require_string(params.get("sessionId"), "sessionId")
        arguments = require_object(params.get("arguments", {}), "arguments")
        if session_id in self.starting_ids:
            raise session_changing()
        session = self.session(params)
        if params["action"] not in session.controls.capabilities.get("methods", []):
            raise ProtocolError("Unsupported management action")
        return await session.intervene(params["action"], arguments)

    async def dispatch_request(self, method, params):
        if method == "initialize":
            if self.initialized:
                raise ProtocolError("Already initialized", -32600)
            if type(params.get("protocolVersion")) is not int or params.get("protocolVersion") < 1:
                raise ProtocolError("protocolVersion must be a positive integer")
            capabilities = params.get("clientCapabilities", {})
            require_object(capabilities, "clientCapabilities")
            if capabilities.get("_meta") is not None:
                require_object(capabilities["_meta"], "clientCapabilities._meta")
            forks.recover(self.db, self.args.state_dir)
            self.initialized = True
            self.client_capabilities = capabilities
            relay_ad = session_relay.advertisement(capabilities)
            self.relay_enabled = relay_ad is not None
            if self.relay_enabled and host_bytes.offered(capabilities):
                self.host_bytes = host_bytes.HostBytes(self)
            return {"protocolVersion": 1,
                    "agentInfo": {"name": "aelix-acp", "title": f"Aelix {self.args.profile.title()}", "version": __version__},
                    "agentCapabilities": {"loadSession": True, "promptCapabilities": {"embeddedContext": True, "image": True},
                                          "mcpCapabilities": {"http": True, "sse": True},
                                          "sessionCapabilities": {"list": {}, "delete": {}, "close": {},
                                                                  "fork": {"_meta": {"aelix/expectedHead": 1}}},
                                          "_meta": {SUBAGENTS_META: {"version": 1, "on": "tool_call_update"},
                                                    **({session_relay.META_KEY: relay_ad} if relay_ad else {}),
                                                    **({host_bytes.META_KEY: {"version": 1}} if self.host_bytes else {}),
                                                    "aelix/errors": {"sessionLocked": SESSION_LOCKED, "turnFailedAfterDispatch": TURN_FAILED_AFTER_DISPATCH}}},
                    "_meta": {"aelix/control": {"version": 1, "methods": ["_aelix/capabilities", "_aelix/manage"],
                              "clientActionCapability": "aelix/clientActions"},
                              **({managed_extensions.META: {"version": managed_extensions.VERSION}}
                                 if self.managed_extensions is not None else {})}, "authMethods": []}
        if method == "session/fork":
            return await self.fork_head(params)
        if method in ("session/new", "session/load"):
            binding_id = session_relay.open_binding(params.get("_meta")) if self.relay_enabled else None
            cwd = str(Path(require_string(params.get("cwd"), "cwd")).expanduser().resolve())
            if not isinstance(params.get("mcpServers", []), list):
                raise ProtocolError("mcpServers must be an array")
            if not Path(cwd).is_dir():
                raise ProtocolError("Session cwd must be an existing directory")
            session_file = None
            index_snapshot = None
            if method == "session/load":
                session_id = require_string(params.get("sessionId"), "sessionId")
                row = self.db.execute("SELECT cwd,profile,file FROM sessions WHERE id=?", (session_id,)).fetchone()
                if not row or row[0] != cwd or row[1] != self.args.profile:
                    raise ProtocolError("Saved session does not match this workspace and profile")
                index_snapshot = row
                session_file = row[2]
                if not session_file or not Path(session_file).is_file():
                    preferences = load_preferences(self.db, session_id)
                    if preferences.get("backendMessageCount") == 0:
                        session_file = None
                    else:
                        raise ProtocolError("No persisted Aelix session file is available")
            else:
                session_id = str(uuid.uuid4())
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
                session = Session(self, session_id, cwd,
                                  initial_mode=getattr(self.args, "permission_mode", "default") if method == "session/new" else None)
                session.index_snapshot = index_snapshot
                session.client_mcp = params.get("mcpServers", [])
                async with session.publication_lock:
                    await session.start(session_file)
                    await session.commands()
                    if method == "session/load":
                        await session.live_history.flush()
                        recorded = history(self.db, session_id)
                        if (first := next(recorded, None)) is not None:
                            nesting = session.declares(NESTED_TOOL_CALLS)
                            for update in chain((first,), recorded):
                                if not nesting and nested_only(update):
                                    continue  # aelix-ai #612: the loading client's opt-in, as live.
                                if session.private_host:
                                    update.pop("rawOutput", None)
                                await self.send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": session_id, "update": update}})
                        elif not session.fork_projection:
                            for update in await self.native_history(session):
                                await session.notify("session/update", {"sessionId": session_id, "update": update})
                                await asyncio.sleep(0)
                        await session.publish_context_usage()
                        await session.save(live=True)
                    await session.publish_recovery_warnings()
                    await session.live_history.flush()
                    session.require_live_worker()
                    self.sessions[session_id] = session
            except BaseException:
                self.sessions.pop(session_id, None)
                if session:
                    await session.close()
                raise
            finally:
                self.starting_ids.discard(session_id)
            result = {**session.selectors(), **({"sessionId": session_id} if method.endswith("new") else {})}
            return session_relay.bind_session(session, result, binding_id)
        if method == "session/close":
            return await self.close_session(self.session(params))
        if method == "session/delete":
            session_id = require_string(params.get("sessionId"), "sessionId")
            row = self.db.execute("SELECT profile FROM sessions WHERE id=?", (session_id,)).fetchone()
            if row is None or row[0] != self.args.profile:
                raise ProtocolError("Unknown session")
            live = self.sessions.get(session_id)
            if live and live.mutation_lock.locked():
                raise ProtocolError("Session is busy; cancel its turn before deleting", -32000)
            lease = live.lease if live and live.lease else acquire_lease(self.args.state_dir, session_id)
            if live:
                live.lease = None  # Hold the lease across native close and SQL deletion.
            self.starting_ids.add(session_id)
            try:
                async with (live.mutation_lock if live else asyncio.Lock()):
                    if live:
                        await live.close()
                    def delete_records(db):
                        db.execute("DELETE FROM preferences WHERE session_id=?", (session_id,))
                        db.execute("DELETE FROM sessions WHERE id=?", (session_id,))
                        db.execute("DELETE FROM events WHERE session_id=?", (session_id,))
                        db.execute("DELETE FROM recovered_history WHERE session_id=?", (session_id,))
                        db.execute("DELETE FROM turns WHERE session_id=?", (session_id,))

                    await write_store(self.args.state_dir, delete_records)
            except BaseException:
                if live and self.sessions.get(session_id) is live:
                    live.lease, lease = lease, None
                raise
            finally:
                if lease:
                    lease.close()
                self.starting_ids.discard(session_id)
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
                anchor = metadata.get("aelix/forkHead")
                if anchor is not None and (not isinstance(anchor, str) or not anchor or len(anchor) > 128):
                    raise ProtocolError("Invalid native fork reference")
                return await session.prompt(params.get("prompt"), metadata.get("aelix/requestId"), anchor)
        if method == "session/cancel":
            session_id = require_string(params.get("sessionId"), "sessionId")
            session = self.restarting_sessions.get(session_id) or self.session(params)
            await session.cancel()
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
                await session.recover_after_compaction()
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
            if action == "sessions/close":
                return await self.close_session(session)
            if session.mutation_lock.locked():
                raise ProtocolError("Session is busy", -32000)
            async with session.mutation_lock:
                await session.recover_after_compaction()
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
                if action in (managed_extensions.LIST, managed_extensions.SET):
                    if self.managed_extensions is None:
                        raise ProtocolError("Managed extensions are unavailable", -32004,
                                            {"kind": "managed_extensions_unavailable"})
                    if action == managed_extensions.LIST and not arguments:
                        return await self.managed_extensions.status(session)
                    if action == managed_extensions.SET and set(arguments) == {"extensionId", "value"}:
                        return await self.managed_extensions.set(self, session, arguments["extensionId"], arguments["value"])
                    raise ProtocolError("Invalid managed extension arguments")
                if action == "extensions/action":
                    return await self.extension_action(session, require_string(arguments["command"], "command") if "command" in arguments else arguments)
                if action not in MANAGE_ACTIONS or action not in session.controls.capabilities.get("methods", []):
                    raise ProtocolError("Unsupported management action")
                if action == "children/notices":
                    return await session.notice_turn()
                if action == "mcp/configure":
                    result = await session.configure_mcp(arguments.get("servers"))
                else:
                    result = await session.rpc("aelix_control", method=action, params=arguments,
                        _timeout=(getattr(self.args, "command_timeout", 600) or None) if action in {"auth/login", "resources/reload", "agents/run"} else 60)
                if action == "tools/set_active":
                    session.active_tools = result["activeTools"]
                    await session.save()
                if action == "resources/reload":
                    await session.restore_tools()
                    await session.commands()
                if action == "agents/use":
                    await session.commands()
                if action in {"auth/set_api_key", "auth/login", "auth/logout", "resources/reload", "settings/set", "agents/use"}:
                    await session.controls.refresh()
                    if action == "agents/use":
                        await session.sync_profile_tools()
                    await session.save()
                    await session.update({"sessionUpdate": "config_option_update", "configOptions": session.controls.options()})
                return result
        if method == "authenticate":
            raise ProtocolError("Configure provider authentication using the existing Aelix CLI first")
        raise ProtocolError(f"Method not supported: {method}", -32601)

    async def handle(self, message):
        request_id = message.get("id")
        replied = False
        try:
            method = message["method"]
            if request_id is None and method.startswith(session_relay.METHOD_PREFIX):
                return  # v1 has no client-to-agent session relay notifications.
            result = await self.dispatch(method, message.get("params") or {})
            if request_id is not None:
                response = {"jsonrpc": "2.0", "id": request_id, "result": result}
                if method == session_relay.LOOKUP_METHOD:
                    response = session_relay.bounded_lookup_response(response)
                await self.send(response)
                replied = True
                if message["method"] in ("session/new", "session/load", "session/fork"):
                    session = self.sessions.get(result.get("sessionId") or message.get("params", {}).get("sessionId"))
                    if session:
                        # Follows the open response, so a host that applies its
                        # own saved preference first still observes the
                        # restored policy.
                        await session.update({"sessionUpdate": "current_mode_update",
                                              "currentModeId": session.controls.permission_mode})
        except Exception as exc:
            if request_id is not None and not replied:
                error = {"code": getattr(exc, "code", -32603), "message": str(exc)}
                if isinstance(exc, ProtocolError) and exc.data is not None:
                    error["data"] = exc.data
                await self.send({"jsonrpc": "2.0", "id": request_id, "error": error})
            else:
                detail = type(exc).__name__ if self.host_bytes else str(exc)
                print(f"[aelix-acp] {detail}", file=sys.stderr)
        finally:
            self.active_request_ids.discard(request_id)

    async def run(self):
        reader = StdinReader()
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
                        limit = self.response_limits.get(message.get("id"))
                        if limit is not None and len(line.removesuffix(b"\n")) > limit:
                            future.set_exception(ProtocolError("Client response exceeds the byte limit"))
                        elif "error" in message:
                            error = message["error"]
                            future.set_exception(ProtocolError("Client request failed", -32603,
                                {"kind": "client_error", "code": error.get("code") if isinstance(error, dict) else None}))
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
                                         "error": {"code": -32600, "message": "Call initialize first"}})
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
            reader.close()
            self.closing = True
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
    parser.add_argument("--permission-mode", choices=sorted(MODE_IDS), default="default",
                        help="Initial mode for new sessions; saved sessions restore their recorded mode")
    parser.add_argument("--extension-manager", help="Writable Aelix CLI used for extension package management")
    parser.add_argument("--extension", action="append", default=[], help="Explicit trusted Aelix extension path (repeatable)")
    parser.add_argument("--managed-extensions", type=managed_extensions.read_launch,
                        help="Absolute path to the Studio launch snapshot for managed extensions")
    parser.add_argument("--provider")
    parser.add_argument("--model")
    args = parser.parse_args()
    if args.max_sessions < 1 or args.max_sessions > 256 or any(not math.isfinite(value) or value < 0 for value in (args.turn_timeout, args.command_timeout)):
        parser.error("max-sessions must be 1..256 and timeouts must be finite and nonnegative")
    if args.managed_extensions is not None and any(
        arg.startswith("-e") or arg.split("=", 1)[0] == "--extension"
        for arg in args.aelix_arg
    ):
        parser.error("use adapter --extension for explicit extensions in managed mode")
    try:
        asyncio.run(Server(args).run())
    except (KeyboardInterrupt, BrokenPipeError, asyncio.CancelledError):
        pass
    except (sqlite3.DatabaseError, ValueError, OSError) as error:
        print(f"[aelix-acp] Startup or transport failed: {error}", file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
