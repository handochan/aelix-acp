"""Beta.2 controls, loaded by file inside the native interpreter.

Only public extension/profile APIs are used. This module must not import ACP:
the native executable may live in an entirely separate Python environment.
"""
import asyncio
import contextlib
import dataclasses
import hashlib
import hmac
import json
import os
from pathlib import Path
import re

LIMIT = 1024 * 1024


class Rejected(Exception):
    pass


def install(api):
    endpoint_file = os.environ.get("AELIX_ACP_CONTROLS_FILE")
    if not endpoint_file:
        return
    token = json.loads(os.environ["AELIX_ACP_ENDPOINT"])["token"]
    nonce = os.environ["AELIX_ACP_NONCE"]
    listener = None
    handlers = set()
    lock = asyncio.Lock()

    async def start(event, ctx):
        nonlocal listener
        ceiling = set(api.get_active_tools())
        from aelix_agent_core.harness.skills import load_skills
        from aelix_agent_core.harness.prompt_templates import load_prompt_templates
        selected = json.loads(os.environ.get("AELIX_ACP_SELECTED_RESOURCES", "{}"))
        skills = load_skills(selected.get("skills", []))
        templates = load_prompt_templates(selected.get("promptTemplates", []))

        def commands():
            return [{"name": "skill:" + s.name, "description": s.description} for s in skills.skills] + [
                {"name": t.name, "description": t.description} for t in templates.templates]

        def tools():
            return {"tools": [{"name": tool.name} for tool in api.get_all_tools() if tool.name in ceiling],
                    "activeTools": api.get_active_tools()}

        async def perform(method, params):
            if method == "commands/list":
                return {"commands": commands(), "scope": "explicit adapter selections"}
            if method == "commands/expand":
                from aelix_coding_agent.cli.resource_commands import expand_resource_command
                text = params.get("text")
                if not isinstance(text, str) or len(text.encode("utf-8")) > 262144:
                    raise Rejected("Resource command must be text under 256 KiB")
                prompt = expand_resource_command(text, skills=skills.skills, templates=templates.templates)
                return {"outcome": "prompt", "prompt": prompt} if prompt is not None else {"outcome": "not_a_command"}
            if method == "models/list":
                from aelix_ai.models import get_supported_thinking_levels
                models = []
                for model in ctx.model_registry.get_available():
                    item = {key: getattr(model, attr) for key, attr in (
                        ("id", "id"), ("provider", "provider"), ("name", "name"),
                        ("reasoning", "reasoning"), ("input", "input"),
                        ("contextWindow", "context_window"), ("maxTokens", "max_tokens"))
                        if hasattr(model, attr)}
                    item["supportedThinkingLevels"] = list(get_supported_thinking_levels(model))
                    models.append(item)
                return {"models": models}
            if method == "models/set":
                selected = params.get("model")
                model = next((m for m in ctx.model_registry.get_available()
                              if m.provider + "/" + m.id == selected), None)
                if model is None:
                    raise Rejected("Model is unavailable in the native authenticated registry")
                if not await api.set_model(model):
                    raise Rejected("Native model selection was rejected")
                return {"model": selected}
            if method == "tools/list":
                return {**tools(), "commands": commands(), "skillDiagnostics": [dataclasses.asdict(d) for d in skills.diagnostics],
                        "promptDiagnostics": [dataclasses.asdict(d) for d in templates.diagnostics]}
            if method == "tools/set_active":
                names = params.get("names")
                if (not isinstance(names, list) or len(names) > 512 or
                        any(not isinstance(n, str) or n not in ceiling for n in names)):
                    raise Rejected("Tools must be selected from the session's launch grant")
                api.set_active_tools(list(dict.fromkeys(names)))
                return tools()
            if method.startswith("agents/"):
                from aelix_coding_agent.agents import discover_profiles, parse_profile, resolve_profile, profile_to_flags, user_agents_dir
                trusted = os.environ.get("AELIX_ACP_PROJECT_TRUSTED") == "true"
                if method == "agents/list":
                    found = discover_profiles(ctx.cwd, project_trusted=trusted)
                    return {"profiles": [dataclasses.asdict(p) for p in found.profiles],
                            "diagnostics": [dataclasses.asdict(d) for d in found.diagnostics]}
                if method == "agents/save":
                    name, content = params.get("name"), params.get("content")
                    if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", name):
                        raise Rejected("Profile name must be a simple name of at most 64 characters")
                    if not isinstance(content, str) or len(content.encode("utf-8")) > 262144:
                        raise Rejected("Profile content must be native Markdown under 256 KiB")
                    path = user_agents_dir() / (name + ".md")
                    parsed = parse_profile(content, file_path=str(path), scope="user")
                    if parsed.profile is None or parsed.profile.name != name or any(d.type == "error" for d in parsed.diagnostics):
                        raise Rejected("Invalid native profile or mismatched name")
                    path.parent.mkdir(parents=True, exist_ok=True)
                    # Creation only. Editing/replacing an existing native profile
                    # is deliberately left to its owner; no symlink overwrites.
                    with path.open("x", encoding="utf-8") as stream:
                        stream.write(content)
                    return {"profile": dataclasses.asdict(parsed.profile)}
                name = params.get("name")
                if not isinstance(name, str) or not name or len(name) > 4096:
                    raise Rejected("A native profile name or absolute file path is required")
                profile = resolve_profile(name, cwd=ctx.cwd, project_trusted=trusted, is_file=Path(name).is_absolute())
                path = Path(profile.file_path)
                raw = path.read_bytes()
                content = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
                checked = parse_profile(content, file_path=str(path), scope=profile.scope)
                if checked.profile != profile:
                    raise Rejected("Profile changed while resolving it; retry selection")
                return {"profile": dataclasses.asdict(profile), "launch": {
                    "name": profile.name, "path": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
                    "flags": profile_to_flags(profile, prompt_path=str(path))}}
            raise Rejected("Unsupported beta.2 extension control")

        async def handle(reader, writer):
            task = asyncio.current_task()
            handlers.add(task)
            operation = disconnect = None
            request_id = None
            try:
                async with asyncio.timeout(30):
                    if len(handlers) > 8:
                        raise Rejected("Too many control connections")
                    message = json.loads(await reader.readline())
                    if not isinstance(message, dict):
                        raise Rejected("Control request must be an object")
                    request_id = message.get("id")
                    auth = message.get("token")
                    if not isinstance(auth, str) or not hmac.compare_digest(auth, token):
                        raise Rejected("Invalid control authentication")
                    if not isinstance(request_id, str) or len(request_id) > 64 or not isinstance(message.get("params"), dict):
                        raise Rejected("Invalid control request")
                    async with lock:
                        if not ctx.is_idle():
                            raise Rejected("Native session is busy")
                        operation = asyncio.create_task(perform(message.get("method", ""), message["params"]))
                        disconnect = asyncio.create_task(reader.read(1))
                        completed, _ = await asyncio.wait([operation, disconnect], return_when=asyncio.FIRST_COMPLETED)
                        if operation not in completed:
                            return
                        result = await operation
                    answer = {"id": request_id, "result": result}
            except Rejected as exc:
                answer = {"id": request_id, "error": str(exc), "code": -32602}
            except TimeoutError:
                answer = {"id": request_id, "error": "Native extension control timed out", "code": -32007}
            except Exception:
                # Provider/plugin exceptions can contain headers or credentials.
                answer = {"id": request_id, "error": "Native extension control failed; refresh session state", "code": -32603}
            else:
                pass
            finally:
                for pending in (operation, disconnect):
                    if pending is not None and not pending.done():
                        pending.cancel()
                await asyncio.gather(*(t for t in (operation, disconnect) if t is not None), return_exceptions=True)
                if "answer" in locals():
                    with contextlib.suppress(ConnectionError, ValueError, TimeoutError):
                        data = (json.dumps(answer) + "\n").encode()
                        if len(data) > LIMIT:
                            data = (json.dumps({"id": request_id, "error": "Extension control result exceeds 1 MiB", "code": -32603}) + "\n").encode()
                        async with asyncio.timeout(2):
                            writer.write(data)
                            await writer.drain()
                writer.close()
                try:
                    async with asyncio.timeout(2):
                        await writer.wait_closed()
                except (ConnectionError, TimeoutError):
                    writer.transport.abort()
                handlers.discard(task)

        listener = await asyncio.start_server(handle, "127.0.0.1", 0, limit=LIMIT)
        path = Path(endpoint_file)
        path.write_text(json.dumps({"version": 1, "host": "127.0.0.1", "port": listener.sockets[0].getsockname()[1], "nonce": nonce}))
        os.chmod(path, 0o600)

    async def stop(event, ctx):
        if listener is not None:
            listener.close()
            await listener.wait_closed()
        tasks = list(handlers)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        Path(endpoint_file).unlink(missing_ok=True)

    api.on("session_start", start)
    api.on("session_shutdown", stop)
