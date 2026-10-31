"""Aelix extension: wait for ACP consent BEFORE every tool execution.

Loaded explicitly into the RPC child. The parent owns a private Unix socket or
authenticated Windows loopback channel; absent/broken channels block the tool.
"""
import asyncio
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from aelix_agent_core.harness.hooks import ToolCallResult


async def before_tool(event, ctx):
    path = os.environ.get("AELIX_ACP_SOCKET")
    endpoint = os.environ.get("AELIX_ACP_ENDPOINT")
    token = os.environ.get("AELIX_ACP_PERMISSION_TOKEN")
    if not path and not endpoint:
        return ToolCallResult(block=True, reason="ACP permission channel is absent")
    writer = None
    try:
        async with asyncio.timeout(300):
            request = {"toolCallId": event.tool_call_id, "name": event.tool_name, "args": event.args}
            if endpoint:
                target = urlsplit(endpoint)
                if target.scheme != "tcp" or target.hostname != "127.0.0.1" or not target.port or target.username or target.password or target.path or target.query or target.fragment or not token:
                    return ToolCallResult(block=True, reason="ACP permission channel is invalid")
                reader, writer = await asyncio.open_connection("127.0.0.1", target.port, limit=65536)
                # Authenticate on a short first line before sending arguments.
                writer.write((json.dumps({"token": token}) + "\n").encode("utf-8"))
            else:
                reader, writer = await asyncio.open_unix_connection(path)
            writer.write((json.dumps(request) + "\n").encode("utf-8"))
            await writer.drain()
            answer = json.loads(await reader.readline())
        if answer.get("allow") is True:
            return None
        return ToolCallResult(block=True, reason="User declined this tool in the ACP client")
    except (OSError, ValueError, TimeoutError) as exc:
        return ToolCallResult(block=True, reason=f"ACP permission unavailable: {type(exc).__name__}")
    finally:
        if writer is not None:
            writer.close()
            await writer.wait_closed()


def setup(aelix):
    aelix.on("tool_call", before_tool)
    ready_file = os.environ.get("AELIX_ACP_READY_FILE")
    nonce = os.environ.get("AELIX_ACP_NONCE")
    if not ready_file or not nonce:
        raise RuntimeError("ACP bridge must be launched by aelix-acp")
    Path(ready_file).write_text(nonce)
    os.chmod(ready_file, 0o600)
