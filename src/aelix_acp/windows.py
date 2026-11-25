"""Windows pipe state and owned worker jobs."""
import ctypes
from ctypes import wintypes
from functools import cache
import os
import subprocess
import sys
import threading


class _IoStatusBlock(ctypes.Structure):
    # The first field is a union of NTSTATUS and a pointer.
    _fields_ = [("status", ctypes.c_void_p), ("information", ctypes.c_size_t)]


class _PipeLocalInformation(ctypes.Structure):
    _fields_ = [(name, wintypes.ULONG) for name in (
        "type", "configuration", "maximum_instances", "current_instances",
        "inbound_quota", "read_data_available", "outbound_quota", "write_quota_available",
        "state", "end",
    )]


@cache
def _query_file_information():
    query = ctypes.WinDLL("ntdll").NtQueryInformationFile
    query.argtypes = [wintypes.HANDLE, ctypes.POINTER(_IoStatusBlock), ctypes.c_void_p,
                      wintypes.ULONG, ctypes.c_int]
    query.restype = wintypes.DWORD
    return query


def pipe_input_ended(fd):
    """Read the pipe connection state even when unread bytes remain."""
    import _winapi
    import msvcrt

    handle = msvcrt.get_osfhandle(fd)
    io_status = _IoStatusBlock()
    info = _PipeLocalInformation()
    status = _query_file_information()(handle, ctypes.byref(io_status), ctypes.byref(info),
                                       ctypes.sizeof(info), 24)  # FilePipeLocalInformation
    if status == 0:
        return info.state in (1, 4)  # FILE_PIPE_DISCONNECTED_STATE / FILE_PIPE_CLOSING_STATE
    if status in (0xC00000B0, 0xC00000B1, 0xC000014B):  # Disconnected, closing, broken
        return True
    # A pipe provider may not implement this information class. A failing
    # peek still reports a read-side error; zero available bytes can be live.
    try:
        _winapi.PeekNamedPipe(handle, 0)
    except OSError as error:
        if error.winerror == 233:  # ERROR_PIPE_NOT_CONNECTED
            return True
        raise
    return False


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("ProcessTime", ctypes.c_longlong), ("JobTime", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD), ("MinWorkingSet", ctypes.c_size_t),
        ("MaxWorkingSet", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t), ("Priority", wintypes.DWORD), ("Scheduling", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in
                ("ReadOps", "WriteOps", "OtherOps", "ReadBytes", "WriteBytes", "OtherBytes")]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("Basic", _BasicLimits), ("Io", _IoCounters),
                ("ProcessMemory", ctypes.c_size_t), ("JobMemory", ctypes.c_size_t),
                ("PeakProcessMemory", ctypes.c_size_t), ("PeakJobMemory", ctypes.c_size_t)]


class WorkerJob:
    """Bind only owned native workers to a non-inheritable kill-on-close job."""

    def __init__(self):
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.api.CreateJobObjectW.restype = wintypes.HANDLE
        self.api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.api.SetInformationJobObject.restype = wintypes.BOOL
        self.api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.api.OpenProcess.restype = wintypes.HANDLE
        self.api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.api.AssignProcessToJobObject.restype = wintypes.BOOL
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api.CloseHandle.restype = wintypes.BOOL
        self.api.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
        self.api.IsProcessInJob.restype = wintypes.BOOL
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = _ExtendedLimits()
        limits.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE.
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.get_last_error()
            self.close()
            raise ctypes.WinError(error)

    def assign(self, pid):
        process = self.api.OpenProcess(0x0100 | 0x0001, False, pid)  # SET_QUOTA | TERMINATE.
        if not process:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not self.api.AssignProcessToJobObject(self.handle, process):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self.api.CloseHandle(process)

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None

    def contains(self, pid):
        process = self.api.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION.
        if not process:
            return False
        try:
            belongs = wintypes.BOOL()
            return bool(self.api.IsProcessInJob(process, self.handle, ctypes.byref(belongs)) and belongs.value)
        finally:
            self.api.CloseHandle(process)


if __name__ == "__main__":
    # No buffered stdin lock/prefetch. RPC bytes after this gate go straight to
    # the native child. Parent owns the sole input write end and sends this only
    # after AssignProcessToJobObject succeeds; EOF/wrong byte is fail-closed.
    deadline = threading.Timer(10, lambda: os._exit(2))
    deadline.daemon = True
    deadline.start()
    try:
        import msvcrt
        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        ready = os.read(sys.stdin.fileno(), 1)
    finally:
        deadline.cancel()
        deadline.join()
    if ready != b"\0" or len(sys.argv) < 2:
        raise SystemExit(2)
    raise SystemExit(subprocess.call(sys.argv[1:], shell=False))
