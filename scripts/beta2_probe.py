"""Test-only public extension API probe. Explicitly loaded by the qualifier."""
import asyncio
import dataclasses
import json
import os
from pathlib import Path


def setup(api):
    listener = None

    async def start(event, ctx):
        nonlocal listener

        async def handle(reader, writer):
            try:
                async with asyncio.timeout(10):
                    request = json.loads(await reader.readline())
                    if request.get("token") != os.environ["BETA2_PROBE_TOKEN"] or not ctx.is_idle():
                        raise ValueError("Probe requires an authenticated idle session")
                    if request["action"] == "switch":
                        model = next(m for m in ctx.model_registry.get_available() if m.id == "two")
                        result = {"accepted": await api.set_model(model)}
                    elif request["action"] == "snapshot":
                        usage = ctx.get_context_usage()
                        result = {"models": [m.id for m in ctx.model_registry.get_available()],
                                  "context": dataclasses.asdict(usage) if usage else None}
                    else:
                        raise ValueError("Unknown probe action")
                    writer.write((json.dumps({"result": result}) + "\n").encode())
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        listener = await asyncio.start_server(handle, "127.0.0.1", 0, limit=65536)
        Path(os.environ["BETA2_PROBE_READY"]).write_text(json.dumps({"port": listener.sockets[0].getsockname()[1]}))

    async def stop(event, ctx):
        if listener:
            listener.close()
            await listener.wait_closed()

    api.on("session_start", start)
    api.on("session_shutdown", stop)
