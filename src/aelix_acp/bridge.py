"""Aelix extension: fail closed unless the owning ACP process allows a tool."""
import asyncio
import json
import os
from pathlib import Path

from aelix_agent_core.harness.hooks import ToolCallResult


async def before_tool(event, ctx):
    path = os.environ.get("AELIX_ACP_SOCKET")
    endpoint = os.environ.get("AELIX_ACP_ENDPOINT")
    if not path and not endpoint:
        return ToolCallResult(block=True, reason="ACP permission channel is absent")
    writer = None
    try:
        async with asyncio.timeout(300):
            token = None
            if endpoint:
                address = json.loads(endpoint)
                if address.get("host") != "127.0.0.1":
                    raise ValueError("Permission endpoint must be loopback")
                reader, writer = await asyncio.open_connection(address["host"], address["port"])
                token = address["token"]
            else:
                reader, writer = await asyncio.open_unix_connection(path)
            provenance = None
            write_allowed = False
            try:
                from aelix_coding_agent.tools.provenance import builtin_provenance
                tools = getattr(getattr(event, "context", None), "tools", [])
                actual = next((tool for tool in reversed(tools) if tool.name == event.tool_name), None)
                if actual is not None:
                    provenance = builtin_provenance(actual)
            except (ImportError, AttributeError, TypeError):
                pass  # Unknown or older tool objects always require approval.
            if provenance == "write":
                try:
                    # Run the native containment/sensitive-path policy in its
                    # own interpreter. Missing compatibility means ASK.
                    from aelix_coding_agent.builtin.permission import _is_auto_allowable_write, _path_from_args
                    write_allowed = _is_auto_allowable_write(_path_from_args(event.args), ctx.cwd) is True
                except (ImportError, AttributeError, TypeError, ValueError, OSError):
                    pass
            writer.write((json.dumps({"toolCallId": event.tool_call_id,
                                      "name": event.tool_name, "args": event.args,
                                      "token": token, "provenance": provenance,
                                      "writeAllowed": write_allowed}) + "\n").encode())
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
