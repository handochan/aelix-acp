"""Private, inherited writer leases for cooperating managed ACP sessions.

These are not native CLI locks. Closing a parent reference must leave the
actual native interpreter's reference locked until that process exits.
"""
import contextlib
import errno
import os
from pathlib import Path
import stat
import subprocess


class OwnershipError(Exception):
    """A fixed, payload-free ownership failure suitable for protocol mapping."""


class LeaseBusy(OwnershipError):
    pass


def _windows_api():
    import ctypes
    from ctypes import wintypes
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                               wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    api.CreateFileW.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    api.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                               wintypes.LPVOID, wintypes.DWORD]
    api.GetFileInformationByHandleEx.restype = wintypes.BOOL
    api.GetCurrentProcess.restype = wintypes.HANDLE
    api.DuplicateHandle.argtypes = [wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
                                   ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD,
                                   wintypes.BOOL, wintypes.DWORD]
    api.DuplicateHandle.restype = wintypes.BOOL
    return api


def _windows_path(path):
    value = os.path.abspath(path)
    if value.startswith('\\\\?\\'):
        return value
    return '\\\\?\\UNC\\' + value[2:] if value.startswith('\\\\') else '\\\\?\\' + value


def local_path(path):
    """Use Windows's extended namespace for owned filesystem operations."""
    return Path(_windows_path(path)) if os.name == 'nt' else Path(path)


def inherited_identity(reference):
    """Observe a held OS reference without closing it or exposing file content."""
    try:
        reference = int(reference)
        if reference < 0:
            raise ValueError
        if os.name != 'nt':
            value = os.fstat(reference)
            if not stat.S_ISREG(value.st_mode) or not value.st_ino:
                raise ValueError
            return f'posix:{value.st_dev}:{value.st_ino}'
        import ctypes
        from ctypes import wintypes

        class FileId(ctypes.Structure):
            _fields_ = [('volume', ctypes.c_ulonglong), ('identifier', ctypes.c_ubyte * 16)]

        class AttributeTag(ctypes.Structure):
            _fields_ = [('attributes', wintypes.DWORD), ('tag', wintypes.DWORD)]

        api = _windows_api()
        identity, attributes = FileId(), AttributeTag()
        if not api.GetFileInformationByHandleEx(reference, 18, ctypes.byref(identity), ctypes.sizeof(identity)):
            raise OSError
        if not api.GetFileInformationByHandleEx(reference, 9, ctypes.byref(attributes), ctypes.sizeof(attributes)):
            raise OSError
        if attributes.attributes & 0x400:  # Reparse points are not lease files.
            raise ValueError
        if not any(identity.identifier):
            raise ValueError
        return f'windows:{identity.volume}:{bytes(identity.identifier).hex()}'
    except (OSError, ValueError, OverflowError):
        raise OwnershipError('Private writer reference cannot be verified') from None


def directory_identity(path):
    """Use physical directory identity, including Windows's full 128-bit ID."""
    try:
        path = Path(path).resolve(strict=True)
        if not path.is_dir():
            raise OwnershipError('Private state directory cannot be verified')
        if os.name != 'nt':
            value = path.stat()
            if not value.st_ino:
                raise OwnershipError('Private state directory cannot be verified')
            return f'posix:{value.st_dev}:{value.st_ino}'
    except (OSError, RuntimeError, ValueError):
        raise OwnershipError('Private state directory cannot be verified') from None
    import ctypes
    api = _windows_api()
    # Share directory access; this is identity observation, not the writer lock.
    handle = api.CreateFileW(_windows_path(path), 0x80, 7, None, 3, 0x02000000, None)
    if handle in (None, ctypes.c_void_p(-1).value):
        raise OwnershipError('Private state directory cannot be verified')
    try:
        return inherited_identity(handle)
    finally:
        api.CloseHandle(handle)


def file_identity(path):
    """Observe the database path's physical file without adopting a symlink."""
    try:
        path = Path(path).absolute()
        if path.is_symlink() or not path.is_file():
            raise OwnershipError('Private state file cannot be verified')
        if os.name != 'nt':
            value = path.stat(follow_symlinks=False)
            if not stat.S_ISREG(value.st_mode) or not value.st_ino:
                raise OwnershipError('Private state file cannot be verified')
            return f'posix:{value.st_dev}:{value.st_ino}'
        import ctypes
        api = _windows_api()
        handle = api.CreateFileW(_windows_path(path), 0x80, 7, None, 3, 0x00200080, None)
        if handle in (None, ctypes.c_void_p(-1).value):
            raise OwnershipError('Private state file cannot be verified')
        try:
            return inherited_identity(handle)
        finally:
            api.CloseHandle(handle)
    except (OSError, RuntimeError, ValueError):
        raise OwnershipError('Private state file cannot be verified') from None


class WriterLease:
    """An exclusive OS lease whose child references outlive parent cleanup.

    Never unlink, replace, truncate, or explicitly unlock a lease. A separate
    immutable binding must also verify this file's identity before dispatch.
    """

    def __init__(self, path):
        self.path = Path(path).absolute()
        self.reference = None
        self.identity = None
        self.parent_identity = None
        self.child_started = False

    def acquire(self, *, create=True):
        if self.reference is not None:
            raise OwnershipError('Private writer reference is already held')
        reference = None
        try:
            if self.path.is_symlink() or not self.path.parent.is_dir():
                raise OwnershipError('Private writer lease path is invalid')
            if os.name == 'nt':
                import ctypes
                api = _windows_api()
                # Nonshare open references, explicitly inherited below, protect
                # the same file object until the final native reference closes.
                reference = api.CreateFileW(_windows_path(self.path), 0xC0000000, 0,
                                            None, 4 if create else 3, 0x00200080, None)
                if reference in (None, ctypes.c_void_p(-1).value):
                    reference = None
                    if ctypes.get_last_error() in (32, 33):
                        raise LeaseBusy('Private native writer is still owned')
                    raise OwnershipError('Private writer lease cannot be acquired')
            else:
                import fcntl
                flags = os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0) | (os.O_CREAT if create else 0)
                reference = os.open(self.path, flags, 0o600)
                value = os.fstat(reference)
                if not stat.S_ISREG(value.st_mode) or value.st_mode & 0o077:
                    raise OwnershipError('Private writer lease permissions are invalid')
                try:
                    fcntl.flock(reference, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as error:
                    if error.errno in (errno.EACCES, errno.EAGAIN):
                        raise LeaseBusy('Private native writer is still owned') from None
                    raise
            self.identity = inherited_identity(reference)
            self.parent_identity = directory_identity(self.path.parent)
            self.reference = reference
            self.verify()
            return self
        except BaseException as error:
            if reference is not None:
                if os.name == 'nt':
                    _windows_api().CloseHandle(reference)
                else:
                    os.close(reference)
            self.reference = None
            if isinstance(error, OSError):
                raise OwnershipError('Private writer lease cannot be acquired') from None
            raise

    def verify(self):
        """Check the held object and its namespace again before dispatch."""
        try:
            if (self.reference is None or inherited_identity(self.reference) != self.identity
                    or directory_identity(self.path.parent) != self.parent_identity):
                raise OwnershipError('Private writer reference changed')
            # Windows's nonshare open also excludes rename/delete. POSIX flock
            # survives unlink, so explicitly reject a substituted pathname.
            if os.name != 'nt' and file_identity(self.path) != self.identity:
                raise OwnershipError('Private writer reference changed')
        except (OSError, RuntimeError, ValueError):
            raise OwnershipError('Private writer reference cannot be verified') from None

    def close(self):
        reference, self.reference = self.reference, None
        if reference is not None:
            if os.name == 'nt':
                _windows_api().CloseHandle(reference)
            else:
                # LOCK_UN would also unlock the child's shared file description.
                os.close(reference)

    @contextlib.contextmanager
    def inheritance(self):
        """Yield (child reference, Popen kwargs); the Job remains noninherited."""
        if self.reference is None:
            raise OwnershipError('Private writer reference is not held')
        self.verify()
        self.child_started = True
        if os.name != 'nt':
            yield self.reference, {'pass_fds': (self.reference,)}
            return
        import ctypes
        from ctypes import wintypes
        api, duplicate = _windows_api(), wintypes.HANDLE()
        process = api.GetCurrentProcess()
        if not api.DuplicateHandle(process, self.reference, process, ctypes.byref(duplicate), 0, True, 2):
            raise OwnershipError('Private writer reference cannot be inherited')
        try:
            startup = subprocess.STARTUPINFO()
            startup.lpAttributeList = {'handle_list': [duplicate.value]}
            yield duplicate.value, {'startupinfo': startup, 'close_fds': True}
        finally:
            api.CloseHandle(duplicate)
