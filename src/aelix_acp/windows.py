"""Owned Windows worker jobs; closing the ACP process kills its worker descendants."""
import ctypes
from ctypes import wintypes
import os
import importlib.util
from pathlib import Path
import subprocess
import sys
import threading


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
    options = {}
    reference = os.environ.get('AELIX_ACP_LEASE_REFERENCE')
    expected = os.environ.get('AELIX_ACP_LEASE_ID')
    if reference is not None or expected is not None:
        # -I -S intentionally ignores PYTHONPATH/site-packages. Read only the
        # adapter-owned helper at a fixed relative path, after Job release.
        helper = Path(__file__).with_name('operations') / 'lease.py'
        spec = importlib.util.spec_from_file_location('aelix_acp_gate_lease', helper)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        reference = int(reference)
        if module.inherited_identity(reference) != expected:
            raise SystemExit(2)
        os.set_handle_inheritable(reference, True)
        startup = subprocess.STARTUPINFO()
        startup.lpAttributeList = {'handle_list': [reference]}
        options = {'startupinfo': startup, 'close_fds': True}
    raise SystemExit(subprocess.call(sys.argv[1:], shell=False, **options))
