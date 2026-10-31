"""Bounded termination of process groups owned by this adapter."""
from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path
import signal
import sys


async def kill_windows_tree(process):
    # Never resolve taskkill through a project's cwd/PATH.
    executable = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "taskkill.exe"
    with contextlib.suppress(OSError):
        killer = await asyncio.create_subprocess_exec(str(executable), "/PID", str(process.pid), "/T", "/F",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            await asyncio.wait_for(killer.wait(), 5)
        except TimeoutError:
            killer.kill()
            await killer.wait()
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()


async def terminate_owned(process):
    if process.returncode is not None:
        return
    if sys.platform == "win32":
        try:
            process.send_signal(signal.CTRL_BREAK_EVENT)
        except OSError:
            await kill_windows_tree(process)
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), 5)
    except TimeoutError:
        if sys.platform == "win32":
            await kill_windows_tree(process)
        else:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
    finally:
        if sys.platform != "win32":
            # The group can outlive its leader after a graceful TERM.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
