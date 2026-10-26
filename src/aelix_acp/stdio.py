"""Bounded raw descriptor input without Windows Proactor pipe or buffered shutdown locks."""
import asyncio
import concurrent.futures
import os
import sys
import threading


class StdinReader:
    """Read newline frames in a daemon with bounded queue/backpressure on every OS."""

    def __init__(self, limit=16 * 1024 * 1024, fd=None):
        self.loop = asyncio.get_running_loop()
        self.queue = asyncio.Queue(maxsize=2)
        self.stopped = threading.Event()
        self.limit = limit
        self.fd = sys.stdin.fileno() if fd is None else fd
        if sys.platform == "win32":
            import msvcrt
            msvcrt.setmode(self.fd, os.O_BINARY)
        self.thread = threading.Thread(target=self._pump, daemon=True)
        self.thread.start()

    def _deliver(self, item):
        if self.stopped.is_set():
            return False
        delivery = self.queue.put(item)
        try:
            future = asyncio.run_coroutine_threadsafe(delivery, self.loop)
        except RuntimeError:
            delivery.close()
            return False
        while not self.stopped.is_set():
            try:
                future.result(timeout=0.2)
                return True
            except concurrent.futures.TimeoutError:
                continue
            except concurrent.futures.CancelledError:
                return False
        future.cancel()
        return False

    def _pump(self):
        buffer = b""
        try:
            while not self.stopped.is_set():
                chunk = os.read(self.fd, 4096)
                if not chunk:
                    if buffer and not self._deliver(buffer):
                        return
                    self._deliver(b"")
                    return
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if len(line) >= self.limit:
                        self._deliver(ValueError("ACP input frame exceeds limit"))
                        return
                    if not self._deliver(line + b"\n"):
                        return
                if len(buffer) > self.limit:
                    self._deliver(ValueError("ACP input frame exceeds limit"))
                    return
        except OSError:
            self._deliver(b"")

    async def readline(self):
        item = await self.queue.get()
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.stopped.set()
