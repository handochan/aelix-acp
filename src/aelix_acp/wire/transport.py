"""Bounded JSONL and JSON-RPC validation, including Windows stdio pipes."""
from __future__ import annotations

import asyncio
import json
import math
import sys
import threading
import os
import queue

from .errors import ProtocolError

MAX_FRAME_BYTES = 16 * 1024 * 1024


class StdoutWriter:
    """Bounded daemon writes without blocking the loop or Python buffer locks."""
    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.fd = sys.stdout.fileno()
        if sys.platform == "win32":
            import msvcrt
            msvcrt.setmode(self.fd, os.O_BINARY)
        self.queue = queue.Queue(maxsize=2)
        self.credit = asyncio.Semaphore(2)
        self.pending = set()
        self.stopped = threading.Event()
        threading.Thread(target=self.pump, name="acp-stdout", daemon=True).start()

    async def write(self, raw):
        await self.credit.acquire()
        future = self.loop.create_future()
        self.pending.add(future)
        self.queue.put_nowait((raw, future))
        await future

    def settle(self, future, error):
        self.pending.discard(future)
        self.credit.release()
        if not future.done():
            if error is None:
                future.set_result(None)
            else:
                future.set_exception(error)

    def pump(self):
        while not self.stopped.is_set():
            item = self.queue.get()
            if item is None:
                return
            raw, future = item
            error = None
            try:
                view = memoryview(raw)
                while view:
                    written = os.write(self.fd, view)
                    if not written:
                        raise BrokenPipeError("Client stdout closed")
                    view = view[written:]
            except OSError as failure:
                error = failure
            try:
                self.loop.call_soon_threadsafe(self.settle, future, error)
            except RuntimeError:
                return

    def close(self):
        self.stopped.set()
        for future in self.pending:
            if not future.done():
                future.cancel()
        try:
            self.queue.put_nowait(None)
        except queue.Full:
            pass


def decode_frame(raw: bytes) -> dict:
    if len(raw) > MAX_FRAME_BYTES:
        raise ProtocolError("JSON-RPC frame exceeds the byte limit", -32600)
    try:
        value = json.loads(raw, parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
        pending = [value]
        visited = 0
        while pending:
            entry = pending.pop()
            visited += 1
            if visited > 100_000:
                raise ValueError("Too many JSON nodes")
            if isinstance(entry, float) and not math.isfinite(entry):
                raise ValueError("Non-finite JSON number")
            if isinstance(entry, str) and any(0xD800 <= ord(char) <= 0xDFFF for char in entry):
                raise ValueError("Invalid Unicode scalar")
            if isinstance(entry, dict):
                pending.extend(entry.keys())
                pending.extend(entry.values())
            elif isinstance(entry, list):
                pending.extend(entry)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ProtocolError("Invalid JSON", -32700) from None
    if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
        raise ProtocolError("Invalid JSON-RPC envelope", -32600)
    request_id = value.get("id")
    if request_id is not None and (type(request_id) not in (int, str) or (isinstance(request_id, str) and not request_id)):
        raise ProtocolError("Invalid JSON-RPC id", -32600)
    if "method" in value:
        if "id" in value and request_id is None:
            raise ProtocolError("ACP request ids cannot be null", -32600)
        if not isinstance(value["method"], str) or not value["method"]:
            raise ProtocolError("Invalid JSON-RPC method", -32600)
        if not isinstance(value.get("params", {}), dict):
            raise ProtocolError("ACP params must be an object", -32602)
    elif "id" not in value or ("result" in value) == ("error" in value):
        raise ProtocolError("Invalid JSON-RPC response", -32600)
    return value


async def stdin_reader() -> tuple[asyncio.StreamReader, object | None]:
    reader = asyncio.StreamReader(limit=MAX_FRAME_BYTES)
    loop = asyncio.get_running_loop()
    if sys.platform != "win32":
        transport, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
        return reader, transport

    # A daemon rather than the default executor: a cancelled blocked stdin read
    # must not hold asyncio.run()'s executor shutdown open on Windows.
    credit = threading.Semaphore(0)
    stopped = threading.Event()

    class DemandReader(asyncio.StreamReader):
        async def readline(self):
            credit.release()
            return await super().readline()

    class PumpControl:
        def close(self):
            stopped.set()
            credit.release()

    reader = DemandReader(limit=MAX_FRAME_BYTES)

    def pump() -> None:
        try:
            while not stopped.is_set():
                credit.acquire()
                if stopped.is_set():
                    return
                raw = sys.stdin.buffer.readline(MAX_FRAME_BYTES + 1)
                if not raw:
                    return
                if len(raw) > MAX_FRAME_BYTES:
                    loop.call_soon_threadsafe(reader.set_exception, ValueError("Input frame too large"))
                    return
                loop.call_soon_threadsafe(reader.feed_data, raw)
        except (OSError, ValueError) as error:
            try:
                loop.call_soon_threadsafe(reader.set_exception, error)
            except RuntimeError:
                pass
        finally:
            try:
                loop.call_soon_threadsafe(reader.feed_eof)
            except RuntimeError:
                pass

    threading.Thread(target=pump, name="acp-stdin", daemon=True).start()
    return reader, PumpControl()
