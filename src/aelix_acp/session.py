"""Newline JSON-RPC ACP server backed by real `aelix --mode rpc` children."""
from __future__ import annotations

import base64
import binascii
import codecs
import asyncio
import contextlib
from collections import Counter
import json
import hmac
import hashlib
import secrets
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import uuid

from .wire.errors import (ProtocolError, BackendError, SOCKET_PATH_TOO_LONG, after_dispatch, delivery_unknown, fork_head_changed,
                          is_native_session_locked, require_object, require_string, session_changing, session_locked)
from .wire.process import terminate_owned
from .stdio import write_diagnostic
from .wire.transport import encode_frame
from . import image_policy
from .runtime import native_frames, native_history, transcript
from .runtime.controls import SessionControls, MODE_IDS
from .runtime.context import SessionContext
from .runtime.store import (LiveHistory, begin_turn, confirm_adapter_input, finish_turn, legacy_permission_mode, load_preferences,
                            recorded_turn, save_session, write_store)
from .runtime.commands import (ALIASES, BUILTINS, PRIVATE_NATIVE_COMMANDS, SELECTOR_STARTUP_COMMAND,
                               compact_instructions, execute as execute_slash)
from .runtime.extensions import extension_reference
from .runtime.studio_resources import StudioResources, tool_lock_key
from .runtime.managed_extensions import managed_preferences
from .runtime.leases import acquire as acquire_lease, lease_path
from .runtime.subagents import SubagentTracker
from .runtime.children import (CHILD_META, END_STATUS, NESTED_TOOL_CALLS, NOTICES, SYNTHETIC_PREFIX, TOOL_STATUS,
                               ChildRoutes, terminal)

PACKAGE = Path(__file__).resolve().parent
KINDS = {"read": "read", "grep": "search", "find": "search", "ls": "read",
         "write": "edit", "edit": "edit", "bash": "execute", "aelix_status": "read",
         "analytics_status": "read", "analytics_query": "read"}
# Native history selectors. A launch prefix must not bypass session/load.
SESSION_SELECTORS = ("--session", "--resume", "-r", "--continue", "-c", "--fork", "--no-session")
NATIVE_ROUTE_ARGUMENTS = frozenset({
    "--provider", "--model", "--api-key", "--require-registration-route",
})
NATIVE_SESSION_SELECTORS = ("model", "thinking_level")
# Native requests that never change the runtime (aelix-ai rpc/control.py
# CLASSIC_READ_ONLY and READ_ONLY_METHODS). Anything else sent during a turn
# may have started work, so a later failure must not be replayed.
READ_ONLY_COMMANDS = frozenset({"get_state", "get_messages", "get_available_models", "get_commands",
                                "get_session_stats", "get_fork_messages", "get_last_assistant_text",
                                "export_html", "abort", "abort_retry", "abort_bash"})
READ_ONLY_CONTROLS = frozenset({"capabilities", "resources/list", "settings/get", "permissions/get",
                                "permissions/evaluate", "mcp/status", "extensions/list", "auth/status",
                                "agents/list", "agents/show", "commands/cancel",
                                "children/list", "children/events", transcript.METHOD})
# aelix-ai #612: a host's view of and interventions on delegated children. Native
# answers them during a turn and during agents/run (its CONCURRENT_METHODS and the
# two lock-free reads), so ACP sends them outside mutation_lock. They act on a
# child, not on the turn, so they never mark a running turn dispatched.
CONCURRENT_CONTROLS = frozenset({"children/list", "children/events", "children/steer",
                                 "children/wrap_up", "children/stop"})
# Native controls that run as long as their work does: the command deadline, and
# commands/cancel when it passes. children/resume (aelix-ai #836) runs a resumed agent
# in the foreground, as agents/run runs a new one.
LONG_CONTROLS = frozenset({"commands/run", "auth/login", "resources/reload", "agents/run", "children/resume"})
# A cancelled turn that still streams is aborted again at native's own grace
# (aelix-ai harness/core.py _REABORT_GRACE_SECONDS), not at every 50 ms poll.
REABORT_SECONDS = 2.0
# How long a closing POSIX worker that serves delegated children or background
# jobs gets between SIGTERM and SIGKILL: native's session-end stages (the turn's
# settle, the jobs' owner_end and the job service's shutdown) take about 40 s
# at their bounds (aelix-ai contract r5 section 6.4). Others keep 5 s.
CHILDREN_CLOSE_SECONDS = 45.0
# A child's question waits for native's own bound (aelix-ai rpc/control_ui.py
# DIALOG_TIMEOUT_SECONDS, 300 s): there native answers it itself and the child
# reads "no answer from the host within 300 s" (fix round 1, P-LOW-4), and its
# `denied` step takes the request back. The adapter's bound is a backstop past it.
CHILD_QUESTION_SECONDS = 330.0
CHILD_APPROVAL_OPTIONS = {"allow_once": ("allow-once", "Allow once"),
                          "allow_always": ("allow-always", "Allow for this agent"),
                          "reject_once": ("reject-once", "Reject")}
# Native UI methods that need no answer.
UI_NOTIFICATIONS = frozenset({"notify", "setStatus", "setWidget", "setTitle", "authProgress"})
# How long a worker that exited gets for its last stdout lines to be read.
READ_TAIL_SECONDS = 2.0
STDERR_TAIL = 16 * 1024
FRAME_LIMIT = 16 * 1024 * 1024
# An unauthenticated loopback peer gets one bounded line for its token.
PRE_AUTH_LIMIT = 64 * 1024
_SESSION_TEMP_PREFIX = "aelix-acp-"
_BRIDGE_SOCKET_NAME = "bridge.sock"
_SHORT_TEMP_DIR = "/tmp"


def _make_session_dir(*, tcp):
    if tcp:
        return tempfile.mkdtemp(prefix=_SESSION_TEMP_PREFIX)

    # bridge.sock is the only Unix socket. mkdtemp appends eight ASCII characters.
    relative_socket = Path(_SESSION_TEMP_PREFIX + "x" * 8) / _BRIDGE_SOCKET_NAME
    limit = 108 if sys.platform.startswith("linux") else 104

    def fits(parent):
        return len(os.fsencode(Path(parent) / relative_socket)) + 1 <= limit

    try:
        default = tempfile.gettempdir()
    except FileNotFoundError:
        default = None
    if default is not None and fits(default):
        return tempfile.mkdtemp(prefix=_SESSION_TEMP_PREFIX, dir=default)
    if os.name == "posix" and fits(_SHORT_TEMP_DIR):
        try:
            return tempfile.mkdtemp(prefix=_SESSION_TEMP_PREFIX, dir=_SHORT_TEMP_DIR)
        except OSError:
            # The short directory may be unavailable. Keep filesystem paths out of the error.
            pass
    raise ProtocolError(f"Could not create a session directory for a Unix socket within the {limit}-byte AF_UNIX limit "
                        "(including the terminating NUL). Set TMPDIR to a shorter, writable directory.",
                        SOCKET_PATH_TOO_LONG,
                        {"kind": "socket_path_too_long", "limitBytes": limit, "retryable": False})


def tool_call(call_id, name, args, cwd, status="pending"):
    result = {"toolCallId": call_id, "title": name, "kind": KINDS.get(name, "other"),
              "status": status, "rawInput": args}
    path = args.get("path") or args.get("file_path")
    if isinstance(path, str):
        try:
            p = Path(path).expanduser()
        except RuntimeError:
            return result
        result["locations"] = [{"path": str(p if p.is_absolute() else Path(cwd) / p)}]
    return result


async def read_line(reader, limit):
    """One newline-terminated line of at most `limit` bytes, read in chunks.

    The stream's own limit stays small for the pre-authentication line, so a
    larger line is assembled here instead of by `readline`.
    """
    line = bytearray()
    while not line.endswith(b"\n"):
        chunk = await reader.read(min(65536, limit + 1 - len(line)))
        if not chunk:
            break
        newline = chunk.find(b"\n")
        line += chunk if newline < 0 else chunk[:newline + 1]
        if len(line) > limit:
            raise ValueError("Permission request exceeds the frame limit")
    return bytes(line)


def ui_enable_params(methods):
    """`ui/enable` for a client-action client; with a children-capable native, its child approvals too.

    aelix-ai #612 (ADR-0308): a delegated child of an Ask parent then asks this
    client through `session/request_permission` (`Session.child_approval`).
    """
    return {"enabled": True, **({"childApprovals": True} if "children/list" in methods else {})}


def text_content(content):
    if isinstance(content, str):
        return content
    return "\n".join(c.get("text", "") for c in content or [] if c.get("type") == "text")


def profile_unavailable(name):
    return ProtocolError(f"This session uses the agent profile '{name}', which could not be loaded. "
                         "Restore or recreate that profile, then open the session again; or start a new session.",
                         -32004, {"kind": "profile_unavailable", "profile": name})


class Session:
    def __init__(self, server, session_id, cwd, *, initial_mode=None):
        self.server, self.id, self.cwd = server, session_id, cwd
        self.initial_mode = initial_mode
        self.proc = None
        self.pending = {}
        self.history_requests = set()
        self.tasks = set()
        self.done = None
        self.failure = None
        self.rpc_closed = False
        self.cancelled = False
        self.cancel_generation = 0
        self.turn_active = False
        self.input_relay = None
        self.private_host = getattr(server, "host_bytes", None) is not None
        self.image_generation = None
        self.image_epoch = None
        self.image_turn_id = None
        self.dispatched = False
        self.tools = {}
        self.tool_arrived = asyncio.Event()
        self.subagents = SubagentTracker()
        self.recovery_warnings = []
        self.permissions = set()
        self.permission_connections = set()
        self.ui_tasks = set()
        self.unstarted = set()  # Dialog tasks not yet run: answer_unstarted answers one cancelled before it ran.
        # Open child questions: a future set when the question is void -> (epoch, child id, call id).
        self.questions = {}
        self.children = ChildRoutes()
        self.child_calls = set()  # Synthetic top-level cards opened for unparented children.
        self.changing = 0  # Raised while restart() or close() replaces or ends the worker.
        self.interventions = {}  # CONCURRENT_CONTROLS requests waiting for native: task -> its answer's future.
        self.fenced = set()  # Those a restart or close interrupted.
        self.reader = None  # The read_rpc task.
        self.ending = False  # While _close() ends the worker.
        self.last_abort = None
        self.aborts = 0  # Native aborts sent, by any path.
        self.management_task = None
        self.extension_commit_started = False
        self._close_task = None
        self.lease = None
        self.fork_peer_lease = None
        self.index_snapshot = None
        self.client_mcp = []  # Runtime-only: server URLs, environment and headers may contain credentials.
        self.state = {}
        self.socket_dir = None
        self.socket_server = None
        self.permission_token = None
        self.permission_endpoint = None
        self.worker_job = None
        self.stderr_task = None
        self.stderr_tail = bytearray()
        self.controls = SessionControls(self)
        self.studio_resources = StudioResources(self)
        self.adapter_event_binding = uuid.uuid4().hex
        self.adapter_event_sequence = 0
        self.tool_origin = None
        self.tool_occurrences = Counter()
        self.pending_tool_results = {}
        self.context = SessionContext(self)
        self.compaction_recovery_needed = False
        self.mutation_lock = asyncio.Lock()
        self.live_history = LiveHistory(server.args.state_dir, self.id)
        self.publication_lock = asyncio.Lock()
        self._pending_extension_change = None
        self._verifying_extension_recovery = False
        self.restore_launch_preferences()
        self.analytics_project = Path(cwd) if (Path(cwd) / "analytics.json").is_file() else None

    def restore_launch_preferences(self, preferences=None):
        if preferences is None:
            preferences = load_preferences(self.server.db, self.id)
        self.fork_projection = preferences.get("forkProjection") is True
        self.fork_head = preferences.get("forkHead")
        config = preferences.get("config", {})
        self.saved_config = config if isinstance(config, dict) else {}  # start() refuses a malformed one.
        self.active_tools = preferences.get("activeTools")
        if self.active_tools is not None and (not isinstance(self.active_tools, list) or any(not isinstance(name, str) for name in self.active_tools)):
            raise ProtocolError("Invalid saved tool selection")
        self.extra_extensions = preferences.get("extensions", [])
        self.extension_overrides = managed_preferences(preferences)
        self.trust_project = preferences.get("trustProject", getattr(self.server.args, "trust_project", None))
        self.agents_enabled = preferences.get("agentsEnabled", getattr(self.server.args, "enable_agents", False))
        if self.trust_project is not None and type(self.trust_project) is not bool:
            raise ProtocolError("Invalid saved workspace trust")
        if type(self.agents_enabled) is not bool:
            raise ProtocolError("Invalid saved delegation setting")
        if not isinstance(self.extra_extensions, list) or any(not isinstance(path, str) for path in self.extra_extensions):
            raise ProtocolError("Invalid stored extension configuration")
        # Older records without a mode stay Ask; an unknown saved mode refuses
        # rather than guessing a posture.
        mode = preferences.get("permission_mode")
        if mode is None:
            mode = legacy_permission_mode(self.server.db, self.id)
        if mode is None:
            mode = self.initial_mode or "default"
        if not isinstance(mode, str) or mode not in MODE_IDS:
            raise ProtocolError("Saved permission mode is not Ask or Yolo; refusing to resume", -32602,
                                {"kind": "unsupported_saved_mode", "mode": str(mode)[:64], "supported": sorted(MODE_IDS)})
        self.launch_mode = mode

    async def start(self, session_file=None, *, fork_source=None, fork_preferences=None, managed_change=None):
        self.tool_origin = None
        self.tool_occurrences.clear()
        self.pending_tool_results.clear()
        self.controls.capabilities = {}
        if self.input_relay is not None:
            self.input_relay.observe_prompt(False)
        launch_prefix = getattr(self.server.args, "aelix_arg", [])
        prefix_options = {argument.split("=", 1)[0] for argument in launch_prefix}
        if prefix_options.intersection(SESSION_SELECTORS):
            raise ProtocolError("Adapter launch cannot override native session selection")
        if self.lease is None:
            self.lease = acquire_lease(self.server.args.state_dir, self.id)
        self.fork_peer_lease = acquire_lease(self.server.args.state_dir, self.id, purpose="fork-peer-v1")
        if self.index_snapshot is not None:
            current = self.server.db.execute("SELECT cwd,profile,file FROM sessions WHERE id=?", (self.id,)).fetchone()
            if current != self.index_snapshot:
                raise ProtocolError("Saved session changed before it could be opened; load it again", -32000)
            self.index_snapshot = None
        # Re-read under the acquired lease, not the constructor's stale snapshot.
        preferences = load_preferences(self.server.db, self.id) if fork_preferences is None else fork_preferences
        saved_agent = preferences.get("agent")
        self.studio_resources.reset(preferences)
        resume_file = session_file if fork_source is None else fork_source["target"]
        self.restore_launch_preferences(preferences)
        if managed_change is not None:
            self.extension_overrides = managed_change.overrides
        self.context.restore(preferences.get("lastContextMeasurement"))
        # A reused object may have an older Yolo policy. Startup approval
        # remains Ask until native confirms the just-read policy below.
        self.controls.permission_mode = "default"
        self.stderr_tail = bytearray()  # A refusal from an earlier worker must not label this one.
        tcp = sys.platform == "win32"
        self.socket_dir = _make_session_dir(tcp=tcp)
        os.chmod(self.socket_dir, 0o700)
        socket_path = str(Path(self.socket_dir) / _BRIDGE_SOCKET_NAME)
        await self.listen(socket_path, tcp=tcp)
        args = [self.server.args.aelix, *launch_prefix, "--mode", "rpc", "--offline", "--no-extensions",
                "--permission-mode", self.launch_mode,
                "--append-system-prompt-file", str(PACKAGE / "profiles" / f"{self.server.args.profile}.md")]
        args += ["--agents" if self.agents_enabled else "--no-agents"]
        if self.trust_project is not None:
            args += ["--approve" if self.trust_project else "--deny"]
        explicit_extensions = []
        if self.server.args.profile == "analytics":
            extension = self.server.args.analytics_extension
            if not extension or not Path(extension).is_file():
                raise ProtocolError("Analytics extension is missing; pass --analytics-extension PATH")
            explicit_extensions.append(str(Path(extension).resolve()))
        explicit_extensions.extend(extension_reference(extension, self.cwd)
                                   for extension in getattr(self.server.args, "extension", []))
        extra_extensions = [extension_reference(extension, self.cwd) for extension in self.extra_extensions]
        managed = getattr(self.server, "managed_extensions", None)
        if managed is not None:
            managed.check_external([*explicit_extensions, str(PACKAGE / "bridge.py")])
            self.extra_extensions, self.extension_overrides = managed.migrate_session_references(
                extra_extensions, self.extension_overrides,
            )
            extra_extensions = self.extra_extensions + managed.arguments(self.extension_overrides, self.socket_dir)
        explicit_extensions.extend(extra_extensions)
        for extension in explicit_extensions:
            args += ["-e", extension]
        # Consent sees arguments after other trusted extension hooks.
        args += ["-e", str(PACKAGE / "bridge.py")]
        # Only a Host-negotiated worker gets route budgets. A generic reopen
        # or fork needs the hook solely to suppress marked queued history.
        policies = getattr(self.server, "image_policies", ()) if self.private_host else ()
        history_file = fork_source["file"] if fork_source is not None else session_file
        image_hook = bool(policies)
        if not image_hook and history_file and Path(history_file).is_file():
            image_hook = await asyncio.to_thread(image_policy.has_running_image, history_file)
        if image_hook:
            args += ["-e", str(PACKAGE / "image_policy.py")]
        self.image_generation = uuid.uuid4().hex
        self.image_epoch = self.image_turn_id = None
        # Adapter defaults must not suppress native resume. Explicit native
        # route arguments retain their original provider and model binding.
        if not resume_file or prefix_options & NATIVE_ROUTE_ARGUMENTS:
            if self.server.args.provider:
                args += ["--provider", self.server.args.provider]
            if self.server.args.model:
                args += ["--model", self.server.args.model]
        if session_file:
            if not Path(session_file).is_file():
                raise ProtocolError("The Aelix session file no longer exists")
            args += ["--session", session_file]
        if fork_source is not None:
            args += ["--no-session"]
        nonce = uuid.uuid4().hex
        ready_file = Path(self.socket_dir) / "ready"
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
                   AELIX_ACP_READY_FILE=str(ready_file), AELIX_ACP_NONCE=nonce)
        for name in ("AELIX_ACP_SOCKET", "AELIX_ACP_ENDPOINT", "AELIX_ACP_PERMISSION_TOKEN", "AELIX_ACP_FORK_SOURCE",
                     "AELIX_ACP_SELECTOR_STARTUP_FILE"):
            env.pop(name, None)
        env.pop("AELIX_ACP_IMAGE_POLICIES", None)
        if self.private_host:
            for name in ("ANTHROPIC_LOG", "OPENAI_LOG", "GEMINI_NEXT_GEN_API_LOG"):
                env.pop(name, None)
        if image_hook:
            env["AELIX_ACP_IMAGE_POLICIES"] = json.dumps(policies)
        guard_selectors = bool(saved_agent and resume_file)
        if guard_selectors:
            env["AELIX_ACP_SELECTOR_STARTUP_FILE"] = str(Path(resume_file).resolve())
        if fork_source is not None:
            env["AELIX_ACP_FORK_SOURCE"] = json.dumps({
                **fork_source, "projection": str(Path(self.socket_dir) / "fork-history.jsonl"),
                "workerLease": str(lease_path(self.server.args.state_dir, self.id, "fork-peer-v1").resolve()),
            })
        if self.permission_endpoint:
            env.update(AELIX_ACP_ENDPOINT=self.permission_endpoint, AELIX_ACP_PERMISSION_TOKEN=self.permission_token)
        else:
            env["AELIX_ACP_SOCKET"] = socket_path
        if sys.platform == "win32":
            from .windows import WorkerJob
            self.worker_job = WorkerJob()
            # The trusted gate consumes one byte before starting ANY native
            # bootstrap (including configured MCP startup subprocesses). A
            # venv's python.exe is a redirector that starts the real
            # interpreter before the job assignment, so run the base one.
            gate = getattr(sys, "_base_executable", None) or sys.executable
            args = [gate, "-I", "-S", "-B", str(PACKAGE / "windows.py"), *args]
        self.proc = await asyncio.create_subprocess_exec(
            *args, cwd=self.cwd, env=env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=65536, **({} if sys.platform == "win32" else {"start_new_session": True}))
        if self.worker_job:
            # The native RPC child receives no command before it joins the job;
            # its later tool/MCP descendants inherit the owned lifetime.
            try:
                self.worker_job.assign(self.proc.pid)
                self.proc.stdin.write(b"\0")
                await self.proc.stdin.drain()
            except BaseException:
                self.proc.kill()
                await self.proc.wait()
                raise
        self.stderr_task = asyncio.create_task(self.read_stderr())
        self.tasks = {asyncio.create_task(self.read_rpc()), self.stderr_task}
        try:
            self.state = await self.rpc("get_state")
        except ProtocolError:
            await self.raise_if_locked()
            raise
        if not ready_file.is_file() or ready_file.read_text() != nonce:
            raise ProtocolError("The tool approval bridge did not load; refusing this session", -32603)
        if fork_source is not None:
            if saved_agent and self.studio_resources.recorded is None:
                try:
                    await self.rpc("aelix_control", method="agents/show", params={"name": saved_agent})
                except BackendError:
                    raise profile_unavailable(saved_agent) from None
            await self.open_fork(nonce, fork_source)
        await self.controls.refresh()
        if self.controls.capabilities.get("version") != 1:
            raise ProtocolError("Aelix runtime control v1 is required; update Aelix", -32004,
                                {"kind": "runtime_control_required", "required": 1})
        methods = self.controls.capabilities.get("methods", [])
        self.recovery_warnings = []
        # Read native selectors before applying an identity that may change
        # them. The empty branch still relies on its cached pre-turn choices.
        native_has_history = resume_file and self.state.get("messageCount", self.state.get("message_count")) != 0
        restored_selectors = {
            config_id: value for config_id, value in self.controls.selections().items()
            if config_id in NATIVE_SESSION_SELECTORS
        } if native_has_history else {}
        # A saved profile or tool selection restricts authority. Refuse rather
        # than resume with the runtime's broader defaults; check before client
        # MCP setup, which already needs tools/set_active.
        missing = [method for method, needed in (("agents/use", saved_agent), ("tools/set_active", self.active_tools is not None or self.client_mcp),
                                                 ("permissions/set", True)) if needed and method not in methods]
        if missing:
            raise ProtocolError("This Aelix runtime cannot enforce this session's saved restrictions; update Aelix",
                                -32004, {"kind": "restrictions_unenforceable", "methods": missing})
        if self.client_mcp:
            if "mcp/configure" not in methods:
                raise ProtocolError("This Aelix runtime cannot accept client MCP servers; update its control API", -32004)
            await self.configure_mcp(self.client_mcp)
        if guard_selectors:
            await self.selector_startup(nonce, "begin")
        if saved_agent:
            if self.studio_resources.recorded is not None:
                if not await self.studio_resources.restore():
                    raise ProtocolError("Restore or recreate that profile, then open the session again.",
                                        -32004, {"kind": "profile_unavailable", "profile": saved_agent})
            else:
                try:
                    await self.rpc("aelix_control", method="agents/use", params={"name": saved_agent})
                except BackendError:
                    raise profile_unavailable(saved_agent) from None
            await self.controls.refresh()
            if self.studio_resources.recorded is not None and self.controls.capabilities.get("activeAgent") != saved_agent:
                raise ProtocolError("Aelix did not restore the recorded profile", -32004)
        restored = self.launch_mode
        await self.controls.observe_permission()
        if self.controls.native_mode != restored:
            await self.controls.apply_permission(restored, persist=False)
        self.controls.permission_mode = restored
        await self.restore_tools()

        if saved_agent:
            # Keep the profile's prompt and authority, then restore the live
            # model first so native can validate reasoning for that model.
            for config_id, value in restored_selectors.items():
                option = next((entry for entry in self.controls.options() if entry["id"] == config_id), None)
                if option is None or option["currentValue"] != value:
                    await self.controls.set(config_id, value, publish=False)
            actual = {option["id"]: option["currentValue"] for option in self.controls.options()}
            if any(actual.get(config_id) != value for config_id, value in restored_selectors.items()):
                raise ProtocolError("Aelix did not restore the native session selectors", -32004)
        config = preferences.get("config", {})
        require_object(config, "saved session config")
        for config_id in (*NATIVE_SESSION_SELECTORS, "steeringMode", "followUpMode", "autoCompactionEnabled", "autoRetryEnabled"):
            option = next((entry for entry in self.controls.options() if entry["id"] == config_id), None)
            if config_id in config and (not option or config[config_id] != option["currentValue"]):
                if not option or config[config_id] not in [entry["value"] for entry in option["options"]]:
                    self.recovery_warnings.append({"kind": "config_unavailable", "configId": config_id,
                        "previousValue": config[config_id], "message": f"Saved {config_id} is unavailable; using the native runtime's current selection."})
                elif config_id not in restored_selectors:
                    # A selection native's history restored wins. With none (no
                    # history, or native resumed without a model) the saved one applies.
                    await self.controls.set(config_id, config[config_id], publish=False)
        metadata = getattr(self.server, "client_capabilities", {}).get("_meta", {})
        if metadata.get("aelix/clientActions") is True and "ui/enable" in methods:
            await self.rpc("aelix_control", method="ui/enable", params=ui_enable_params(methods))
        if guard_selectors:
            await self.selector_startup(nonce, "finish")
        snapshot = None
        if managed is not None:
            snapshot = await managed.verify_start(
                self, require_complete=managed_change is not None or self._verifying_extension_recovery,
            )
        if fork_source is None and managed_change is None:
            await self.save(live=True)
        else:
            self.require_live_worker()
        return snapshot

    async def selector_startup(self, nonce, operation):
        result = await self.rpc("aelix_control", method="commands/run",
                                params={"text": f"/{SELECTOR_STARTUP_COMMAND} {operation} {nonce}"})
        messages = result.get("messages", [])
        if (result.get("outcome") != "handled" or len(messages) != 1
                or messages[0].get("text") != "selector-startup-ok"):
            raise ProtocolError("Aelix did not confirm the selector startup guard", -32004)

    async def open_fork(self, nonce, source):
        result = await self.rpc("aelix_control", method="commands/run",
                                params={"text": f"/aelix-acp-fork-head {nonce}"})
        try:
            reply = json.loads(result["messages"][0]["text"]) if result["outcome"] == "handled" else {}
        except (KeyError, IndexError, ValueError, TypeError):
            reply = {}
        if not isinstance(reply, dict):
            reply = {}
        if reply.get("error") == "locked":
            raise session_locked(source["sessionId"], "native")
        if reply.get("error") == "peer_locked":
            raise session_locked(source["sessionId"], "acp")
        if reply.get("error") == "changed":
            raise ProtocolError("Parent session changed while preparing the fork", -32000)
        if reply.get("error") == "head_changed":
            raise fork_head_changed()
        filename = reply.get("file")
        if filename != source["target"]:
            raise ProtocolError("Aelix could not create the native fork", -32004)
        switched = await self.rpc("switch_session", session_path=filename)
        if switched.get("cancelled"):
            raise ProtocolError("Aelix did not open the native fork", -32004)
        self.state = await self.rpc("get_state")
        if self.state.get("sessionFile") != filename:
            raise ProtocolError("Aelix did not open the native fork", -32004)

    def fork_history(self):
        path = Path(self.socket_dir) / "fork-history.jsonl"
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                yield json.loads(line)
        path.unlink()

    async def listen(self, socket_path, *, tcp):
        """Open this worker's approval channel."""
        self.permission_endpoint = None
        self.permission_token = None
        if tcp:
            # Windows has no private Unix socket here: a per-worker token
            # authenticates the loopback channel instead.
            self.permission_token = secrets.token_urlsafe(32)
            self.socket_server = await asyncio.start_server(self.permission, "127.0.0.1", 0, limit=PRE_AUTH_LIMIT, backlog=8)
            self.permission_endpoint = f"tcp://127.0.0.1:{self.socket_server.sockets[0].getsockname()[1]}"
        else:
            self.socket_server = await asyncio.start_unix_server(self.permission, path=socket_path, limit=PRE_AUTH_LIMIT)
            os.chmod(socket_path, 0o600)

    async def raise_if_locked(self):
        """Report native's writer-lock refusal as a structured, final error."""
        await self.settle_exit()
        if is_native_session_locked(self.stderr_tail.decode("utf-8", errors="replace")):
            raise session_locked(self.id, "native") from None

    async def settle_exit(self):
        # Native explains a startup refusal on stderr and exits. Let a worker
        # whose RPC stream already closed finish exiting and flush stderr.
        if self.proc is None or self.stderr_task is None or not self.rpc_closed:
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.proc.wait(), 2)
        if self.proc.returncode is not None:
            await asyncio.wait({self.stderr_task}, timeout=2)

    async def restore_tools(self):
        if self.active_tools is None:
            return
        try:
            if "tools/set_active" not in self.controls.capabilities.get("methods", []):
                raise ProtocolError("This Aelix runtime cannot restore the saved tool selection", -32004)
            resources = await self.rpc("aelix_control", method="resources/list", params={})
            available = {tool["name"] for tool in resources.get("tools", [])}
            selected = [name for name in self.active_tools if name in available]
            result = await self.rpc("aelix_control", method="tools/set_active", params={"names": selected})
            if not self.studio_resources.tools_match(result, selected):
                raise ProtocolError("Aelix did not restore the saved tool selection", -32004,
                                    {"kind": "restrictions_unenforceable", "methods": ["tools/set_active"]})
        except BaseException:
            if self.server.sessions.get(self.id) is self:
                self.studio_resources.mark_uncertain()
            raise

    async def reload_resources(self, arguments, *, timeout, refresh_first=False):
        try:
            result = await self.rpc("aelix_control", method="resources/reload", params=arguments, _timeout=timeout)
            if refresh_first:
                await self.controls.refresh()
            await self.restore_tools()
        except BaseException:
            if self.active_tools is not None and not self.studio_resources.uncertain:
                self.studio_resources.mark_uncertain()
            raise
        self.studio_resources.invalidate_pages()
        return result

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
        self.active_tools = None
        self.studio_resources.recorded = None
        self.studio_resources.invalidate_pages()

    def require_live_worker(self, expected=None, *, committed=False):
        proc, lease = self.proc, self.lease
        if (proc is None or proc.returncode is not None or lease is None or lease.closed
                or self.rpc_closed or self._close_task is not None
                or (expected is not None and (proc is not expected[0] or lease is not expected[1]))):
            kind = "session_worker_closed_after_commit" if committed else "session_worker_unavailable"
            raise ProtocolError("Aelix process closed its RPC stream", -32603, {"kind": kind})
        return proc, lease

    async def save(self, inherited_history=None, *, live=False, managed_commit=False):
        before_commit = None
        if live:
            operation = self.studio_resources.profile_operation or self.studio_resources.tool_operation
            restore_operation = self.studio_resources.restore_operation
            if restore_operation is not None:
                restore_operation.require_restore_current()
            worker = self.require_live_worker()

            def before_commit():
                if restore_operation is not None:
                    restore_operation.require_restore_current()
                elif operation is not None:
                    operation.require_current()
                self.require_live_worker(worker)
                if managed_commit:
                    self.extension_commit_started = True

        if inherited_history is not None:
            self.fork_projection = True
        row = (self.id, self.cwd, self.server.args.profile, self.state.get("sessionFile"),
               self.state.get("sessionName") or f"Aelix {self.server.args.profile}", int(time.time()))
        config = self.controls.selections()
        if not self.controls.model_selected():
            # Native has no model, e.g. the saved one is unavailable right now. Keep
            # the saved model and reasoning choice so a later start can restore them.
            config.update({config_id: value for config_id, value in self.saved_config.items()
                           if config_id in NATIVE_SESSION_SELECTORS and config_id not in config})
        preferences = {"permission_mode": self.controls.permission_mode, "extensions": self.extra_extensions,
            "config": config,
            **({"extensionOverrides": self.extension_overrides} if self.extension_overrides else {}),
            "lastContextMeasurement": self.context.last_measurement,
            "agent": self.controls.capabilities.get("activeAgent"),
            **self.studio_resources.preferences(),
            "activeTools": self.active_tools,
            "forkProjection": self.fork_projection,
            "forkHead": self.fork_head,
            "trustProject": self.trust_project, "agentsEnabled": self.agents_enabled,
            "backendMessageCount": self.state.get("messageCount", self.state.get("message_count", 0))}
        await write_store(self.server.args.state_dir, save_session, row, preferences, inherited_history,
                          before_commit=before_commit)
        self.saved_config = config
        if live:
            if restore_operation is not None:
                restore_operation.require_restore_current()
            self.require_live_worker(worker, committed=True)

    def selectors(self):
        result = self.controls.selectors()
        if self.native_transcript():
            result["_meta"][transcript.CAPABILITY] = transcript.VERSION
        if self.recovery_warnings:
            result["_meta"]["aelix/recoveryWarnings"] = self.recovery_warnings
        return result

    async def publish_recovery_warnings(self):
        for warning in self.recovery_warnings:
            await self.update({"sessionUpdate": "agent_message_chunk", "content": {
                "type": "text", "text": "Session recovery: " + warning["message"]}})

    async def publish_context_usage(self):
        try:
            await self.context.publish_usage()
        except (ProtocolError, ConnectionError):
            # An auxiliary snapshot must not replace the outcome of the turn.
            await self.context.invalidate_usage()

    async def compact(self, instructions):
        await self.context.start_compaction("manual")
        try:
            result = await self.rpc("compact", _timeout=getattr(self.server.args, "command_timeout", 600) or None,
                                    **({"customInstructions": instructions} if instructions.strip() else {}))
        except ProtocolError as error:
            if error.code == -32007:
                await self.close()
                # Keep the accepted ACP id and mutation lock. The next distinct
                # prompt reopens the saved native file without replaying work.
                self.compaction_recovery_needed = True
                self.server.sessions[self.id] = self
                await self.context.invalidate_usage()
                await self.context.finish_compaction("unknown")
                raise after_dispatch(ProtocolError("Aelix compaction outcome is unknown after the deadline", -32603,
                                                   error.data)) from error
            await self.context.finish_compaction("failed")
            await self.publish_context_usage()
            if isinstance(error, BackendError) and error.data["command"] == "compact" and str(error) == "Nothing to compact":
                # Pinned native reports this explicit no-op before starting the
                # summary. Complete the ACP turn so the host keeps its prelude.
                await self.update({"sessionUpdate": "agent_message_chunk",
                                   "content": {"type": "text", "text": str(error)}})
                return
            raise
        else:
            await self.context.finish_compaction("completed", result)
            await self.publish_context_usage()
            with contextlib.suppress(ProtocolError, ConnectionError):
                self.state = await self.rpc("get_state", _timeout=5)
            if self.lease is not None:
                try:
                    await self.save()
                except ProtocolError as error:
                    write_diagnostic(error.data["kind"])

    async def rpc(self, command, *, _timeout=45, _image_write=None, _project_history=False, _await_response=True,
                  _before_native_write=None, **kwargs):
        request_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future() if _await_response else None
        if future is not None:
            self.pending[request_id] = future
        if _project_history:
            self.history_requests.add(request_id)
        if (task := asyncio.current_task()) in self.interventions:
            self.interventions[task] = future  # raise_fence reads whether native answered.
        try:
            if self.rpc_closed:
                raise ProtocolError(self.failure or "Aelix RPC stream is closed", -32603)
            if not self.proc or self.proc.returncode is not None:
                raise ProtocolError("Aelix child is not running", -32603)
            frame = {"id": request_id, "type": command, **kwargs}
            if _image_write is None:
                raw = (json.dumps(frame) + "\n").encode()
            else:
                raw = encode_frame(frame)
                if len(raw) > image_policy.MAX_NATIVE_LINE:
                    raise image_policy.ImageRejected("native-line-limit")
                current, issued = _image_write
                if not current():
                    raise image_policy.ImageRejected("stale-turn")
                issued()  # write() can fail after handing bytes to the pipe.
            if _before_native_write is not None:
                _before_native_write()
            self.proc.stdin.write(raw)
            await self.proc.stdin.drain()
            # Queue writes have their own receipts. A late drain must not
            # change the dispatch outcome tracked for another prompt.
            if self.turn_active and command not in ("steer", "follow_up") and command not in READ_ONLY_COMMANDS and not (
                    command == "aelix_control" and kwargs.get("method") in READ_ONLY_CONTROLS | CONCURRENT_CONTROLS):
                self.dispatched = True
            if future is None:
                return
            try:
                return await asyncio.wait_for(future, _timeout)
            except TimeoutError:
                if command == "aelix_control" and kwargs.get("method") in LONG_CONTROLS:
                    with contextlib.suppress(ProtocolError, TimeoutError):
                        await self.rpc("aelix_control", method="commands/cancel", params={}, _timeout=5)
                elif command == "compact":
                    with contextlib.suppress(ProtocolError, TimeoutError):
                        await self.rpc("abort", _timeout=5)
                raise ProtocolError(f"Aelix RPC {command} timed out", -32007, {"kind": "timeout", "command": command}) from None
        finally:
            self.pending.pop(request_id, None)
            if _project_history:
                self.history_requests.discard(request_id)

    async def read_stderr(self):
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while chunk := await self.proc.stderr.read(4096):
            # Bounded copy for native refusals reported only on stderr.
            self.stderr_tail = (self.stderr_tail + chunk)[-STDERR_TAIL:]
            if not self.private_host:
                text = f"[aelix:{self.id[:8]}] " + decoder.decode(chunk)
                await self.server.forward_stderr(text.encode("utf-8"))

    async def read_native_history(self):
        try:
            messages = await self.rpc("get_messages", _project_history=True)
        except (ProtocolError, ConnectionError, OSError):
            raise native_history.unavailable() from None
        if not isinstance(messages, list):
            raise native_history.unavailable()
        return messages

    async def rpc_frames(self):
        async for line, envelope, projected in native_frames.read_frames(
            self.proc.stdout, private=self.private_host, wants_history=self.history_requests.__contains__
        ):
            if projected is not None:
                request_id, messages = projected
                future = self.pending.get(request_id)
                if future is not None and not future.done():
                    if messages is None:
                        future.set_exception(native_history.unavailable())
                    else:
                        future.set_result(messages)
                # The suspended reader must not retain the future or its result.
                del projected, messages, future
            elif envelope is not None:
                if response := native_frames.RESPONSE.match(line):
                    future = self.pending.get(response["id"].decode("ascii"))
                    if future is not None and not future.done():
                        future.set_exception(ProtocolError("Native response exceeds the frame limit", -32603))
                elif envelope:
                    await self.event(envelope)
            else:
                yield line

    async def read_rpc(self):
        self.reader = asyncio.current_task()
        try:
            async for line in self.rpc_frames():
                try:
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise ValueError("RPC frame must be an object")
                except ValueError:
                    write_diagnostic("[aelix-acp] Ignored non-JSON child stdout")
                    continue
                future = self.pending.get(event.get("id"))
                if event.get("type") == "response" and future is not None:
                    if not future.done():
                        if event.get("success"):
                            future.set_result(event.get("data") or {})
                        elif is_native_session_locked(event.get("error", "")):
                            future.set_exception(session_locked(self.id, "native"))
                        else:
                            detail = "Native RPC failed" if self.private_host else str(event.get("error", "RPC failed"))
                            error = BackendError(detail, event.get("command", "unknown"))
                            error.studio_tool_lock = tool_lock_key(event.get("error"))
                            future.set_exception(error)
                else:
                    await self.event(event)
        except Exception as exc:
            self.failure = "Aelix stream failed" if self.private_host else f"Aelix stream failed: {exc}"
        finally:
            self.rpc_closed = True
            self.invalidate_image_acceptance()
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

    def clear_image_sample(self):
        if self.socket_dir and self.image_epoch:
            with contextlib.suppress(OSError):
                (Path(self.socket_dir) / "image-state.json").unlink(missing_ok=True)

    async def event(self, event):
        kind = event.get("type")
        if kind in ("compaction_start", "compaction_end"):
            if kind == "compaction_start":
                self.clear_image_sample()
            await self.context.native_compaction_event(event)
        elif kind == "message_start" and (event.get("message") or {}).get("role") == "assistant":
            self.failure = None
            self.tool_origin = None
            self.tool_occurrences.clear()
        elif kind == "message_update":
            update = event.get("assistantMessageEvent") or event.get("assistant_message_event") or {}
            if update.get("type") in ("text_delta", "thinking_delta") and update.get("delta"):
                chunk = {"sessionUpdate": "agent_thought_chunk" if update["type"] == "thinking_delta"
                         else "agent_message_chunk", "content": {"type": "text", "text": update["delta"]}}
                await self.native_update(chunk, event, "assistant")
        elif kind in ("tool_execution_start", "tool_execution_update", "tool_execution_end"):
            call_id = event.get("toolCallId") or event.get("tool_call_id")
            name = event.get("toolName") or event.get("tool_name") or "tool"
            if kind == "tool_execution_start":
                # Aelix emits execution_start before the extension consent hook.
                self.subagents.tool_started(call_id, name)
                self.children.call_started(call_id)
                call = tool_call(call_id, name, event.get("args") or {}, self.cwd, "pending")
                known = call_id in self.tools
                self.tools[call_id] = call
                self.tool_arrived.set()
                await self.native_update({"sessionUpdate": "tool_call_update" if known else "tool_call", **call},
                                         event, "toolResult")
            else:
                result = event.get("result") or event.get("partialResult") or event.get("partial_result") or {}
                failed = event.get("isError", event.get("is_error", False)) or result.get("is_error", result.get("isError", False))
                update = {"sessionUpdate": "tool_call_update", "toolCallId": call_id,
                          "status": ("failed" if failed else "completed") if kind.endswith("end") else "in_progress",
                          **({} if self.private_host else {"rawOutput": result}),
                          "content": [{"type": "content", "content": {"type": "text", "text": text_content(result.get("content"))}}]}
                if kind.endswith("end"):
                    self.subagents.tool_ended(call_id)
                # Every frame of a delegating call carries its latest children,
                # so a client that keeps only the newest frame still has them.
                if meta := self.subagents.meta(call_id):
                    update["_meta"] = meta
                await self.native_update(update, event, "toolResult")
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
                            detail = type(exc).__name__ if self.private_host else str(exc)
                            write_diagnostic(f"[aelix-acp] Standalone preview unavailable: {detail}")
        elif kind == "message_end":
            message = event.get("message") or {}
            error = message.get("errorMessage") or message.get("error_message")
            if error:
                self.failure = "Native model request failed" if self.private_host else error
                self.clear_image_sample()
            await self.settle_message(event, message)
        elif kind == "aelix_subagent":
            await self.subagent_progress(event.get("event"), event.get("data") or {})
        elif kind == "aelix_child":
            await self.child_progress(event.get("event"), event.get("data"))
        elif kind == "aelix_notice":
            # aelix-ai #612: a job notice waits; the client wakes the session with
            # _aelix/manage children/notices. Only a client that asked gets it.
            if self.declares(NOTICES):
                await self.notify("_aelix/notice", {
                    "sessionId": self.id, "event": event.get("event"),
                    "data": event.get("data") if isinstance(event.get("data"), dict) else {}})
        elif kind == "extension_ui_request":
            if self.ending:
                # Native is being ended while its frames are still read: no one is asked.
                if event.get("method") not in UI_NOTIFICATIONS and self.proc and self.proc.returncode is None:
                    with contextlib.suppress(ConnectionError, RuntimeError):
                        self.proc.stdin.write((json.dumps({"type": "extension_ui_response", "id": event.get("id"),
                                                           "cancelled": True}) + "\n").encode())
                return
            # A child's question is registered before its task runs: a frame read meanwhile can void it.
            void = self.open_question(event) if event.get("method") == "childApproval" else None
            task = asyncio.create_task(self.handle_ui(event, void))
            self.tasks.add(task)
            self.ui_tasks.add(task)
            task.add_done_callback(self.tasks.discard)
            task.add_done_callback(self.ui_tasks.discard)
            if event.get("method") not in UI_NOTIFICATIONS:
                self.unstarted.add(task)  # handle_ui's first step takes it out
                task.add_done_callback(lambda ended: self.answer_unstarted(ended, event, void))

    def answer_unstarted(self, task, event, void):
        """A dialog's task ended before it first ran: native's one answer is `{cancelled: true}` (fix round 2, F2).

        A `session/cancel` handled in the loop turn that created the task cancels
        it before its coroutine starts, so neither its client request nor the
        answer `handle_ui` writes (a child question's in its `finally`, an
        extension dialog's after its try) happens, and native would wait its 300 s
        (one child question at a time, so the next child's too). A child's
        question and an extension's dialog alike get the cancelled answer that
        one cancelled a turn later gets.
        """
        if task not in self.unstarted:
            return
        self.unstarted.discard(task)
        if void is not None:
            self.questions.pop(void, None)
        if self.proc and self.proc.returncode is None and not self.rpc_closed:
            with contextlib.suppress(ConnectionError, RuntimeError):
                self.proc.stdin.write((json.dumps({"type": "extension_ui_response", "id": event.get("id"),
                                                   "cancelled": True}) + "\n").encode())

    async def subagent_progress(self, name, data):
        """Show a delegated child on its parent `agent` tool call (standard ACP)."""
        parent, meta = self.subagents.record(name, data)
        if parent is None:
            # No parent tool call (for example `/agents run`): keep the
            # extension notification. Standard clients ignore it.
            await self.notify("_aelix/subagent", {"sessionId": self.id, "event": name, "data": data})
            return
        if meta is None:
            return
        update = {"sessionUpdate": "tool_call_update", "toolCallId": parent, "_meta": meta}
        if parent == self.subagents.open_call:
            update["status"] = "in_progress"
        await self.update(update)

    def declares(self, capability):
        metadata = getattr(self.server, "client_capabilities", {}).get("_meta") or {}
        return metadata.get(capability) is True

    async def child_progress(self, name, data):
        """One native `aelix_child` frame (aelix-ai #612) as standard ACP updates.

        Every client gets the child's `lastText`, `stuck`, `wrappingUp` and `end` on
        its parent card's `_meta["aelix/subagents"]`. A client that declared
        `aelix/nestedToolCalls` also gets the child's own calls as tool calls
        `"<child id>:<call id>"` with `_meta.parentToolCallId`, and a child with no
        parent call (`/agents run`) as a top-level card `aelix-child:<id>`.
        """
        epoch = self.children.epoch
        routed = self.children.accept(name, data)
        if self.children.epoch != epoch:
            self.epoch_changed()
        if isinstance(data, dict) and (terminal(name, data) or (
                name == "approval" and data.get("step") in ("withdrawn", "denied"))):
            # Native has dropped the question (fix round 1, P-LOW-1): the child ended, or
            # its request was withdrawn or answered without this client (its 300 s, a plan).
            asked = (data.get("epoch"), data.get("id"), *([data.get("callId")] if name == "approval" else []))
            self.void_questions(lambda key: key[:len(asked)] == asked)
        if routed is None:
            return
        key, parent = routed
        epoch, child_id = key
        nested = self.declares(NESTED_TOOL_CALLS)
        card = parent or SYNTHETIC_PREFIX + child_id
        ended = terminal(name, data)
        child = {"id": child_id, "epoch": epoch, "seq": data["seq"], "event": name}
        if ended:
            child["end"] = data.get("end")
        if parent is None and not nested:
            # Without nesting an unparented child keeps `_aelix/subagent` (subagent_progress).
            if ended:
                self.children.ended(key)
            return
        if parent is None and name == "state" and card not in self.child_calls:
            self.child_calls.add(card)
            await self.update({"sessionUpdate": "tool_call", "toolCallId": card, "kind": "other",
                               "title": f"agent {data.get('profile') or ''}".strip(), "status": "in_progress",
                               "_meta": {CHILD_META: child}})
        running = None
        if name in ("tool_start", "tool_end") and isinstance(data.get("callId"), str):
            tool = data.get("name") if isinstance(data.get("name"), str) else "tool"
            # The tool a child runs, on its entry for every client, as aelix_subagent's
            # currentTool did for a foreground child (fix round 1, Codex C-L7).
            running = {"currentTool": self.children.tool(key, data["callId"], tool if name == "tool_start" else None)}
        if nested and name in ("tool_start", "tool_end", "approval") and isinstance(data.get("callId"), str):
            await self.child_tool(name, data, card, child)
        fields = {"state": {"profile": data.get("profile"), "state": data.get("state"),
                            "wrappingUp": data.get("wrappingUp") is True},
                  "text": {"lastText": data.get("text")}, "stuck": {"stuck": data.get("stuck")},
                  "wrap_up": {"wrappingUp": data.get("requested") is True},
                  "tool_start": running, "tool_end": running}.get(name)
        if fields is not None:
            if ended:
                fields.update(end=data.get("end"), currentTool=None)
            # Onto what this child showed before: the next prompt's clear dropped its entry (V-F1).
            meta = self.subagents.merge(card, child_id, self.children.remember(key, fields))
            closing = ended and parent is None
            if meta is not None or closing:
                update = {"sessionUpdate": "tool_call_update", "toolCallId": card,
                          "_meta": {**(meta or self.subagents.meta(card) or {}), CHILD_META: child}}
                if closing:
                    update["status"] = END_STATUS.get(data.get("end"), "failed")
                elif card == self.subagents.open_call:
                    update["status"] = "in_progress"
                await self.update(update)
        if ended:
            self.children.ended(key)
            if parent is None:
                self.child_calls.discard(card)

    def open_question(self, event):
        """A child's question opens: the future that voids it, keyed by (epoch, child id, call id)."""
        child, tool = event.get("child"), event.get("tool")
        key = (child.get("epoch"), child.get("id"), tool.get("callId")) if (
            isinstance(child, dict) and isinstance(tool, dict)) else (None, None, None)
        void = asyncio.get_running_loop().create_future()
        self.questions[void] = key
        return void

    def void_questions(self, matches):
        """End the adapter's wait on each open child question whose key `matches`: native has dropped it."""
        for void, key in list(self.questions.items()):
            if not void.done() and matches(key):
                void.set_result(None)

    def epoch_changed(self):
        """Native's epoch is newer (a frame, or a children/list or children/events reply): its questions are void."""
        current = self.children.epoch
        self.void_questions(lambda key: key[0] != current)

    def observe_epoch(self, epoch):
        if isinstance(epoch, str) and epoch:
            before = self.children.epoch
            self.children.observe(epoch)
            if self.children.epoch != before:
                self.epoch_changed()

    async def child_tool(self, name, data, card, child):
        call_id = f"{child['id']}:{data['callId']}"
        meta = {"parentToolCallId": card, CHILD_META: child}
        if name == "tool_start":
            tool = data.get("name") if isinstance(data.get("name"), str) else "tool"
            update = {"sessionUpdate": "tool_call", "toolCallId": call_id, "title": tool,
                      "kind": KINDS.get(tool, "other"), "status": "in_progress", "_meta": meta}
            if isinstance(data.get("args"), str) and data["args"]:
                update["content"] = [{"type": "content", "content": {"type": "text", "text": data["args"]}}]
        elif name == "tool_end":
            update = {"sessionUpdate": "tool_call_update", "toolCallId": call_id,
                      "status": TOOL_STATUS.get(data.get("status"), "failed"), "_meta": meta}
        else:
            status = {"asked": "pending", "allowed": "in_progress"}.get(data.get("step"))
            if status is None:
                return  # A denied or withdrawn call ends with its tool_end.
            update = {"sessionUpdate": "tool_call_update", "toolCallId": call_id, "status": status, "_meta": meta}
        await self.update(update)

    def native_transcript(self):
        return transcript.supported(self.controls.capabilities)

    def transcript_enabled(self):
        return self.native_transcript() and transcript.offered(getattr(self.server, "client_capabilities", {}))

    async def native_update(self, update, event, role):
        supported = self.native_transcript()
        coordinates = transcript.live(event, role) if supported else None
        recorded_meta = {}
        if coordinates:
            if self.transcript_enabled():
                update = transcript.tagged(update, coordinates)
            elif role == "toolResult":
                recorded_meta[transcript.TOOL_STREAM_META] = coordinates
        if event["type"] == "tool_execution_start" and supported:
            call_id = update["toolCallId"]
            origin = None
            if self.tool_origin is not None:
                # The stored assistant can differ from the live message_end
                # payload. Native starts tools in the accepted source order.
                origin = {**self.tool_origin, "occurrence": self.tool_occurrences[call_id]}
                self.tool_occurrences[call_id] += 1
            if coordinates is not None:
                self.pending_tool_results[transcript.stream_key(coordinates)] = (call_id, origin)
                # These children were already known when this execution
                # started. Their late progress can enter the live raw-ID
                # rollup but belongs outside this occurrence on replay.
                previous_children = set(self.subagents.children.get(call_id, ())) | {
                    child_id for (_, child_id), parent in self.children.parents.items() if parent == call_id}
                if previous_children:
                    recorded_meta[transcript.TOOL_STREAM_META] = {
                        **coordinates, "previousChildren": sorted(previous_children)}
            recorded_meta[transcript.TOOL_ORIGIN_META] = origin
        recorded = {**update, "_meta": {**update.get("_meta", {}), **recorded_meta}} if recorded_meta else None
        await self.record_update(update, recorded=recorded)

    async def settle_message(self, event, message):
        role = message.get("role")
        if not self.native_transcript() or not isinstance(role, str) or role not in transcript.ROLES:
            return
        coordinates = transcript.live(event, role)
        if coordinates is None:
            return
        if role == "assistant":
            self.tool_origin = ({key: coordinates[key] for key in ("nativeSessionId", "entryId")}
                                if coordinates["status"] == "stored" else None)
            self.tool_occurrences.clear()
        elif role == "toolResult":
            linked = self.pending_tool_results.pop(transcript.stream_key(coordinates), None)
            if linked and coordinates["status"] == "stored":
                self.live_history.append(transcript.recorded_result(*linked, coordinates))
        if not self.transcript_enabled():
            return
        if role == "user":
            for index, update in enumerate(self.server.native_message_updates(self, message, canonical=True)):
                chunk = transcript.tagged(update, {**coordinates, "part": index})
                await self.notify("session/update", {"sessionId": self.id, "update": chunk})
        await self.notify(transcript.SETTLED, {"sessionId": self.id, **coordinates})

    def adapter_message(self, update, *, source="adapter"):
        self.adapter_event_sequence += 1
        return transcript.tagged(update, {"source": source, "eventId": uuid.uuid4().hex,
                                          "bindingId": self.adapter_event_binding,
                                          "sequence": self.adapter_event_sequence})

    async def update(self, update):
        if (update.get("sessionUpdate") in ("user_message_chunk", "agent_message_chunk", "agent_thought_chunk")
                and transcript.supported(self.controls.capabilities)):
            update = self.adapter_message(update)
        await self.record_update(update)

    async def record_update(self, update, *, recorded=None):
        self.live_history.append(update if recorded is None else recorded)
        await self.notify("session/update", {"sessionId": self.id, "update": update})

    async def notify(self, method, params):
        """One notification about this session (`session/update`, `_aelix/notice`, `_aelix/subagent`).

        While the worker is ended, its last frames reach a client still here that
        was not told this session was replaced (fix round 2, N1: the two `_aelix/`
        notifications too); a `session/update` is recorded either way.
        """
        message = {"jsonrpc": "2.0", "method": method, "params": params}
        if not self.ending:
            await self.server.send(message)
        elif not getattr(self.server, "closing", False) and self.server.sessions.get(self.id) is self:
            with contextlib.suppress(ConnectionError, OSError):
                await self.server.send(message)

    async def permission(self, reader, writer):
        owner = asyncio.current_task()
        allowed = False
        try:
            # Loopback peers are unauthenticated until they present the token.
            if self.ending or len(self.permission_connections) >= (8 if self.permission_endpoint else 32):
                return
            self.permission_connections.add(owner)
            if self.permission_endpoint or self.permission_token is not None:
                # The token arrives on its own line, read under the small
                # stream limit; only an authenticated peer may send a large
                # request (tool arguments can carry whole files).
                async with asyncio.timeout(5):
                    hello = json.loads(await reader.readline())
                token = hello.get("token") if isinstance(hello, dict) else None
                if not self.permission_token or not isinstance(token, str) or not hmac.compare_digest(token, self.permission_token):
                    return
            async with asyncio.timeout(30 if self.permission_endpoint else 300):
                request = json.loads(await read_line(reader, FRAME_LIMIT))
            if not isinstance(request, dict):
                return
            async with asyncio.timeout(300):
                while request.get("toolCallId") not in self.tools:
                    self.tool_arrived.clear()
                    if self.cancelled or self.rpc_closed:
                        return
                    await self.tool_arrived.wait()
                existing = self.tools[request["toolCallId"]]
                if existing.get("title") != request.get("name") or self.cancelled:
                    return
                if self.controls.permission_mode == "yolo":
                    allowed = True
                else:
                    call = tool_call(request["toolCallId"], request["name"], request["args"], self.cwd)
                    self.tools[call["toolCallId"]] = call
                    await self.update({"sessionUpdate": "tool_call_update", **call})
                    if self.cancelled:
                        return
                    permission_task = asyncio.current_task()
                    self.permissions.add(permission_task)
                    try:
                        answer = await self.server.client_request("session/request_permission", {
                            "sessionId": self.id, "toolCall": call,
                            "options": [{"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
                                        {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"}]})
                        outcome = answer.get("outcome") or {}
                        allowed = outcome.get("outcome") == "selected" and outcome.get("optionId") == "allow-once"
                    finally:
                        self.permissions.discard(permission_task)
        except (Exception, asyncio.CancelledError):
            allowed = False
        finally:
            try:
                granted = allowed and not self.cancelled
                writer.write((json.dumps({"allow": granted}) + "\n").encode())
                # This socket carries only this tiny reply. Send it before
                # publishing progress, which can wait on the ACP stdout pipe.
                await writer.drain()
                writer.close()
                if granted:
                    await self.update({"sessionUpdate": "tool_call_update",
                                       "toolCallId": request["toolCallId"], "status": "in_progress"})
            except (ConnectionError, OSError):
                pass
            finally:
                self.permission_connections.discard(owner)
                writer.close()
                with contextlib.suppress(ConnectionError):
                    await writer.wait_closed()

    async def commands(self):
        data = await self.rpc("get_commands")
        dynamic = data.get("commands", [])
        if self.server.args.profile == "analytics" and not any(c.get("name") == "analytics" for c in dynamic):
            raise ProtocolError("The Analytics extension did not register its command; refusing this profile", -32603)
        catalog = [{"name": name, "description": description} for name, description in BUILTINS.items()]
        catalog.extend({"name": c["name"], "description": c.get("description") or c["name"]}
                       for c in dynamic if c.get("name") not in BUILTINS and c.get("name") not in PRIVATE_NATIVE_COMMANDS)
        await self.update({"sessionUpdate": "available_commands_update", "availableCommands": catalog})

    async def handle_ui(self, event, void=None):
        self.unstarted.discard(asyncio.current_task())  # From here on the answer is this task's (answer_unstarted).
        method = event.get("method")
        if method == "authProgress":
            await self.server.send({"jsonrpc": "2.0", "method": "_aelix/notification",
                                    "params": {"sessionId": self.id, "event": event}})
            return
        if method in UI_NOTIFICATIONS:
            text = event.get("message") or event.get("text")
            if text:
                await self.update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": str(text)}})
            return
        if method == "childApproval":
            await self.child_approval(event, void)
            return
        answer = {"cancelled": True}
        try:
            answer = await self.server.client_action(self, "dialog", event)
        except (ProtocolError, asyncio.CancelledError):
            pass
        if self.proc and self.proc.returncode is None:
            self.proc.stdin.write((json.dumps({"type": "extension_ui_response", "id": event.get("id"), **answer}) + "\n").encode())
            await self.proc.stdin.drain()

    async def child_approval(self, event, void=None):
        """A delegated child asks before a change (aelix-ai #612, ADR-0308): `session/request_permission`.

        Answered from this task, outside mutation_lock, so a parent's own request
        (the bridge) and a child's can be open together (K13). Every ending that
        is not an offered choice is `{cancelled: true}`: `session/cancel`, a client
        error (K16), and a request of an epoch native has since replaced, which is
        answered without asking. The parent's own approvals stay on the bridge.

        A question native drops while it is open is void (fix round 1, P-LOW-1): the
        child's end, its request's `withdrawn` or `denied` step, a newer epoch. The
        adapter then takes the request back from the client (`$/cancel_request`)
        and answers native `{cancelled: true}`, which it ignores.
        """
        if void is None:
            void = self.open_question(event)
        answer = {"cancelled": True}
        try:
            child, tool = event.get("child"), event.get("tool")
            choices = {choice.get("kind"): choice.get("id") for choice in event.get("choices") or []
                       if isinstance(choice, dict) and choice.get("kind") in CHILD_APPROVAL_OPTIONS
                       and isinstance(choice.get("id"), str)}
            if (not isinstance(child, dict) or not isinstance(tool, dict) or not choices
                    or not isinstance(child.get("id"), str) or not isinstance(tool.get("callId"), str)
                    or self.children.stale(child.get("epoch")) or void.done()
                    or (self.cancelled and self.done is not None and not self.done.done())):
                return
            name = tool.get("name") if isinstance(tool.get("name"), str) else "tool"
            args = tool.get("args") if isinstance(tool.get("args"), dict) else {}
            cwd = child.get("cwd") if isinstance(child.get("cwd"), str) else self.cwd
            call = tool_call(f"{child['id']}:{tool['callId']}", name, args, cwd)
            nested = self.declares(NESTED_TOOL_CALLS)
            if not nested and isinstance(event.get("title"), str) and event["title"]:
                # This id names no card such a client has: native's title says who asks for what. A
                # nesting client keeps its nested card's own title, the tool's name (fix round 1, NIT-5).
                call["title"] = event["title"]
            meta = {CHILD_META: {key: child.get(key) for key in ("id", "epoch", "profile", "jobId", "background")}}
            if nested:
                parent = child.get("parentToolCallId")
                meta["parentToolCallId"] = (self.children.card_of(parent) if isinstance(parent, str) and parent
                                            else SYNTHETIC_PREFIX + child["id"])
            call["_meta"] = meta
            options = [{"optionId": CHILD_APPROVAL_OPTIONS[kind][0], "name": CHILD_APPROVAL_OPTIONS[kind][1], "kind": kind}
                       for kind in CHILD_APPROVAL_OPTIONS if kind in choices]
            reply = await self.ask_client({"sessionId": self.id, "toolCall": call, "options": options}, void)
            if reply is None:
                return
            outcome = reply.get("outcome") or {}
            selected = next((choices[kind] for kind in choices if outcome.get("outcome") == "selected"
                             and outcome.get("optionId") == CHILD_APPROVAL_OPTIONS[kind][0]), None)
            if selected is not None:
                answer = {"value": selected}
        except (Exception, asyncio.CancelledError):
            answer = {"cancelled": True}
        finally:
            self.questions.pop(void, None)
            if self.proc and self.proc.returncode is None and not self.rpc_closed:
                with contextlib.suppress(ConnectionError, RuntimeError):
                    self.proc.stdin.write((json.dumps({"type": "extension_ui_response", "id": event.get("id"),
                                                       **answer}) + "\n").encode())
                    await self.proc.stdin.drain()

    async def end_children(self):
        """The worker is gone: close each card its live children left open (fix round 1, Codex C-M3, P-LOW-2).

        Native's own terminal frames, read through its shutdown, close the cards of
        the children it ended. For a child still live when the worker was gone, its
        own calls still open become `failed`, and its entry on the card gets state
        and end `stopped` (native's end for a child a stop ended), with
        `_meta["aelix/child"].closedBy = "adapter"`; its top-level card closes
        `failed`. Then the epoch is retired: a frame or question of it is stale.
        """
        nested = self.declares(NESTED_TOOL_CALLS)
        for key, parent, seq, calls in self.children.live():
            epoch, child_id = key
            card = parent or SYNTHETIC_PREFIX + child_id
            child = {"id": child_id, "epoch": epoch, "seq": seq, "event": "state", "end": "stopped",
                     "closedBy": "adapter"}
            if nested:
                for call in calls:
                    await self.update({"sessionUpdate": "tool_call_update", "toolCallId": f"{child_id}:{call}",
                                       "status": "failed", "_meta": {"parentToolCallId": card, CHILD_META: child}})
            if parent is None and not nested:
                continue
            meta = self.subagents.merge(card, child_id, self.children.remember(
                key, {"state": "stopped", "end": "stopped", "currentTool": None}))
            update = {"sessionUpdate": "tool_call_update", "toolCallId": card,
                      "_meta": {**(meta or self.subagents.meta(card) or {}), CHILD_META: child}}
            if parent is None:
                update["status"] = END_STATUS.get("stopped", "failed")
                self.child_calls.discard(card)
            await self.update(update)
        self.children.retire()

    async def ask_client(self, params, void):
        """The client's reply to a child's question, or None once the question is void (its request taken back)."""
        request = asyncio.ensure_future(self.server.client_request("session/request_permission", params, withdraw=True,
                                                                  timeout=CHILD_QUESTION_SECONDS))
        try:
            await asyncio.wait({request, void}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if not request.done():
                request.cancel()
                await asyncio.gather(request, return_exceptions=True)
        return None if void.done() else request.result()

    async def restart(self, *, extensions=None, trust_project=None, agents_enabled=None, preserve_history=False,
                      preserve_cancellation=False):
        if self.id in self.server.starting_ids:
            raise ProtocolError("Session is already starting", -32000)
        self.server.starting_ids.add(self.id)
        if preserve_cancellation:
            self.server.pending_stop_sessions[self.id] = self
        # Children actions are refused until this returns; close() below ends those in flight.
        self.changing += 1
        self.invalidate_image_acceptance()
        if self.turn_active:
            self.dispatched = True  # Replacing the worker is itself an effect.
        held_lease = self.lease
        self.lease = None
        managed_change = self._pending_extension_change
        self._pending_extension_change = None
        try:
            if preserve_history:
                if held_lease is None:
                    held_lease = acquire_lease(self.server.args.state_dir, self.id)
                row = self.server.db.execute("SELECT cwd,profile,file FROM sessions WHERE id=?", (self.id,)).fetchone()
                if not row or (row[0], row[1]) != (self.cwd, self.server.args.profile):
                    raise ProtocolError("Saved session does not match this workspace and profile", -32004)
                filename = row[2]
                if not filename or not Path(filename).is_file():
                    if load_preferences(self.server.db, self.id).get("backendMessageCount") != 0:
                        raise ProtocolError("No persisted Aelix session file is available", -32004)
                    filename = None
            else:
                filename = self.state.get("sessionFile")
                if filename and not Path(filename).is_file():
                    filename = None
            await self.close()
            self.lease, held_lease = held_lease, None
            self.tasks = set()
            self._close_task = None
            self.failure = None
            self.rpc_closed = False
            if not preserve_cancellation:
                self.reset_cancellation()
            if extensions is not None:
                self.extra_extensions = extensions
            if trust_project is not None:
                self.trust_project = trust_project
            if agents_enabled is not None:
                self.agents_enabled = agents_enabled
            async with self.publication_lock:
                if not preserve_history:
                    await self.save()
                if managed_change is not None or self.studio_resources.recorded is not None:
                    # Only Stop can address the candidate before publication.
                    self.server.pending_stop_sessions[self.id] = self
                snapshot = await self.start(filename, managed_change=managed_change)
                await self.commands()
                if preserve_history:
                    await self.update({"sessionUpdate": "current_mode_update",
                                       "currentModeId": self.controls.permission_mode})
                    await self.update({"sessionUpdate": "config_option_update", "configOptions": self.controls.options()})
                await self.publish_context_usage()
                await self.publish_recovery_warnings()
                await self.save(live=True, managed_commit=managed_change is not None)
                self.server.sessions[self.id] = self
            return snapshot
        except BaseException:
            if held_lease is not None:
                if preserve_history:
                    held_lease.close()
                else:
                    self.lease = held_lease
            await self.close()
            raise
        finally:
            # Compaction recovery retains the session handle even if the
            # replacement worker closed. Its publication attempt has ended.
            self.studio_resources.restore_operation = None
            self.extension_commit_started = False
            self.server.pending_stop_sessions.pop(self.id, None)
            self.server.starting_ids.discard(self.id)
            self.changing -= 1

    def invalidate_image_acceptance(self):
        if self.input_relay is not None:
            self.input_relay.invalidate_images()

    def raise_fence(self):
        """A close begins (aelix-ai #612; a restart's too): end the child interventions waiting for native.

        Each one native has not answered yet has its line on native's stdin, so it
        answers that its delivery is unknown (`intervene`). One native answered
        before the fence rose returns that answer, on every Python version (fix
        round 1, Codex C-M1: 3.11's `wait_for` returned it and 3.12's raised).
        New ones are refused until the fence is lowered. Only this ends any:
        restart()'s own raise of the counter, before its close, refuses new
        ones alone (fix round 2, F1).
        """
        self.changing += 1
        self.invalidate_image_acceptance()
        for task, answer in list(self.interventions.items()):
            if not task.done() and (answer is None or not answer.done()):
                self.fenced.add(task)
                task.cancel()

    async def intervene(self, action, arguments):
        """One CONCURRENT_CONTROLS request: outside mutation_lock, during a turn or agents/run."""
        if (self.changing or self._close_task is not None or self.rpc_closed
                or self.proc is None or self.proc.returncode is not None):
            raise session_changing()
        task = asyncio.current_task()
        # No await between the check above and the line's write in rpc().
        self.interventions[task] = None
        try:
            try:
                answer = await self.rpc("aelix_control", method=action, params=arguments, _timeout=60)
            except (asyncio.CancelledError, ProtocolError):
                if task not in self.fenced:
                    raise
            else:
                if task not in self.fenced:
                    if action in ("children/list", "children/events") and isinstance(answer, dict):
                        self.observe_epoch(answer.get("epoch"))  # P-LOW-1: a reply names native's epoch too
                    return answer
                # Python 3.11's wait_for returns an answer that came after the fence's cancel.
            if task.cancelling():
                task.uncancel()
            raise delivery_unknown(action) from None
        finally:
            self.interventions.pop(task, None)
            self.fenced.discard(task)

    async def apply_extension_override(self, change):
        # The caller holds the mutation lock. Only this restart may use the
        # candidate. A recovery restart always reloads durable preferences.
        self._pending_extension_change = change
        try:
            return await self.restart(preserve_history=True)
        except (Exception, asyncio.CancelledError) as failure:
            self._pending_extension_change = None
            cancelled = isinstance(failure, asyncio.CancelledError)
            if cancelled and not self.cancelled:
                raise  # Adapter shutdown.
            data = getattr(failure, "data", None) or {}
            recovered = False
            self._verifying_extension_recovery = True
            try:
                await self.restart(preserve_history=True)
                recovered = True
            except Exception as recovery_error:
                write_diagnostic(f"[aelix-acp] Extension selection recovery failed: {type(recovery_error).__name__}")
            finally:
                self._verifying_extension_recovery = False
            details = {
                "kind": "managed_extension_change_failed", "extensionId": change.extension_id,
                "reason": "cancelled" if cancelled else data.get("reason") or data.get("kind") or "restart_failed",
                "recovered": recovered,
            }
            if data.get("kind") == "managed_extension_not_applied":
                details["failedExtensionId"] = data["extensionId"]
            raise ProtocolError(
                "Extension change was cancelled" if cancelled else "Could not apply the managed extension override",
                -32003 if cancelled else -32004, details,
            ) from failure

    async def recover_after_compaction(self, *, for_prompt=False):
        if not self.compaction_recovery_needed:
            return
        try:
            await self.restart(preserve_history=True, preserve_cancellation=for_prompt)
        finally:
            self.server.sessions[self.id] = self
        self.compaction_recovery_needed = False

    async def prompt(self, blocks, request_key=None, fork_anchor=None):
        key = request_key or uuid.uuid4().hex
        if not isinstance(key, str) or not key or len(key) > 256:
            raise ProtocolError("Request id must be a non-empty string of at most 256 characters")
        fingerprint = hashlib.sha256(json.dumps(blocks, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
        # Replay receipt results without changing the worker's cancellation state.
        prior = recorded_turn(self.server.db, self.id, key, fingerprint)
        if prior is not None:
            return prior
        # Establish the turn before its first suspension. A later wire Stop
        # addresses this generation throughout recovery and receipt admission.
        self.reset_cancellation()
        if self.compaction_recovery_needed:
            await self.recover_after_compaction(for_prompt=True)
        prior = await write_store(self.server.args.state_dir, begin_turn, self.id, key, fingerprint,
                                  diagnostic="turn_receipt_write_failed")
        if prior is not None:
            return prior
        result, error = None, None
        self.dispatched = False
        self.turn_active = True
        try:
            result = await self._prompt(blocks)
            if fork_anchor is not None and result["stopReason"] == "end_turn":
                await self.record_fork_head(fork_anchor)
            return result
        except Exception as failure:
            # Once native may have started work, a failure is final: an
            # automatic replay could repeat model calls and tool effects.
            error = after_dispatch(failure) if self.dispatched else failure
            update = {"sessionUpdate": "agent_message_chunk",
                      "content": {"type": "text", "text": "Request failed: " + str(error)}}
            self.live_history.append(self.adapter_message(update) if transcript.supported(self.controls.capabilities) else update)
            if error is failure:
                raise
            raise error from None
        finally:
            self.turn_active = False
            # Bookkeeping must not replace the turn's own outcome (a marked
            # -33002 would otherwise become a replayable internal error).
            await self.live_history.flush()
            try:
                await write_store(self.server.args.state_dir, finish_turn, self.id, key, result, error,
                                  diagnostic="turn_receipt_write_failed")
            except Exception:
                write_diagnostic("turn_receipt_write_failed")

    async def record_fork_head(self, anchor):
        def capture(filename):
            with open(filename, "rb") as stream:
                return hashlib.file_digest(stream, "sha256").hexdigest()

        # Query again for command turns, which can return without the model
        # path's idle-state refresh. A failed capture cannot authorize a fork.
        self.fork_head = None
        try:
            self.state = await self.rpc("get_state")
            filename = self.state.get("sessionFile")
            if filename and not self.state.get("isStreaming", self.state.get("is_streaming", False)):
                digest = await asyncio.to_thread(capture, filename)
                self.fork_head = {"id": anchor, "sha256": digest}
            await self.save()
        except (OSError, ProtocolError) as error:
            self.fork_head = None
            if isinstance(error, ProtocolError) and (error.data or {}).get("kind") == "session_store_write_failed":
                write_diagnostic("session_store_write_failed")

    async def _prompt(self, blocks):
        if self.done and not self.done.done():
            raise ProtocolError("A prompt is already running in this session")
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
        text_only = all(block.get("type") == "text" for block in blocks)
        instructions = compact_instructions(text) if blocks[0]["type"] == "text" else None
        if instructions is not None and (
                not text_only or "[[AION_FILES]]" in map(str.strip, instructions.splitlines())):
            raise ProtocolError("/compact accepts text instructions only")
        command = text.partition(" ")[0].removeprefix("/") if text_only and text.startswith("/") else ""
        if instructions is not None or not (text_only and text.startswith("/")):
            # Model input (and /compact) without a model is refused before the
            # input is recorded; slash commands run, as /model can select one.
            await self.controls.require_model()
        adapter_input = instructions is not None or ALIASES.get(command, command) in BUILTINS
        tracked_input = transcript.supported(self.controls.capabilities) and (adapter_input or bool(command))
        first_input = self.adapter_event_sequence + 1
        for block in blocks:
            update = {"sessionUpdate": "user_message_chunk", "content": block}
            if tracked_input:
                update = self.adapter_message(update, source="adapter" if adapter_input else "composer")
            while not self.live_history.append(update, retry_if_full=True):
                await self.live_history.flush()
        last_input = self.adapter_event_sequence
        await self.live_history.flush()
        if code := self.live_history.failure:
            raise ProtocolError(code, -32603, {"kind": code})
        if self.cancelled:
            return {"stopReason": "cancelled"}
        if instructions is not None:
            await self.compact(instructions.removeprefix(" "))
            return {"stopReason": "end_turn"}
        if text.startswith("/") and text_only:
            try:
                handled, prepared = await execute_slash(self, text)
            except ProtocolError:
                if self.cancelled:
                    return {"stopReason": "cancelled"}
                raise
            if handled and prepared is None:
                if tracked_input and not adapter_input:
                    try:
                        await write_store(self.server.args.state_dir, confirm_adapter_input, self.id,
                                          self.adapter_event_binding, first_input, last_input,
                                          diagnostic="history_write_failed")
                    except ProtocolError as error:
                        write_diagnostic(error.data["kind"])
                return {"stopReason": "end_turn"}
            if prepared is not None:
                text = prepared
        if self.cancelled:
            return {"stopReason": "cancelled"}
        await self.controls.require_model()  # A template or skill expansion is model input too.
        return await self.drive_turn(
            lambda: self.rpc("prompt", message=text, **({"images": images} if images else {})),
            input_relay=self.input_relay,
        )

    async def notice_turn(self):
        """`_aelix/manage children/notices` (aelix-ai #612): native's notice turn, run as a prompt turn.

        Called under mutation_lock. Native runs the whole turn before it answers
        `{started, placed, reason, remaining}`; the turn timeout bounds it and
        `session/cancel` cancels it (native's commands/cancel, then abort): the
        answer is then `{stopReason: "cancelled"}` with no native fields, since
        native answered the cancelled request with an error. A notice it placed
        stays placed.
        """
        if self.done and not self.done.done():
            raise ProtocolError("A prompt is already running in this session")
        # A cancel of an earlier turn must not deny this turn's tools (the bridge reads it).
        self.reset_cancellation()
        answer, sent = {}, []

        async def send():
            sent.append(True)
            try:
                answer.update(await self.rpc("aelix_control", method="children/notices", params={}, _timeout=None))
            except BackendError:
                if not self.cancelled:
                    raise

        try:
            stop = await self.drive_turn(send)
        except Exception as failure:
            if not sent:
                raise
            raise after_dispatch(failure) from None
        finally:
            await self.live_history.flush()
        return {**answer, **stop}

    async def drive_turn(self, send, *, input_relay=None):
        """Run the native turn `send` starts until native is idle, under the turn timeout."""
        self.done = asyncio.get_running_loop().create_future()
        self.failure = None
        self.tools.clear()
        self.pending_tool_results.clear()
        self.subagents.clear()  # Per turn; self.children (spawned children's parents) stays.
        try:
            if self.private_host and self.server.image_policies:
                self.image_epoch = self.image_turn_id = uuid.uuid4().hex
                (Path(self.socket_dir) / "image-epoch").write_text(self.image_epoch, encoding="ascii")
            timeout = getattr(self.server.args, "turn_timeout", 600)
            async with asyncio.timeout(timeout or None):
                await send()
                if self.cancelled:
                    await self.cancel(again=True)
                await self.wait_until_idle(input_relay=input_relay)
            await self.context.finish_compaction("failed")
            await self.publish_context_usage()
            if self.failure and not self.cancelled:
                raise ProtocolError(self.failure, -32603)
            stop_reason = "cancelled" if self.cancelled else "end_turn"
            await self.save()
            return {"stopReason": stop_reason}
        except TimeoutError:
            await self.cancel()
            raise ProtocolError("Aelix turn timed out", -32603)
        finally:
            if input_relay is not None:
                input_relay.observe_prompt(False)
            if self.done and not self.done.done():
                self.done.cancel()
            await self.context.finish_compaction("failed")

    async def wait_until_idle(self, *, input_relay=None):
        # agent_end closes one provider attempt, not the enclosing prompt's
        # retries, overflow recovery or threshold compaction (#334 in native).
        while True:
            self.state = await self.rpc("get_state")
            streaming = self.state.get("isStreaming", self.state.get("is_streaming", False))
            if input_relay is not None:
                input_relay.observe_prompt(streaming)
            if not streaming:
                return
            if self.cancelled and self.abort_due():
                # Native acceptance precedes before_agent_start/build_context.
                # An earlier abort can precede the turn task's creation. Native
                # gives an abort REABORT_SECONDS to end a turn before its own
                # next one (aelix-ai #800 K5: every poll was one more abort).
                await self.abort()
            await asyncio.sleep(0.05)

    def reset_cancellation(self):
        self.cancelled = False
        self.cancel_generation += 1

    def abort_due(self):
        return self.last_abort is None or time.monotonic() - self.last_abort >= REABORT_SECONDS

    async def abort(self):
        self.aborts += 1
        self.last_abort = time.monotonic()
        with contextlib.suppress(ProtocolError, TimeoutError):
            await self.rpc("abort", _timeout=5)

    def begin_cancel(self):
        """Apply Stop to the admitted turn and its dialogs before any I/O."""
        # Every open child question ends, during a manual /compact too (fix round 1, P-LOW-3).
        # An extension's dialog opened during /compact stays open: the compaction goes on.
        self.void_questions(lambda key: True)
        if self.context.manual_compaction:
            # Classic RPC abort only interrupts native turns and retries.
            # Keep waiting for the actual manual compaction outcome.
            return False
        self.cancelled = True
        self.invalidate_image_acceptance()
        self.image_epoch = None
        self.tool_arrived.set()
        if self.management_task and not self.management_task.done() and not self.extension_commit_started:
            self.management_task.cancel()
        for task in [*self.permission_connections, *self.permissions, *self.ui_tasks]:
            task.cancel()
        return True

    async def cancel(self, *, again=False):
        if self.begin_cancel():
            await self.cancel_native(again=again)

    async def cancel_native(self, *, again=False):
        generation = self.cancel_generation
        if again and not self.abort_due():
            return
        aborts = self.aborts
        if "commands/cancel" in self.controls.capabilities.get("methods", []) and self.proc and self.proc.returncode is None:
            with contextlib.suppress(ProtocolError, TimeoutError):
                # Native output may be held behind an ACP notification. Send
                # both controls in order without requiring the first reply.
                await self.rpc("aelix_control", method="commands/cancel", params={}, _await_response=False)
        # The turn's poll may have aborted while the native stdin buffer drained.
        # That was this cancel's one abort.
        if (self.proc and self.proc.returncode is None and self.aborts == aborts
                and self.cancel_generation == generation):
            await self.abort()

    async def close(self):
        if self._close_task is None:
            self.invalidate_image_acceptance()
            self._close_task = asyncio.create_task(self._close())
            self.server.cleanup_tasks.add(self._close_task)
            self._close_task.add_done_callback(self.server.cleanup_tasks.discard)
        await asyncio.shield(self._close_task)

    async def _close(self):
        self.raise_fence()
        self.studio_resources.retire_approval()
        self.studio_resources.invalidate_pages()
        self.ending = True
        try:
            await self._end_worker()
        finally:
            self.ending = False
            self.changing -= 1

    async def _end_worker(self):
        self.permission_token = None
        await self.settle_exit()
        for task in list(self.permission_connections):
            task.cancel()
        await asyncio.gather(*self.permission_connections, return_exceptions=True)
        for task in list(self.permissions):
            task.cancel()
        # The reader keeps routing and recording native's frames through its shutdown
        # (fix round 1, Codex C-M3): its last tool_end and terminal state close the cards.
        reader = self.reader if self.reader in self.tasks and not self.reader.done() else None
        owned_tasks = [task for task in self.tasks if task is not asyncio.current_task() and task is not reader]
        for task in owned_tasks:
            task.cancel()
        await asyncio.gather(*owned_tasks, return_exceptions=True)
        if self.proc and self.proc.returncode is None:
            async def discard(stream):
                while await stream.read(65536):
                    pass
            drains = [asyncio.create_task(discard(stream)) for stream in
                      ((self.proc.stderr,) if reader else (self.proc.stdout, self.proc.stderr))]
            try:
                if self.worker_job:
                    # Kill-on-close ends the worker and every descendant in its job.
                    self.worker_job.close()
                    self.worker_job = None
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self.proc.wait(), 5)
                    if self.proc.returncode is None:
                        self.proc.kill()
                        await self.proc.wait()
                else:
                    methods = self.controls.capabilities.get("methods", [])
                    serves_children = "children/list" in methods or "children/notices" in methods
                    await terminate_owned(self.proc, grace=CHILDREN_CLOSE_SECONDS if serves_children else 5)
            finally:
                for task in drains:
                    task.cancel()
                await asyncio.gather(*drains, return_exceptions=True)
        if reader is not None:
            # The worker has exited: its stdout ends once its last lines are read.
            await asyncio.wait({reader}, timeout=READ_TAIL_SECONDS)
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        try:
            await self.end_children()
        except Exception as failure:  # A card left open must not keep the worker's resources.
            write_diagnostic(f"[aelix-acp] Could not close the children's cards: {type(failure).__name__}")
        await self.live_history.flush()
        if self.worker_job:
            self.worker_job.close()
            self.worker_job = None
        if self.socket_server:
            self.socket_server.close()
            await self.socket_server.wait_closed()
            self.socket_server = None
        # Publication owns the lease and worker files until the save and registration settle.
        async with self.publication_lock:
            if self.fork_peer_lease:
                self.fork_peer_lease.close()
                self.fork_peer_lease = None
            if self.lease:
                self.lease.close()
                self.lease = None
            if self.socket_dir:
                shutil.rmtree(self.socket_dir, ignore_errors=True)
                self.socket_dir = None
            if self.server.sessions.get(self.id) is self:
                self.server.sessions.pop(self.id, None)
