"""Immutable native/session binding metadata; no native history mutation."""
import hashlib
import json
import os
import stat

from .ledger import LedgerError, token
from .lease import OwnershipError, directory_identity, file_identity, local_path


class NativeBindings:
    def __init__(self, ledger):
        self.ledger = ledger

    @classmethod
    def require_unmanaged(cls, native_file):
        """A flag change cannot reopen a registered managed history unfenced."""
        _, _, marker = cls.native_identity(native_file)
        if marker.exists() or marker.is_symlink():
            raise OwnershipError('Managed native history requires its original operation store')

    @staticmethod
    def native_identity(native_file):
        """Read only the bounded native session header, never transcript bodies."""
        try:
            path = local_path(native_file)
            if path.is_symlink():
                raise OwnershipError('Native session binding cannot be verified')
            path = path.resolve(strict=True)
            value = path.stat()
            if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
                raise OwnershipError('Native session binding requires one regular file')
            with path.open('rb') as source:
                line = source.readline(65537)
            if len(line) > 65536:
                raise ValueError
            header = json.loads(line)
            if not isinstance(header, dict) or header.get('type') != 'session':
                raise ValueError
            native_id = token(header.get('id'))
            parent_id = directory_identity(path.parent)
            marker = path.parent / '.aelix-acp-bindings' / (hashlib.sha256(native_id.encode()).hexdigest() + '.json')
            return native_id, parent_id, marker
        except (OSError, ValueError, RuntimeError, LedgerError):
            raise OwnershipError('Native session binding cannot be verified') from None

    @staticmethod
    def _read_marker(path):
        try:
            if path.is_symlink() or not path.is_file():
                raise ValueError
            value = path.stat()
            if value.st_nlink != 1 or (os.name != 'nt' and value.st_mode & 0o077):
                raise ValueError
            with path.open('rb') as source:
                data = source.read(2049)
            if len(data) > 2048:
                raise ValueError
            result = json.loads(data)
            if not isinstance(result, dict):
                raise ValueError
            return result
        except (OSError, ValueError):
            raise OwnershipError('Native ownership metadata cannot be verified') from None

    def _marker_value(self, session_id, profile, native_id, parent_id, native_file_id, lease_id):
        return dict(schema='aelix.private-native-binding/1', storeId=self.ledger.store_id,
                    rootId=self.ledger._root_id, sessionId=token(session_id), profile=token(profile),
                    nativeId=native_id, nativeParentId=parent_id, nativeFileId=native_file_id, leaseId=lease_id)

    def prepare_writer(self, session_id, profile, *, registered_file=None):
        """Fence a new unpublished session or a known registered ACP mapping.

        The caller obtains registered_file only from its validated sessions DB;
        this is not a path/import API. An existing broken binding is never reset.
        """
        row = self.ledger.session_binding(session_id)
        marker_value = None
        if registered_file is not None:
            native_id, parent_id, marker = self.native_identity(registered_file)
            if marker.exists():
                marker_value = self._read_marker(marker)
                lease_id = marker_value.get('leaseId')
                if marker_value != self._marker_value(session_id, profile, native_id, parent_id,
                                                       file_identity(local_path(registered_file)), lease_id):
                    raise OwnershipError('Native session belongs to another managed binding')
            elif row is not None:
                raise OwnershipError('Native ownership metadata is missing')
        elif row is not None:
            raise OwnershipError('Managed native session requires its registered mapping')
        lease = self.ledger.acquire_writer(session_id, create=row is None and marker_value is None)
        try:
            expected_id = row['lease_id'] if row is not None else marker_value and marker_value.get('leaseId')
            if expected_id is not None and lease.identity != expected_id:
                raise OwnershipError('Native writer lease identity changed')
            if registered_file is not None:
                self.bind_native(session_id, profile, registered_file, lease, allow_create=row is None)
            self.ledger.recover_session_pending(session_id, lease)
            return lease
        except BaseException:
            lease.close()
            raise

    def bind_native(self, session_id, profile, native_file, lease, *, native_id=None, allow_create=False):
        """Publish immutable ownership before session save/response or prompt."""
        self.ledger._validate_storage_identity()
        if lease.path != self.ledger.lease_path(session_id):
            raise OwnershipError('Native writer lease scope differs')
        lease.verify()
        observed_id, parent_id, marker = self.native_identity(native_file)
        if native_id is not None and token(native_id) != observed_id:
            raise OwnershipError('Native session identity differs from its state')
        expected = self._marker_value(session_id, profile, observed_id, parent_id,
                                      file_identity(local_path(native_file)), lease.identity)
        row = self.ledger.session_binding(session_id)
        if row is not None:
            if (row['profile'], row['native_id'], row['native_parent_id'], row['lease_id']) != (
                    profile, observed_id, parent_id, lease.identity):
                raise OwnershipError('Managed native binding changed')
            if not marker.exists():
                raise OwnershipError('Native ownership metadata is missing')
        try:
            if marker.parent.is_symlink():
                raise OwnershipError('Native ownership directory is invalid')
            if allow_create:
                marker.parent.mkdir(mode=0o700, exist_ok=True)
            if os.name != 'nt' and marker.parent.stat().st_mode & 0o077:
                raise OwnershipError('Native ownership directory permissions are invalid')
            if not marker.exists() and row is None and allow_create:
                # A crash leaves this marker invalid or unregistered, never an
                # invitation to unlink it and create another namespace/writer.
                fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
                with os.fdopen(fd, 'wb') as destination:
                    destination.write(json.dumps(expected, sort_keys=True, separators=(',', ':')).encode())
                    destination.flush()
                    os.fsync(destination.fileno())
            if self._read_marker(marker) != expected:
                raise OwnershipError('Native session belongs to another managed binding')
            marker_id = file_identity(marker)
            if row is not None and marker_id != row['marker_id']:
                raise OwnershipError('Native ownership metadata identity changed')
            self.ledger.register_session_binding(session_id, profile, observed_id, parent_id,
                                                 lease.identity, marker_id)
            lease.verify()
        except OSError:
            raise OwnershipError('Native ownership metadata cannot be established') from None

