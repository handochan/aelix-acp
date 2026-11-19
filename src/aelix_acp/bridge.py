"""Aelix extension: wait for ACP consent BEFORE every tool execution.

Loaded explicitly into the RPC child. The parent owns a private Unix socket or
authenticated Windows loopback channel; absent/broken channels block the tool.
"""
import asyncio
import errno
import hashlib
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
    from aelix_agent_core.session.context import build_session_context
    from aelix_agent_core.session.fs import LocalFileSystem
    from aelix_agent_core.session.jsonl_repo import JsonlSessionRepo
    from aelix_agent_core.session.jsonl_storage import JsonlSessionStorage, load_jsonl_session_metadata
    from aelix_agent_core.session.repo_utils import fork_at_leaf, get_entries_to_fork
    from aelix_agent_core.session.session_lock import SessionWriterLock

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
        raw_snapshot = await asyncio.to_thread(path.read_bytes)
        if not raw_snapshot.endswith(b"\n") or revision() != before:
            return {"error": "changed"}
        if not acquired and not compatible_fork_peer(peer):
            return {"error": "peer_locked"}
        if hashlib.sha256(raw_snapshot).hexdigest() != source["expectedSha256"]:
            return {"error": "head_changed"}
        snapshot = raw_snapshot.decode("utf-8")
        del raw_snapshot

        class SnapshotFileSystem(LocalFileSystem):
            async def read_text_file(self, filename):
                if filename == str(path):
                    return snapshot
                return await super().read_text_file(filename)

            async def read_text_lines(self, filename, max_lines=None):
                if filename == str(path) and max_lines == 1:
                    return [snapshot.partition("\n")[0]]
                return await super().read_text_lines(filename, max_lines=max_lines)

            async def rename_file(self, source_file, target_file):
                if target_file == source["target"]:
                    with open(source_file, "rb") as stream:
                        digest = hashlib.file_digest(stream, "sha256").hexdigest()
                    with open(source["receipt"], "x", encoding="ascii") as receipt:
                        receipt.write(digest)
                        receipt.flush()
                        os.fsync(receipt.fileno())
                await super().rename_file(source_file, target_file)

        fs = SnapshotFileSystem()
        repo = JsonlSessionRepo(fs=fs)
        metadata = await load_jsonl_session_metadata(fs, str(path))
        parent = await repo.open(metadata)
        entries = await get_entries_to_fork(parent.get_storage(), fork_at_leaf(await parent.get_leaf_id()), "at")
    finally:
        lock.release()

    # Project the same native cut without sending image bodies through RPC.
    with open(source["projection"], "x", encoding="utf-8") as projection:
        for message in build_session_context(entries).messages:
            if message.role not in ("user", "assistant"):
                continue
            content = message.content
            text = content if isinstance(content, str) else "\n".join(
                block.text for block in content if block.type == "text")
            for offset in range(0, len(text), 16384):
                update = {"sessionUpdate": "user_message_chunk" if message.role == "user" else "agent_message_chunk",
                          "content": {"type": "text", "text": text[offset:offset + 16384]}}
                projection.write(json.dumps(update, ensure_ascii=False) + "\n")

    # The adapter knows this path even if the RPC reply never arrives.
    # Check that the original worker's ACP lease still exists while holding
    # the target lock used by both native writers and draft recovery.
    target = Path(source["target"])
    target_lock = SessionWriterLock(str(target))
    try:
        if not target_lock.try_acquire() or not target_lock.held:
            return {"error": "unavailable"}
        if not compatible_fork_peer(source["workerLease"]) or target.exists() or target.is_symlink():
            return {"error": "unavailable"}
        await JsonlSessionStorage.create(
            fs, str(target), cwd=cwd, session_id=source["nativeId"],
            parent_session_path=str(path), entries=entries)
        return {"file": str(target)}
    finally:
        target_lock.release()


def register_selector_startup(aelix, filename, nonce):
    from aelix_agent_core.session.jsonl_storage import JsonlSessionStorage
    from aelix_agent_core.session.read_only import ReadOnlySessionStorage

    class SelectorStartupStorage(ReadOnlySessionStorage):
        """Run native selector hooks while keeping temporary selections off disk."""

        async def append_entry(self, entry):
            if entry.type not in ("model_change", "thinking_level_change"):
                await super().append_entry(entry)

        async def append_to_leaf(self, build):
            # Selector changes during profile restoration are temporary. Use
            # the same filter as append_entry for native's atomic append API.
            entry = build(await self.create_entry_id(), await self.get_leaf_id())
            await self.append_entry(entry)
            return entry

    expected = Path(filename)
    phase = "begin"
    bound_session = guarded = None

    async def selector_startup(args, ctx):
        nonlocal phase, bound_session, guarded
        if not phase or not hmac.compare_digest(args.encode(), f"{phase} {nonce}".encode()):
            raise ValueError("Selector startup requires its one-time authorization")
        session = ctx.session_manager.get_session()
        if session is None or not session.session_file or Path(session.session_file).resolve(strict=True) != expected:
            raise ValueError("Selector startup requires the bound native session")
        storage = session.get_storage()
        if phase == "begin":
            if type(storage) is not JsonlSessionStorage:
                raise ValueError("Selector startup requires writable native JSONL storage")
            # The native worker retains its writer lock. Use the pinned
            # Session storage slot; never replace, truncate or reopen the file.
            bound_session, guarded = session, SelectorStartupStorage(storage)
            session._storage = guarded
            # A fork worker rebuilds its extension runtime when binding the
            # child. Retain the authorization until the matching file is
            # protected, then make later extension reloads ineligible.
            os.environ.pop("AELIX_ACP_SELECTOR_STARTUP_FILE", None)
            phase = "finish"
        else:
            if session is not bound_session or storage is not guarded:
                raise ValueError("The guarded native session changed during startup")
            session._storage = guarded.inner
            phase = None
            bound_session = guarded = None
        return "selector-startup-ok"

    aelix.register_command("aelix-acp-selector-startup", description="Private ACP startup guard", handler=selector_startup)


def setup(aelix):
    aelix.on("tool_call", before_tool)
    ready_file = os.environ.get("AELIX_ACP_READY_FILE")
    nonce = os.environ.get("AELIX_ACP_NONCE")
    if not ready_file or not nonce:
        raise RuntimeError("ACP bridge must be launched by aelix-acp")
    selector_file = os.environ.get("AELIX_ACP_SELECTOR_STARTUP_FILE")
    if selector_file is not None:
        register_selector_startup(aelix, selector_file, nonce)
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
