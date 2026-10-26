"""Durable, payload-free records for negotiated managed prompt operations.

Claim before native dispatch; commit a bounded terminal before publishing it.
An interrupted claim is never an instruction to execute that operation again.
"""
import contextlib
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import stat
import time
import uuid

from .lease import OwnershipError, WriterLease, directory_identity, file_identity, inherited_identity, local_path


SCHEMA = 'aelix.operation/1'
META_KEY = 'aelix/operation'
TOKEN = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z')
TERMINALS = {'completed', 'cancelled', 'failed', 'interrupted'}
CODES = {'end_turn', 'cancelled', 'native_error', 'timeout', 'transport_closed', 'owner_lost', 'dispatch_failed'}
TERMINAL_CODES = {
    'completed': {'end_turn'},
    'cancelled': {'cancelled'},
    'failed': {'native_error', 'timeout', 'transport_closed', 'dispatch_failed'},
    'interrupted': {'owner_lost', 'timeout', 'transport_closed', 'dispatch_failed'},
}


class LedgerError(Exception):
    """Only fixed public messages/codes; never raw SQLite or native errors."""
    def __init__(self, code):
        self.code = code
        super().__init__({
            'invalid_operation': 'Invalid managed operation metadata',
            'operation_conflict': 'Operation identity conflicts with the accepted request',
            'store_changed': 'Managed operation store identity changed',
            'store_unavailable': 'Managed operation store is unavailable',
            'ownership_changed': 'Managed writer ownership cannot be verified',
        }[code])


def token(value):
    if not isinstance(value, str) or TOKEN.fullmatch(value) is None:
        raise LedgerError('invalid_operation')
    return value


@dataclass(frozen=True)
class OperationContext:
    operation_id: str
    store_id: str
    conversation_id: str
    turn_id: str
    generation: str

    @classmethod
    def parse(cls, value):
        keys = {'schema', 'id', 'storeId', 'conversationId', 'turnId', 'generation'}
        if (not isinstance(value, dict) or len(value) != len(keys)
                or set(value) != keys or value.get('schema') != SCHEMA):
            raise LedgerError('invalid_operation')
        return cls(*(token(value[key]) for key in ('id', 'storeId', 'conversationId', 'turnId', 'generation')))


class OperationLedger:
    """One SQLite namespace/key epoch; no startup sweep, reset, or auto-pruning.

    This class does not establish native/session binding by itself. The managed
    session must bind its native identity to this exact private lease before
    dispatch. Recovery always opens a fresh lease, never a parent's old reference.
    """

    def __init__(self, state_dir, *, create=True):
        self.db = None
        try:
            self.root = local_path(state_dir)
            if create:
                self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.root = self.root.resolve(strict=True)
            if os.name != 'nt' and self.root.stat().st_mode & 0o077:
                raise LedgerError('store_unavailable')
            root_id = directory_identity(self.root)
            self._root_id = root_id
            path = self.root / 'operations.sqlite3'
            self._path = path
            if path.is_symlink():
                raise LedgerError('store_unavailable')
            created = False
            if create:
                try:
                    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                except FileExistsError:
                    pass
                else:
                    os.close(fd)
                    created = True
            elif not path.is_file():
                raise LedgerError('store_unavailable')
            value = path.stat(follow_symlinks=False)
            if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1 or (os.name != 'nt' and value.st_mode & 0o077):
                raise LedgerError('store_unavailable')
            self.db = sqlite3.connect(path, timeout=0.25, isolation_level=None)
            self._file_id = file_identity(path)
            # No row stores prompt/output bodies; cap malformed local records
            # before SQLite/Python can materialize oversized values.
            self.db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 8192)
            self.db.row_factory = sqlite3.Row
            self.db.execute('PRAGMA busy_timeout=250')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA secure_delete=ON')
            if created:
                with self.transaction():
                    self.db.execute('CREATE TABLE operation_meta (singleton INTEGER PRIMARY KEY CHECK(singleton=1), '
                                    'schema_version INTEGER NOT NULL, store_id TEXT NOT NULL, '
                                    'fingerprint_key BLOB NOT NULL, root_id TEXT NOT NULL, file_id TEXT NOT NULL)')
                    self.db.execute('INSERT INTO operation_meta VALUES(1,1,?,?,?,?)',
                                    (uuid.uuid4().hex, secrets.token_bytes(32), root_id, self._file_id))
                    self.db.execute('CREATE TABLE operations (session_id TEXT NOT NULL, operation_id TEXT NOT NULL, '
                                    'profile TEXT NOT NULL, conversation_id TEXT NOT NULL, '
                                    "fingerprint BLOB NOT NULL CHECK(typeof(fingerprint)='blob' AND length(fingerprint)=32), "
                                    'turn_id TEXT NOT NULL, generation TEXT NOT NULL, owner TEXT NOT NULL, '
                                    'lease_id TEXT NOT NULL, state TEXT NOT NULL, code TEXT, stop_reason TEXT, '
                                    'created INTEGER NOT NULL, updated INTEGER NOT NULL, '
                                    'PRIMARY KEY(session_id,operation_id))')
                    self.db.execute('CREATE TABLE managed_sessions (session_id TEXT PRIMARY KEY, profile TEXT NOT NULL, '
                                    'native_id TEXT NOT NULL, native_parent_id TEXT NOT NULL, lease_id TEXT NOT NULL, '
                                    'marker_id TEXT NOT NULL, UNIQUE(native_id,native_parent_id))')
            row = self.db.execute('SELECT schema_version,store_id,fingerprint_key,root_id,file_id FROM operation_meta '
                                  'WHERE singleton=1').fetchone()
            if (not row or row['schema_version'] != 1 or row['root_id'] != root_id or row['file_id'] != self._file_id
                    or not isinstance(row['fingerprint_key'], bytes) or len(row['fingerprint_key']) != 32
                    or not isinstance(row['store_id'], str) or not re.fullmatch('[0-9a-f]{32}', row['store_id'])):
                raise LedgerError('store_changed')
            self.store_id, self._key = row['store_id'], row['fingerprint_key']
        except (OSError, RuntimeError, ValueError, sqlite3.Error, OwnershipError):
            self.close()
            raise LedgerError('store_unavailable') from None
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None

    @contextlib.contextmanager
    def transaction(self):
        try:
            self._validate_storage_identity()
            self.db.execute('BEGIN IMMEDIATE')
            yield
            self._validate_storage_identity()
            self.db.execute('COMMIT')
        except BaseException as error:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            if isinstance(error, sqlite3.Error):
                raise LedgerError('store_unavailable') from None
            raise

    def _validate_storage_identity(self):
        try:
            if directory_identity(self.root) != self._root_id or file_identity(self._path) != self._file_id:
                raise LedgerError('store_changed')
        except OwnershipError:
            raise LedgerError('store_unavailable') from None

    def lease_path(self, session_id):
        session_id = token(session_id)
        return self.root / 'writer-leases' / (hashlib.sha256(session_id.encode()).hexdigest() + '.lock')

    def acquire_writer(self, session_id, *, create=True):
        self._validate_storage_identity()
        path = self.lease_path(session_id)
        lease = None
        try:
            if path.parent.is_symlink():
                raise OwnershipError('Private writer directory is invalid')
            if create:
                path.parent.mkdir(mode=0o700, exist_ok=True)
            elif not path.parent.is_dir():
                raise OwnershipError('Private writer directory is unavailable')
            if os.name != 'nt' and path.parent.stat().st_mode & 0o077:
                raise OwnershipError('Private writer directory permissions are invalid')
            lease = WriterLease(path).acquire(create=create)
            self._validate_storage_identity()
            return lease
        except BaseException as error:
            if lease is not None:
                lease.close()
            if isinstance(error, OSError):
                raise OwnershipError('Private writer directory is unavailable') from None
            raise

    def session_binding(self, session_id):
        self._validate_storage_identity()
        try:
            return self.db.execute('SELECT * FROM managed_sessions WHERE session_id=?',
                                   (token(session_id),)).fetchone()
        except sqlite3.Error:
            raise LedgerError('store_unavailable') from None

    def register_session_binding(self, session_id, profile, native_id, parent_id, lease_id, marker_id):
        values = tuple(token(value) for value in (session_id, profile, native_id, parent_id, lease_id, marker_id))
        with self.transaction():
            self.db.execute('INSERT OR IGNORE INTO managed_sessions VALUES(?,?,?,?,?,?)', values)
            stored = self.session_binding(session_id)
            if stored is None or tuple(stored) != values:
                raise LedgerError('ownership_changed')

    def recover_session_pending(self, session_id, lease):
        """Recover only the selected session before its NEW native bootstrap."""
        if (not isinstance(lease, WriterLease) or lease.child_started
                or lease.path != self.lease_path(session_id)):
            raise LedgerError('ownership_changed')
        lease.verify()
        with self.transaction():
            rows = self.db.execute('SELECT lease_id FROM operations WHERE session_id=? AND state=?',
                                   (token(session_id), 'pending')).fetchall()
            if any(row['lease_id'] != lease.identity for row in rows):
                raise LedgerError('ownership_changed')
            self.db.execute('UPDATE operations SET state=?,code=?,updated=? WHERE session_id=? AND state=?',
                            ('interrupted', 'owner_lost', time.time_ns() // 1_000_000, session_id, 'pending'))

    def _validate_context(self, operation):
        if not isinstance(operation, OperationContext):
            raise LedgerError('invalid_operation')
        for value in (operation.operation_id, operation.store_id, operation.conversation_id,
                      operation.turn_id, operation.generation):
            token(value)
        if not hmac.compare_digest(operation.store_id, self.store_id):
            raise LedgerError('store_changed')

    def fingerprint(self, session_id, profile, operation, message):
        self._validate_context(operation)
        token(session_id)
        token(profile)
        if not isinstance(message, str):
            raise LedgerError('invalid_operation')
        # Execution fences stay with the ORIGINAL claim. A new connection or
        # generation cannot turn a completed/uncertain operation into a new one.
        try:
            content = json.dumps([SCHEMA, session_id, profile, operation.conversation_id, message],
                                 ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        except UnicodeError:
            raise LedgerError('invalid_operation') from None
        return hmac.digest(self._key, content, 'sha256')

    def _row(self, session_id, operation_id):
        try:
            self._validate_storage_identity()
            row = self.db.execute('SELECT * FROM operations WHERE session_id=? AND operation_id=?',
                                  (token(session_id), token(operation_id))).fetchone()
            if row is not None and (not isinstance(row['fingerprint'], bytes) or len(row['fingerprint']) != 32):
                raise LedgerError('store_unavailable')
            return row
        except sqlite3.Error:
            raise LedgerError('store_unavailable') from None

    def _receipt(self, row, *, replayed=False):
        if (row['state'] not in TERMINALS | {'pending'}
                or row['code'] not in CODES | {None}
                or row['stop_reason'] not in {'end_turn', 'cancelled', None}):
            raise LedgerError('store_unavailable')
        if row['state'] == 'pending':
            if row['code'] is not None or row['stop_reason'] is not None:
                raise LedgerError('store_unavailable')
        elif row['code'] not in TERMINAL_CODES[row['state']]:
            raise LedgerError('store_unavailable')
        else:
            expected_stop = ('end_turn' if row['state'] == 'completed'
                             else 'cancelled' if row['state'] == 'cancelled' else None)
            if row['stop_reason'] != expected_stop:
                raise LedgerError('store_unavailable')
        for field in ('operation_id', 'conversation_id', 'turn_id', 'generation'):
            token(row[field])
        result = dict(schema=SCHEMA, id=row['operation_id'], storeId=self.store_id,
                      conversationId=row['conversation_id'], turnId=row['turn_id'],
                      generation=row['generation'], state=row['state'], code=row['code'],
                      effectsUnknown=row['state'] != 'completed', automaticRetry=False, replayed=replayed)
        if row['stop_reason'] is not None:
            result['stopReason'] = row['stop_reason']
        return result

    def lookup(self, session_id, profile, operation):
        self._validate_context(operation)
        row = self._row(session_id, operation.operation_id)
        if row is None:
            return None
        if row['profile'] != profile or row['conversation_id'] != operation.conversation_id:
            raise LedgerError('operation_conflict')
        return self._receipt(row)

    def claim(self, session_id, profile, operation, message, owner, lease):
        """Return (newly claimed, receipt); known IDs never dispatch again."""
        owner = token(owner)
        fingerprint = self.fingerprint(session_id, profile, operation, message)
        with self.transaction():
            row = self._row(session_id, operation.operation_id)
            if row is not None:
                if not hmac.compare_digest(row['fingerprint'], fingerprint):
                    raise LedgerError('operation_conflict')
                return False, self._receipt(row, replayed=row['state'] in TERMINALS)
            try:
                if (not isinstance(lease, WriterLease) or lease.reference is None
                        or lease.path != self.lease_path(session_id)
                        or inherited_identity(lease.reference) != lease.identity):
                    raise OwnershipError('Private writer reference cannot be verified')
                lease.verify()
            except OwnershipError:
                raise LedgerError('ownership_changed') from None
            now = time.time_ns() // 1_000_000
            self.db.execute('INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                            (session_id, operation.operation_id, profile, operation.conversation_id,
                             fingerprint, operation.turn_id, operation.generation, owner,
                             lease.identity, 'pending', None, None, now, now))
            return True, self._receipt(self._row(session_id, operation.operation_id))

    def finish(self, session_id, operation_id, owner, state, code):
        """Store only allowlisted terminal data; publication happens afterwards."""
        if (not isinstance(state, str) or not isinstance(code, str)
                or state not in TERMINALS or code not in TERMINAL_CODES[state]):
            raise LedgerError('invalid_operation')
        stop_reason = 'end_turn' if state == 'completed' else 'cancelled' if state == 'cancelled' else None
        with self.transaction():
            row = self._row(session_id, operation_id)
            if row is None or row['owner'] != token(owner):
                raise LedgerError('ownership_changed')
            # Never overwrite a committed terminal because stdout publication or
            # subsequent cleanup failed; a late old owner sees recovery's result.
            if row['state'] in TERMINALS:
                return self._receipt(row)
            self.db.execute('UPDATE operations SET state=?,code=?,stop_reason=?,updated=? '
                            'WHERE session_id=? AND operation_id=? AND owner=? AND state=?',
                            (state, code, stop_reason, time.time_ns() // 1_000_000,
                             session_id, operation_id, owner, 'pending'))
            return self._receipt(self._row(session_id, operation_id))

    def recover_abandoned(self, session_id, profile, operation, *, code='owner_lost'):
        """Settle pending only behind a fresh native-lifetime lease barrier.

        LeaseBusy means pending/recovery waiting, never a settled interruption.
        This method does not start a native worker or automatically replay work.
        """
        if code not in TERMINAL_CODES['interrupted']:
            raise LedgerError('invalid_operation')
        receipt = self.lookup(session_id, profile, operation)
        if receipt is None or receipt['state'] in TERMINALS:
            return receipt
        lease = self.acquire_writer(session_id, create=False)
        try:
            with self.transaction():
                row = self._row(session_id, operation.operation_id)
                if row['lease_id'] != lease.identity:
                    raise LedgerError('ownership_changed')
                if row['state'] == 'pending':
                    self.db.execute('UPDATE operations SET state=?,code=?,updated=? '
                                    'WHERE session_id=? AND operation_id=? AND state=?',
                                    ('interrupted', code, time.time_ns() // 1_000_000,
                                     session_id, operation.operation_id, 'pending'))
                return self._receipt(self._row(session_id, operation.operation_id))
        finally:
            lease.close()
