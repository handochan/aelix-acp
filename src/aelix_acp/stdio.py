"""Raw descriptor I/O for the ACP process."""
import asyncio
import concurrent.futures
import errno
import os
import queue
import select
import stat
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
    size = len(remaining)
    while remaining:
        try:
            written = os.write(fd, remaining[:size])
        except OSError as error:
            if os.name == "nt" and error.errno == errno.ENOSPC and stat.S_ISFIFO(os.fstat(fd).st_mode):
                # Python 3.11 exposes the CRT's ENOSPC when a nowait pipe writes
                # zero bytes. Retry a smaller write before waiting for space.
                if size > 1:
                    size //= 2
                    continue
            elif error.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
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
        size = len(remaining)


_stderr_queue = queue.Queue(maxsize=8)
_stderr_thread = None
_stderr_start_lock = threading.Lock()


def _pump_stderr():
    binary_fds = set()
    while True:
        fd, raw, done = _stderr_queue.get()
        try:
            if os.name == "nt" and fd not in binary_fds:
                import msvcrt
                # setmode and _write take the same CRT descriptor lock.
                msvcrt.setmode(fd, os.O_BINARY)
                binary_fds.add(fd)
            write_all(fd, raw)
        except OSError:
            pass
        finally:
            if done is not None:
                done.set()


def offer_stderr(fd, raw, done=None):
    """Admit a whole record without waiting for the inherited stderr reader."""
    global _stderr_thread
    with _stderr_start_lock:
        if _stderr_thread is None:
            _stderr_thread = threading.Thread(target=_pump_stderr, name="acp-stderr", daemon=True)
            _stderr_thread.start()
    try:
        _stderr_queue.put_nowait((fd, raw, done))
    except queue.Full:
        return False
    return True


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

        if offer_stderr(fd, raw, done) and done is not None:
            # Give a terminal diagnostic time to flush without stalling exit.
            done.wait(0.2)
    except OSError:
        pass
