"""Aelix's native extension lifecycle with argv-only execution and client consent."""
from __future__ import annotations

import asyncio
from pathlib import Path
import shlex
import re
import os
import uuid
import subprocess
import sys

from ..wire.errors import ProtocolError
from ..wire.process import terminate_owned

READ_ACTIONS = {"list", "verify", "discover"}
WRITE_ACTIONS = {"install", "remove", "update"}


async def run_extension_action(server, session, raw: str) -> dict:
    task = asyncio.create_task(_run_extension_action(server, session, raw))
    session.management_task = task
    try:
        return await task
    except asyncio.CancelledError:
        if session.cancelled:
            raise ProtocolError("Management operation cancelled", -32003) from None
        raise
    finally:
        session.management_task = None


async def _run_extension_action(server, session, raw) -> dict:
    try:
        if isinstance(raw, dict):
            if set(raw) - {"action", "target"} or not isinstance(raw.get("action"), str):
                raise ProtocolError("Extension action requires action and optional target")
            parts = [raw["action"]]
            if "target" in raw:
                if not isinstance(raw["target"], str):
                    raise ProtocolError("Extension target must be a string")
                parts.append(raw["target"])
        else:
            parts = shlex.split(raw, posix=os.name != "nt") or ["list"]
            if os.name == "nt":
                parts = [part[1:-1] if len(part) >= 2 and part[0] == part[-1] and part[0] in {"'", '"'} else part for part in parts]
    except ValueError as error:
        raise ProtocolError(f"Invalid extension command: {error}") from None
    action, *arguments = parts
    if action in {"enable", "disable", "reload"}:
        if action == "reload":
            if getattr(session, "compat", None) is not None:
                from .preview import manage
                return await manage(session, "resources/reload", {})
            result = await session.rpc("aelix_control", method="resources/reload", params={},
                                       _timeout=getattr(server.args, "command_timeout", 600) or None)
            await session.controls.refresh()
            await session.restore_tools()
            session.save()
            await session.commands()
            return result
        if len(arguments) != 1:
            raise ProtocolError(f"/extension {action} requires one extension path or native module reference")
        reference = extension_reference(arguments[0], session.cwd)
        if action == "disable" and getattr(session, "compat", None) is not None and reference not in session.extra_extensions:
            raise ProtocolError("Only adapter-selected extensions can be disabled here; change the owning launch configuration or native profile")
        if action == "enable" and Path(reference).is_absolute() and not Path(reference).exists():
            raise ProtocolError("Extension source path does not exist")
        await require_consent(server, session, f"{action} extension", {"source": reference})
        selected = list(session.extra_extensions)
        if action == "enable" and reference not in selected:
            if len(selected) >= 64:
                raise ProtocolError("At most 64 explicit extensions may be enabled")
            selected.append(reference)
        if action == "disable":
            selected = [entry for entry in selected if entry != reference]
        if getattr(session, "compat", None) is not None:
            # Do not report the loader's per-extension success: the public
            # beta.2 API exposes no authoritative loader-error inventory.
            from .preview import restart
            await restart(session, extensions=selected)
            return {"enabledExtensions": selected, "restarted": True, "loadState": "not_observed"}
        await session.restart(extensions=selected)
        resources = await session.rpc("aelix_control", method="resources/list", params={})
        errors = resources.get("extensionErrors", [])
        return {"enabledExtensions": selected, "loadErrors": errors, "ready": not errors}
    if action not in READ_ACTIONS | WRITE_ACTIONS or len(arguments) > 1:
        raise ProtocolError("Use /extension list|verify|discover|install|remove|update|enable|disable|reload [target]")
    if arguments and (not arguments[0] or arguments[0].startswith("-") or "\x00" in arguments[0]):
        raise ProtocolError("Extension target must be one explicit package, URL, name or path")
    if action in {"install", "remove"} and not arguments:
        raise ProtocolError(f"/extension {action} requires a target")
    executable = getattr(server.args, "extension_manager", None) or server.args.aelix
    if action in WRITE_ACTIONS:
        if ".app/contents/" in Path(executable).resolve().as_posix().casefold():
            raise ProtocolError("Bundled app resources are read-only. Configure --extension-manager with a writable managed Aelix environment", -32004)
        await require_consent(server, session, f"{action} extension", {"arguments": arguments})
    prefix = getattr(server.args, "aelix_arg", []) if not getattr(server.args, "extension_manager", None) else []
    argv = [executable, *prefix, *(["--offline"] if getattr(server.args, "offline", False) else []), "extension", action, *arguments]
    if action in WRITE_ACTIONS:
        argv.append("--yes")
    process = await asyncio.create_subprocess_exec(*argv, cwd=session.cwd, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        **({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32" else {"start_new_session": True}))
    chunks = []
    size = 0
    try:
        async with asyncio.timeout(180):
            while chunk := await process.stdout.read(65536):
                size += len(chunk)
                if size > 1024 * 1024:
                    raise ProtocolError("Extension manager output exceeded 1 MiB", -32603)
                chunks.append(chunk)
            code = await process.wait()
    except BaseException:
        # Drain output even when cancellation stopped the normal reader.
        async def discard():
            while await process.stdout.read(65536):
                pass
        drain = asyncio.create_task(discard())
        try:
            await terminate_owned(process)
        finally:
            drain.cancel()
            await asyncio.gather(drain, return_exceptions=True)
        raise
    result = {"action": action, "exitCode": code, "text": b"".join(chunks).decode("utf-8", errors="replace"),
              "requiresReload": action in WRITE_ACTIONS and code == 0}
    if code:
        raise ProtocolError("Aelix extension manager failed", -32603, result)
    return result


async def require_consent(server, session, action: str, details: dict) -> None:
    response = await server.client_request("session/request_permission", {
        "sessionId": session.id,
        "toolCall": {"toolCallId": "management-" + uuid.uuid4().hex, "title": action,
                     "kind": "execute", "status": "pending", "rawInput": details},
        "options": [{"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
                    {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"}],
    })
    outcome = response.get("outcome") or {}
    if outcome.get("outcome") != "selected" or outcome.get("optionId") != "allow-once":
        raise ProtocolError("Extension change was not approved", -32003)


def extension_reference(value: str, cwd: str) -> str:
    if not value or "\x00" in value or len(value) > 4096:
        raise ProtocolError("Invalid extension source")
    explicit_path = value.startswith(("/", "./", "../", "~", "\\")) or value.endswith(".py") or Path(value).is_absolute()
    if explicit_path:
        path = Path(value).expanduser()
        path = path if path.is_absolute() else Path(cwd) / path
        return str(path.resolve())
    if re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*(?::[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)?", value):
        if (Path(cwd) / value).exists():
            raise ProtocolError("A project path shadows this module reference; use an explicit absolute path")
        return value
    raise ProtocolError("Extension source must be an explicit path or a native Python module reference")
