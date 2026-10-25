"""TEST ONLY: sessionless native API spike, never a production entrypoint.

Run with the public native wheel interpreter and an isolated temporary HOME.
Fixture OAuth exercises native callbacks, not a real provider authorization.
No request payloads, exception text, keys or callback values are logged.
"""
import asyncio
import dataclasses
import json
import os
from pathlib import Path
import sys

from aelix_ai.oauth.auth_storage import AuthStorage, default_auth_path
from aelix_ai.oauth._registry import register_oauth_provider
from aelix_ai.oauth.types import OAuthAuthInfo, OAuthCredentials, OAuthLoginCallbacks, OAuthPrompt
from aelix_coding_agent.cli.config import get_agent_dir
from aelix_coding_agent.cli.extension_install import list_installed_extensions
from aelix_coding_agent.agents import resolve_profile


class FixtureOAuth:
    id, name, uses_callback_server = "probe-oauth", "Synthetic callback fixture", False

    async def login(self, callbacks):
        await callbacks.on_auth(OAuthAuthInfo(url="https://fixture.invalid/oauth?state=synthetic-secret"))
        await callbacks.on_prompt(OAuthPrompt(message="Synthetic authorization code"))
        return OAuthCredentials(refresh="synthetic-refresh", access="synthetic-access", expires=9999999999999)


async def main():
    if not __debug__:
        raise RuntimeError("The test sandbox checks require Python without -O")
    # This executable is deliberately restricted to a caller-created sandbox.
    sandbox = Path(sys.argv[1]).resolve()
    assert Path(os.environ["HOME"]).resolve() == sandbox / "home"
    agent = Path(get_agent_dir()).resolve()
    assert agent.is_relative_to(sandbox) and Path.cwd().resolve().is_relative_to(sandbox)
    storage = AuthStorage(agent / "auth.json")  # SAME explicit path as public CLI.
    register_oauth_provider(FixtureOAuth())
    tasks, replies = {}, {}
    output = asyncio.Lock()

    async def send(frame):
        async with output:
            print(json.dumps(frame), flush=True)

    async def execute(frame):
        key, method, args = frame["id"], frame["method"], frame.get("params", {})
        try:
            if method == "probe/roots":
                result = {"cli": str(agent / "auth.json"), "implicit": str(default_auth_path())}
            elif method == "auth/status":
                await storage.load()  # Explicit invalidation, not automatic cache coherence.
                result = dataclasses.asdict(await storage.get_auth_status(args["provider"]))
            elif method == "probe/held_status":
                result = dataclasses.asdict(await storage.get_auth_status(args["provider"]))
            elif method == "auth/set_api_key":
                await storage.set_api_key(args["provider"], args["key"])
                result = {"appliesAt": "new_or_reloaded_storage", "liveWorkerUpdated": False}
            elif method == "auth/logout":
                await storage.logout(args["provider"])
                result = {"removedFromNativeStore": True, "liveWorkerUpdated": False}
            elif method == "probe/login":
                # Only the registered synthetic provider can be invoked by this spike.
                async def on_auth(info):
                    await send({"jsonrpc": "2.0", "method": "probe/transient", "params": {"kind": "oauth", "url": info.url}})

                async def on_prompt(prompt):
                    future = asyncio.get_running_loop().create_future()
                    replies[key] = future
                    await send({"jsonrpc": "2.0", "method": "probe/transient", "params": {"kind": "input", "requestId": key}})
                    return await future

                await storage.login("probe-oauth", OAuthLoginCallbacks(on_auth=on_auth, on_prompt=on_prompt))
                result = {"configured": True}
            elif method == "profiles/resolve":
                profile = resolve_profile(args["name"], cwd=str(Path.cwd()), project_trusted=False, agent_dir=str(agent))
                result = {"name": profile.name, "scope": profile.scope, "tools": profile.tools}
            elif method == "packages/list":
                result = {"installedCount": len(list_installed_extensions()), "liveLoaded": "unknown"}
            else:
                await send({"jsonrpc": "2.0", "id": key, "error": {"code": -32601, "message": "unsupported"}})
                return
            await send({"jsonrpc": "2.0", "id": key, "result": result})
        except asyncio.CancelledError:
            await send({"jsonrpc": "2.0", "id": key, "error": {"code": -32800, "message": "cancelled"}})
        except Exception:
            await send({"jsonrpc": "2.0", "id": key, "error": {"code": -32603, "message": "native operation failed"}})
        finally:
            replies.pop(key, None)
            tasks.pop(key, None)

    reader = asyncio.StreamReader(limit=65536)
    await asyncio.get_running_loop().connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
    try:
        while line := await reader.readline():
            frame = json.loads(line)
            if frame["method"] == "probe/cancel":
                if task := tasks.get(frame["params"]["requestId"]):
                    task.cancel()
            elif frame["method"] == "probe/reply":
                future = replies.get(frame["params"]["requestId"])
                if future and not future.done():
                    future.set_result(frame["params"]["value"])
            else:
                tasks[frame["id"]] = asyncio.create_task(execute(frame))
    finally:
        pending = list(tasks.values())
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
