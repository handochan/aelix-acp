"""Aelix extension: wait for ACP consent BEFORE every tool execution.

Loaded explicitly into the RPC child. The parent owns a private Unix socket or
authenticated Windows loopback channel; absent/broken channels block the tool.
"""
import asyncio
import errno
import hmac
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


def compatible_fork_peer(marker):
    """Check the ACP-specific marker without taking the parent's writer lease."""
    try:
        with open(marker, "r+b") as stream:
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                contended = {errno.EAGAIN, errno.EWOULDBLOCK}
                if os.name == "nt":
                    contended.update({errno.EACCES, errno.EDEADLK})
                if error.errno in contended:
                    return True
                raise
    except FileNotFoundError:
        pass
    return False


async def fork_native_head(source, cwd):
    """Freeze a source read and let the pinned native repository publish its HEAD."""
    from aelix_agent_core.session.fs import LocalFileSystem
    from aelix_agent_core.session.jsonl_repo import JsonlSessionRepo
    from aelix_agent_core.session.jsonl_storage import load_jsonl_session_metadata
    from aelix_agent_core.session.repo_utils import ForkOptions, fork_at_leaf
    from aelix_agent_core.session.session_lock import SessionWriterLock
    from aelix_coding_agent.cli.config import get_session_dir

    path = Path(source["file"]).resolve(strict=True)
    peer = source["peer"]
    lock = SessionWriterLock(str(path))
    try:
        if peer and not compatible_fork_peer(peer):
            return {"error": "peer_locked"}
        acquired = lock.try_acquire()
        if lock.degraded or lock.error is not None:
            return {"error": "unavailable"}
        if not acquired and not peer:
            return {"error": "locked"}

        # An active ACP parent retains its native lock. The calling adapter
        # serializes this read with that parent's ACP mutations. Rechecking the
        # file also catches a concurrent close, replacement or external append.
        def revision():
            current = path.stat()
            return current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns

        before = revision()
        snapshot = await asyncio.to_thread(path.read_text, encoding="utf-8")
        if not snapshot.endswith("\n") or revision() != before:
            return {"error": "changed"}
        if not acquired and not compatible_fork_peer(peer):
            return {"error": "peer_locked"}

        class SnapshotFileSystem(LocalFileSystem):
            async def read_text_file(self, filename):
                if filename == str(path):
                    return snapshot
                return await super().read_text_file(filename)

            async def read_text_lines(self, filename, max_lines=None):
                if filename == str(path) and max_lines == 1:
                    return [snapshot.partition("\n")[0]]
                return await super().read_text_lines(filename, max_lines=max_lines)

        fs = SnapshotFileSystem()
        repo = JsonlSessionRepo(fs=fs, sessions_root=get_session_dir())
        metadata = await load_jsonl_session_metadata(fs, str(path))
        parent = await repo.open(metadata)
        branch = await repo.fork(metadata, ForkOptions(
            cwd=cwd, entry_id=fork_at_leaf(await parent.get_leaf_id()), position="at"))
        return {"file": branch.session_file}
    finally:
        lock.release()


def setup(aelix):
    aelix.on("tool_call", before_tool)
    ready_file = os.environ.get("AELIX_ACP_READY_FILE")
    nonce = os.environ.get("AELIX_ACP_NONCE")
    if not ready_file or not nonce:
        raise RuntimeError("ACP bridge must be launched by aelix-acp")
    fork_source = os.environ.pop("AELIX_ACP_FORK_SOURCE", None)
    if fork_source is not None:
        source = json.loads(fork_source)
        invoked = False

        async def fork_head(args, ctx):
            nonlocal invoked
            if invoked or not hmac.compare_digest(args.encode(), nonce.encode()):
                raise ValueError("The fork command requires its startup authorization")
            invoked = True
            return json.dumps(await fork_native_head(source, str(Path.cwd())))

        aelix.register_command("aelix-acp-fork-head", description="Prepare native HEAD for the ACP child", handler=fork_head)
    Path(ready_file).write_text(nonce)
    os.chmod(ready_file, 0o600)
