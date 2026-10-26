"""Aelix extension: wait for ACP consent BEFORE every tool execution.

Loaded explicitly into the RPC child. The parent owns a private Unix socket or
authenticated Windows loopback channel; absent/broken channels block the tool.
"""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from aelix_agent_core.harness.hooks import ToolCallResult

_managed_lease = None


def _check_managed_lease():
    if _managed_lease is not None:
        observe, reference, expected = _managed_lease
        if observe(reference) != expected:
            raise RuntimeError('Managed native writer reference changed')


async def before_tool(event, ctx):
    try:
        _check_managed_lease()
    except Exception:
        return ToolCallResult(block=True, reason='Managed native ownership is unavailable')
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
                request["token"] = token
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
    global _managed_lease
    aelix.on("tool_call", before_tool)
    ready_file = os.environ.get("AELIX_ACP_READY_FILE")
    nonce = os.environ.get("AELIX_ACP_NONCE")
    if not ready_file or not nonce:
        raise RuntimeError("ACP bridge must be launched by aelix-acp")
    reference = os.environ.get('AELIX_ACP_LEASE_REFERENCE')
    expected = os.environ.get('AELIX_ACP_LEASE_ID')
    if reference is not None or expected is not None:
        if not reference or not expected:
            raise RuntimeError('Managed native writer reference is missing')
        # The native interpreter need not have ACP installed on sys.path.
        helper = Path(__file__).with_name('operations') / 'lease.py'
        spec = importlib.util.spec_from_file_location('aelix_acp_native_lease', helper)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _managed_lease = (module.inherited_identity, int(reference), expected)
        _check_managed_lease()
        generation = os.environ.get('AELIX_ACP_WORKER_GENERATION')
        if not generation:
            raise RuntimeError('Managed native generation is missing')
        ready = {'nonce': nonce, 'leaseId': expected, 'nativePid': os.getpid(), 'generation': generation}
        Path(ready_file).write_text(json.dumps(ready), encoding='utf-8')
    else:
        Path(ready_file).write_text(nonce)
    os.chmod(ready_file, 0o600)
