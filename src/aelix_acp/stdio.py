"""Raw descriptor I/O for the ACP process."""
import asyncio
import concurrent.futures
import errno
import os
import queue
import select
import sys
import threading
import time


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


def write_all(fd, raw):
    """Finish a raw write without changing the inherited descriptor's flags."""
    remaining = memoryview(raw)
    writable = None
    while remaining:
        try:
            written = os.write(fd, remaining)
        except OSError as error:
            if error.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                raise
            if os.name == "nt":
                # select() cannot wait on Windows pipe handles.
                time.sleep(0.01)
            else:
                if writable is None:
                    writable = select.poll()
                    writable.register(fd, select.POLLOUT)
                # HUP/ERR also wake poll. Let the next write report the OS error.
                writable.poll()
            continue
        if not written:
            raise BrokenPipeError("Client stdout closed")
        remaining = remaining[written:]


_stderr_lock = threading.Lock()
_diagnostics = queue.Queue(maxsize=8)
_diagnostics_thread = None
_diagnostics_start_lock = threading.Lock()


def write_stderr(fd, raw):
    # Native stderr forwarding and ACP diagnostics share the inherited stream.
    with _stderr_lock:
        write_all(fd, raw)


def _pump_diagnostics():
    while True:
        fd, raw, done = _diagnostics.get()
        try:
            write_stderr(fd, raw)
        except OSError:
            pass
        finally:
            if done is not None:
                done.set()


def write_diagnostic(message):
    """Keep backpressure on stderr off the ACP event loop."""
    stream = sys.stderr
    if stream is None:
        return
    try:
        try:
            fd = stream.fileno()
        except (AttributeError, OSError):
            fd = None  # In-memory stderr redirection.
        if fd is None or (os.name == "nt" and os.isatty(fd)):
            print(message, file=stream)
            return

        raw = (message + "\n").encode(stream.encoding or "utf-8", stream.errors or "backslashreplace")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            done = threading.Event()
        else:
            done = None

        global _diagnostics_thread
        with _diagnostics_start_lock:
            if _diagnostics_thread is None:
                _diagnostics_thread = threading.Thread(target=_pump_diagnostics, name="acp-diagnostics", daemon=True)
                _diagnostics_thread.start()
        # Diagnostics are best effort if the reader stalls. An admitted record
        # is written in full rather than publishing a partial diagnostic.
        _diagnostics.put_nowait((fd, raw, done))
        if done is not None:
            # Give a terminal diagnostic time to flush without stalling exit.
            done.wait(0.2)
    except (OSError, queue.Full):
        pass
