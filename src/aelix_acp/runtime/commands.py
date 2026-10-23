"""Explicit slash-command dispatch. Control commands never become model input."""
from __future__ import annotations

import json
import shlex

from ..wire.errors import ProtocolError

BUILTINS = {
    "help": "List commands and runtime support",
    "model": "List or select provider/model",
    "thinking": "Show or select the reasoning level",
    "permissions": "Show or select the permission mode",
    "mode": "Show or select steering mode",
    "follow-up": "Show or select follow-up mode",
    "settings": "Inspect or change Aelix settings",
    "compact": "Compact conversation context",
    "cost": "Show session usage and cost",
    "stats": "Show session statistics",
    "session": "Show session identity and state",
    "context": "Show context and model limits",
    "name": "Show or rename the session",
    "tools": "Inspect or select active tools",
    "skills": "List available skills",
    "hooks": "Inspect loaded extension hooks",
    "mcp": "Inspect connected MCP servers",
    "extension": "Manage installed and loaded extensions",
    "reload": "Reload Aelix resources",
    "export": "Export the transcript as HTML",
    "copy": "Return the last assistant message",
    "new": "Start a new Aelix session",
    "resume": "Resume a session by ACP id",
    "fork": "Fork at a conversation entry",
    "clone": "Clone the conversation",
    "tree": "Show session fork points",
    "import": "Import an Aelix session",
    "clear": "Clear the client conversation view",
    "login": "Configure provider authentication",
    "logout": "Remove provider authentication",
    "trust": "Manage workspace trust",
    "agents": "Manage or run agent profiles",
    "scoped-models": "Manage enabled model patterns",
    "statusline": "Configure client status presentation",
    "hotkeys": "Show the client's keyboard shortcuts",
    "expand": "Expand a tool result",
    "quit": "Close this session's Aelix runtime",
}
ALIASES = {"rename": "name", "exit": "quit"}
CLIENT_ACTIONS = {"clear", "login", "logout", "trust", "agents", "statusline", "hotkeys", "expand", "import"}


def split_command(text: str) -> tuple[str, str] | None:
    # Leading whitespace and ordinary prose do not turn into control messages.
    if not text.startswith("/") or text.startswith("//"):
        return None
    head, separator, tail = text[1:].partition(" ")
    if not head or any(c.isspace() for c in head):
        raise ProtocolError("Slash commands need a non-empty name on the first line")
    return ALIASES.get(head, head), tail if separator else ""


def json_argument(raw: str):
    try:
        return json.loads(raw)
    except ValueError:
        return raw


async def execute(session, text: str) -> tuple[bool, str | None]:
    """Return (handled, prepared_model_prompt). A known control never falls through."""
    parsed = split_command(text)
    if not parsed:
        return False, None
    name, raw = parsed
    args = raw.strip()

    async def output(value):
        rendered = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
        await session.update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": rendered}})

    async def control(method, params=None):
        if method not in session.controls.capabilities.get("methods", []):
            raise ProtocolError(f"/{name} needs Aelix runtime control v1 ({method}); update the Aelix runtime", -32004)
        timeout = getattr(session.server.args, "command_timeout", 600) or None
        return await session.rpc("aelix_control", method=method, params=params or {}, _timeout=timeout)

    if name == "help":
        await output("\n".join(f"/{command} — {description}" for command, description in BUILTINS.items()))
    elif name in {"model", "thinking", "permissions", "mode", "follow-up"}:
        option_id = {"model": "model", "thinking": "thinking_level", "permissions": "permission_mode",
                     "mode": "steeringMode", "follow-up": "followUpMode"}[name]
        if args:
            await session.controls.set(option_id, args)
        await output(next((option for option in session.controls.options() if option["id"] == option_id), {}))
    elif name in {"cost", "stats", "context"}:
        await output(await session.rpc("get_session_stats"))
    elif name == "session":
        from .controls import sanitize_model
        state = dict(await session.rpc("get_state"))
        state["model"] = sanitize_model(state.get("model") or {})
        await output(state)
    elif name == "name":
        if args:
            await session.rpc("set_session_name", name=args)
        session.state = await session.rpc("get_state")
        session.save()
        await output(session.state.get("sessionName") or "Unnamed session")
    elif name == "compact":
        await output(await session.rpc("compact", _timeout=getattr(session.server.args, "command_timeout", 600) or None,
                                      **({"customInstructions": raw} if args else {})))
    elif name == "export":
        await output(await session.rpc("export_html", **({"outputPath": args} if args else {})))
    elif name == "copy":
        await output(await session.rpc("get_last_assistant_text"))
    elif name == "new":
        await output(await session.server.client_action(session, "new_session", {"cwd": session.cwd}))
    elif name == "tree":
        await output(await session.rpc("get_fork_messages"))
    elif name in {"fork", "clone"}:
        entry = args
        if name == "fork" and not entry:
            points = (await session.rpc("get_fork_messages")).get("messages", [])
            if not points:
                raise ProtocolError("No conversation entry is available to fork")
            entry = points[-1]["entryId"]
        created = await session.server.branch_session(session, {"kind": name, **({"entryId": entry} if name == "fork" else {})})
        metadata = getattr(session.server, "client_capabilities", {}).get("_meta", {})
        if metadata.get("aelix/clientActions") is True and created.get("sessionId"):
            await session.server.client_action(session, "open_session", created)
        await output(created)
    elif name == "resume":
        if not args:
            rows = session.server.db.execute("SELECT id,title FROM sessions WHERE cwd=? AND profile=? ORDER BY updated DESC", (session.cwd, session.server.args.profile))
            await output({"sessions": [{"sessionId": row[0], "title": row[1]} for row in rows]})
        else:
            row = session.server.db.execute("SELECT id FROM sessions WHERE id=? AND cwd=? AND profile=?", (args, session.cwd, session.server.args.profile)).fetchone()
            if not row:
                raise ProtocolError("Unknown ACP session for this workspace and profile")
            await output(await session.server.client_action(session, "open_session", {"sessionId": args}))
    elif name == "reload":
        await output(await control("resources/reload"))
        await session.controls.refresh()
        await session.restore_tools()
        await session.commands()
    elif name in {"tools", "skills", "hooks", "mcp"}:
        if name == "tools" and args:
            result = await control("tools/set_active", {"names": shlex.split(args)})
            session.active_tools = result["activeTools"]
            await output(result)
        else:
            resources = await control("resources/list")
            await output(resources.get(name, []))
    elif name == "settings":
        if not args:
            await output(await control("settings/get"))
        else:
            key, separator, value = args.partition(" ")
            if not separator:
                raise ProtocolError("Use /settings KEY JSON_VALUE, or /settings to list supported settings")
            await output(await control("settings/set", {"key": key, "value": json_argument(value)}))
    elif name == "scoped-models":
        if args:
            await output(await control("settings/set", {"key": "enabled_models", "value": shlex.split(args)}))
        else:
            await output((await control("settings/get")).get("settings", {}).get("enabled_models", []))
    elif name in {"login", "logout"} and args and " " not in args:
        await output(await control("auth/login" if name == "login" else "auth/logout", {"provider": args}))
        await session.controls.refresh()
    elif name == "agents":
        operation, _, remainder = args.partition(" ")
        operation = operation or "list"
        if operation == "list":
            await output(await control("agents/list"))
        elif operation in {"show", "use"}:
            await output(await control("agents/" + operation, {"name": remainder.strip()}))
            if operation == "use":
                await session.controls.refresh()
                session.save()
                await session.commands()
        elif operation == "run":
            profile, _, task = remainder.partition(" ")
            await output(await control("agents/run", {"name": profile, "task": task}))
        else:
            raise ProtocolError("Use /agents list|show NAME|use NAME|run NAME TASK")
    elif name == "trust" and args in {"on", "off", "true", "false"}:
        trusted = args in {"on", "true"}
        await require_trust(session, trusted)
        await output({"trusted": trusted})
    elif name == "extension":
        await output(await session.server.extension_action(session, args))
    elif name == "quit":
        await output("Aelix runtime closed. Use session/load to reopen this conversation.")
        await session.close()
    elif name in CLIENT_ACTIONS:
        await output(await session.server.client_action(session, name, raw))
    else:
        # Extension commands, prompt templates and skills share Aelix's own
        # dispatch/expansion implementation. Never ask an LLM to emulate them.
        result = await control("commands/run", {"text": text})
        for message in result.get("messages", []):
            await output(message["text"])
        if result.get("outcome") == "prompt":
            return True, result["prompt"]
        if result.get("outcome") == "not_a_command":
            raise ProtocolError(f"Unknown slash command: /{name}")
        if result.get("outcome") == "error":
            raise ProtocolError(f"/{name} failed in the Aelix command handler", -32603)
    if session.proc and session.proc.returncode is None and not session.rpc_closed:
        session.state = await session.rpc("get_state")
        session.save()
    await session.update({"sessionUpdate": "config_option_update", "configOptions": session.controls.options()})
    return True, None


async def require_trust(session, trusted):
    from .extensions import require_consent
    await require_consent(session.server, session, "Change project trust", {"cwd": session.cwd, "trusted": trusted})
    await session.restart(trust_project=trusted)
