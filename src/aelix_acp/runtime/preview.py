"""Adapter-owned beta.2 management; never advertised as native control v1."""
import asyncio
import contextlib
import hashlib
from pathlib import Path

from ..wire.errors import ProtocolError
from .extensions import require_consent

METHODS = {"resources/list", "resources/configure", "resources/reload", "tools/set_active",
           "settings/get", "settings/set", "extensions/list", "extensions/action",
           "agents/list", "agents/show", "agents/save", "agents/use"}


def methods(session):
    return METHODS if getattr(session, "compat", None) is not None else set()


def resource_flags(resources, cwd):
    if not isinstance(resources, dict) or set(resources) - {"inheritSkills", "skills", "promptTemplates"}:
        raise ProtocolError("Resources accept inheritSkills, skills and promptTemplates")
    flags = []
    if "inheritSkills" in resources:
        if type(resources["inheritSkills"]) is not bool:
            raise ProtocolError("inheritSkills must be a boolean")
        if not resources["inheritSkills"]:
            flags.append("--no-skills")
    for key, flag in (("skills", "--skill"), ("promptTemplates", "--prompt-template")):
        values = resources.get(key, [])
        if not isinstance(values, list) or len(values) > 64:
            raise ProtocolError(f"{key} must be a list of at most 64 paths")
        for value in values:
            if not isinstance(value, str) or not value or "\x00" in value:
                raise ProtocolError(f"Invalid {key} path")
            path = Path(value).expanduser()
            path = (Path(cwd) / path if not path.is_absolute() else path).resolve()
            if not path.exists():
                raise ProtocolError(f"Selected {key} path no longer exists")
            flags += [flag, str(path)]
    return flags


def profile_flags(profile):
    if profile is None:
        return []
    if not isinstance(profile, dict) or set(profile) != {"name", "path", "sha256", "flags"}:
        raise ProtocolError("Invalid stored native profile")
    path = Path(profile["path"])
    if (not path.is_absolute() or not path.is_file() or path.stat().st_size > 1024 * 1024 or
            hashlib.sha256(path.read_bytes()).hexdigest() != profile["sha256"]):
        raise ProtocolError("Selected native profile changed or disappeared; select it again before resuming", -32004)
    flags = profile["flags"]
    if not isinstance(flags, list) or len(flags) > 512 or any(not isinstance(flag, str) or "\x00" in flag for flag in flags):
        raise ProtocolError("Invalid stored native profile flags")
    switches = {"--no-tools", "--no-builtin-tools", "--no-skills", "--no-extensions", "--no-context-files"}
    valued = {"--provider", "--model", "--tools", "--skill", "-e", "--thinking", "--system-prompt-file", "--append-system-prompt-file"}
    index = 0
    while index < len(flags):
        flag = flags[index]
        index += 1
        if flag in valued and index < len(flags):
            index += 1
        elif flag not in switches:
            raise ProtocolError("Unsupported stored native profile flag")
    return flags


async def restart(session, **changes):
    # Once child replacement begins, finish replacement/cleanup even if the
    # client cancels. Cancellation is not evidence that startup rolled back.
    operation = asyncio.create_task(session.restart(**changes))

    async def finish():
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            await operation
            raise ProtocolError("Replacement completed after cancellation; inspect current session state", -32003) from None

    task = asyncio.create_task(finish())
    session.management_task = task
    try:
        return await task
    finally:
        session.management_task = None


async def manage(session, action, params):
    session.cancelled = False
    task = asyncio.create_task(_manage(session, action, params))
    session.management_task = task
    try:
        return await task
    except asyncio.CancelledError:
        if session.cancelled:
            if action == "settings/set":
                with contextlib.suppress(ProtocolError):
                    await session.controls.refresh()
                    session.save()
            raise ProtocolError("Management operation cancelled; inspect actual state before retrying", -32003) from None
        raise
    finally:
        if session.management_task is task:
            session.management_task = None


async def _manage(session, action, params):
    if action not in methods(session):
        raise ProtocolError("Management action is unavailable in this runtime profile", -32004,
                            {"kind": "runtime_capability_required", "capability": action})
    if action == "resources/list":
        result = await session.compat.call("tools/list")
        return {**result, "resources": session.preview_resources, "profile": session.preview_profile,
                "extensions": {"operator": getattr(session.server.args, "extension", []), "selected": session.extra_extensions},
                "inventoryScope": "launch-granted tools and adapter selections; not native loader inventory"}
    if action == "tools/set_active":
        result = await session.compat.call(action, params)
        session.active_tools = result["activeTools"]
        session.save()
        return result
    if action == "resources/reload":
        await require_consent(session.server, session, "Restart Aelix resources", {})
        await restart(session)
        return {"restarted": True, "sessionId": session.id}
    if action == "resources/configure":
        resource_flags(params, session.cwd)
        if session.preview_profile and ("skills" in params or "inheritSkills" in params):
            raise ProtocolError("Skill selections are owned by the active native profile; edit/select that profile")
        await require_consent(session.server, session, "Change startup resources and restart", params)
        await restart(session, preview_resources=params)
        return {"restarted": True, "resources": session.preview_resources}
    if action == "settings/get":
        return {"configOptions": session.controls.options(), "scope": "session",
                "resources": session.preview_resources, "profile": session.preview_profile}
    if action == "settings/set":
        value = params.get("value")
        if isinstance(value, bool):
            value = str(value).lower()
        return await session.controls.set(params.get("key"), value)
    if action == "extensions/list":
        return (await _manage(session, "resources/list", {}))["extensions"]
    if action == "extensions/action":
        return await session.server.extension_action(session, params.get("command", params))
    if action in {"agents/list", "agents/show"}:
        return await session.compat.call(action, params)
    if action == "agents/save":
        await require_consent(session.server, session, "Create native Aelix profile", {
            "name": params.get("name"), "content": params.get("content")})
        return await session.compat.call(action, params)
    if action == "agents/use":
        if params.get("name") == "--none":
            await require_consent(session.server, session, "Clear native profile and restart", {})
            await restart(session, preview_profile=None, reset_config=True)
            return {"restarted": True, "profile": None, "sessionId": session.id}
        compiled = await session.compat.call("agents/show", params)
        launch = compiled["launch"]
        profile_flags(launch)
        await require_consent(session.server, session, "Apply native profile and restart", compiled["profile"])
        await restart(session, preview_profile=launch, preview_resources={}, reset_config=True)
        return {"restarted": True, "profile": compiled["profile"], "sessionId": session.id}
    raise ProtocolError("Unsupported beta.2 action")
