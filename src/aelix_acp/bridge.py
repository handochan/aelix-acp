"""Aelix extension: wait for ACP consent BEFORE every tool execution.

Loaded explicitly into the RPC child. No network listeners or credentials. The
parent owns a private Unix socket; absent/broken channels block the tool.
"""
import asyncio
import json
import os
from pathlib import Path

from aelix_agent_core.harness.hooks import ToolCallResult


async def before_tool(event, ctx):
    path = os.environ.get("AELIX_ACP_SOCKET")
    if not path:
        return ToolCallResult(block=True, reason="ACP permission channel is absent")
    writer = None
    try:
        async with asyncio.timeout(300):
            reader, writer = await asyncio.open_unix_connection(path)
            writer.write((json.dumps({"toolCallId": event.tool_call_id,
                                      "name": event.tool_name,
                                      "args": event.args}) + "\n").encode())
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
