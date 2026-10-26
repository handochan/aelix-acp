"""Durable admission/outcome behavior; no native or host acceptance is implied."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from aelix_acp.operations.ledger import LedgerError, OperationContext, OperationLedger, SCHEMA
from aelix_acp.operations.lease import LeaseBusy, OwnershipError


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / 'state'
        self.ledger = OperationLedger(self.root)
        self.operation = OperationContext('operation-1', self.ledger.store_id, 'conversation-1', 'turn-1', 'generation-1')
        self.lease = self.ledger.acquire_writer('session-1')

    def tearDown(self):
        self.lease.close()
        self.ledger.close()
        self.temporary.cleanup()

    def claim(self, text='accepted prompt', *, ledger=None, operation=None, lease=None, owner='owner-1'):
        return (ledger or self.ledger).claim('session-1', 'coding', operation or self.operation,
                                              text, owner, self.lease if lease is None else lease)

    def test_claim_is_durable_before_dispatch_and_pending_duplicate_does_not_claim(self):
        self.assertTrue(self.claim()[0])
        other = OperationLedger(self.root, create=False)
        try:
            claimed, receipt = self.claim(ledger=other, owner='another-owner')
            self.assertFalse(claimed)
            self.assertEqual(receipt['state'], 'pending')
        finally:
            other.close()

    def test_completed_duplicate_survives_reconnect_and_keeps_original_execution_scope(self):
        self.claim()
        terminal = self.ledger.finish('session-1', self.operation.operation_id, 'owner-1', 'completed', 'end_turn')
        self.lease.close()
        self.ledger.close()
        self.ledger = OperationLedger(self.root, create=False)
        changed_generation = replace(self.operation, generation='new-generation', turn_id='another-turn')
        claimed, receipt = self.ledger.claim('session-1', 'coding', changed_generation,
                                             'accepted prompt', 'new-owner', None)
        self.assertFalse(claimed)
        self.assertEqual(receipt, dict(terminal, replayed=True))

    def test_same_key_with_a_different_accepted_prompt_is_a_conflict(self):
        self.claim()
        sentinel = 'SYNTHETIC_DIFFERENT_PROMPT_MUST_NOT_BE_IN_ERROR'
        with self.assertRaises(LedgerError) as caught:
            self.claim(sentinel)
        self.assertEqual(caught.exception.code, 'operation_conflict')
        self.assertNotIn(sentinel, str(caught.exception))

    def test_a_publication_failure_cannot_overwrite_an_already_committed_terminal(self):
        self.claim()
        completed = self.ledger.finish('session-1', self.operation.operation_id, 'owner-1', 'completed', 'end_turn')
        # The caller's error path after a failed stdout write must retain this.
        after_failure = self.ledger.finish('session-1', self.operation.operation_id,
                                           'owner-1', 'interrupted', 'transport_closed')
        self.assertEqual(after_failure, completed)

    def test_recovery_waits_for_the_original_os_lease_and_never_reclaims_the_operation(self):
        self.claim()
        with self.assertRaises(LeaseBusy):
            self.ledger.recover_abandoned('session-1', 'coding', self.operation)
        self.assertEqual(self.ledger.lookup('session-1', 'coding', self.operation)['state'], 'pending')
        self.lease.close()
        receipt = self.ledger.recover_abandoned('session-1', 'coding', self.operation)
        self.assertEqual((receipt['state'], receipt['effectsUnknown'], receipt['automaticRetry']),
                         ('interrupted', True, False))
        self.assertFalse(self.ledger.claim('session-1', 'coding', self.operation,
                                           'accepted prompt', 'new-owner', None)[0])

    def test_replaced_lease_file_does_not_grant_recovery(self):
        self.claim()
        path = self.lease.path
        self.lease.close()
        path.rename(path.with_suffix('.retained'))
        path.touch(mode=0o600)
        with self.assertRaises(LedgerError) as caught:
            self.ledger.recover_abandoned('session-1', 'coding', self.operation)
        self.assertEqual(caught.exception.code, 'ownership_changed')
        self.assertEqual(self.ledger.lookup('session-1', 'coding', self.operation)['state'], 'pending')

    def test_copied_state_directory_cannot_adopt_the_original_namespace(self):
        self.lease.close()
        self.ledger.close()
        copied = self.root.with_name('copied-state')
        shutil.copytree(self.root, copied)
        with self.assertRaises(LedgerError) as caught:
            OperationLedger(copied)
        self.assertEqual(caught.exception.code, 'store_changed')
        self.ledger = OperationLedger(self.root, create=False)

    def replace_live_root(self):
        retained = self.root.with_name('retained-state')
        try:
            self.root.rename(retained)
        except PermissionError:
            if sys.platform == 'win32':
                self.skipTest('Windows refuses moving the open SQLite/lease directory')
            raise
        self.root.mkdir()

    def test_live_instance_refuses_a_new_lease_after_root_replacement(self):
        self.replace_live_root()
        unexpected = None
        try:
            with self.assertRaises(LedgerError) as caught:
                unexpected = self.ledger.acquire_writer('session-1')
            self.assertEqual(caught.exception.code, 'store_changed')
        finally:
            if unexpected:
                unexpected.close()

    def test_live_instance_refuses_claim_with_its_old_reference_after_root_replacement(self):
        self.replace_live_root()
        with self.assertRaises(LedgerError) as caught:
            self.claim()
        self.assertEqual(caught.exception.code, 'store_changed')

    def test_corrupt_integer_fingerprint_is_not_used_as_an_allocation_size(self):
        self.claim()
        self.ledger.db.execute('PRAGMA ignore_check_constraints=ON')
        self.ledger.db.execute('UPDATE operations SET fingerprint=65536')
        with self.assertRaises(LedgerError) as caught:
            self.claim()
        self.assertEqual(caught.exception.code, 'store_unavailable')

    def test_live_lookup_refuses_root_replacement(self):
        self.claim()
        self.replace_live_root()
        with self.assertRaises(LedgerError) as caught:
            self.ledger.lookup('session-1', 'coding', self.operation)
        self.assertEqual(caught.exception.code, 'store_changed')

    def test_live_claim_refuses_replacing_the_database_file_in_the_same_root(self):
        path = self.root / 'operations.sqlite3'
        try:
            path.rename(path.with_suffix('.retained'))
        except PermissionError:
            if sys.platform == 'win32':
                self.skipTest('Windows refuses replacing the open SQLite file')
            raise
        path.touch(mode=0o600)
        with self.assertRaises(LedgerError) as caught:
            self.claim()
        self.assertEqual(caught.exception.code, 'store_changed')

    def test_reopening_a_copied_database_cannot_silently_restore_the_old_epoch(self):
        self.lease.close()
        self.ledger.close()
        path = self.root / 'operations.sqlite3'
        retained = path.with_suffix('.retained')
        path.rename(retained)
        shutil.copyfile(retained, path)
        path.chmod(0o600)
        try:
            with self.assertRaises(LedgerError) as caught:
                OperationLedger(self.root, create=False)
            self.assertEqual(caught.exception.code, 'store_changed')
        finally:
            path.unlink()
            retained.rename(path)
            self.ledger = OperationLedger(self.root, create=False)

    @unittest.skipIf(os.name == 'nt', 'POSIX permission boundary')
    def test_a_publicly_readable_hmac_database_is_refused(self):
        self.ledger.close()
        path = self.root / 'operations.sqlite3'
        path.chmod(0o644)
        try:
            with self.assertRaises(LedgerError) as caught:
                OperationLedger(self.root, create=False)
            self.assertEqual(caught.exception.code, 'store_unavailable')
        finally:
            path.chmod(0o600)
            self.ledger = OperationLedger(self.root, create=False)

    def test_recovery_does_not_recreate_a_missing_lease_file(self):
        self.claim()
        path = self.lease.path
        self.lease.close()
        path.unlink()
        with self.assertRaises(OwnershipError):
            self.ledger.recover_abandoned('session-1', 'coding', self.operation)
        self.assertFalse(path.exists())

    def test_a_held_reference_to_a_replaced_lease_path_cannot_claim_new_work(self):
        path = self.lease.path
        try:
            path.rename(path.with_suffix('.retained'))
        except PermissionError:
            if sys.platform == 'win32':
                self.skipTest('Windows nonshare lease excludes rename/delete')
            raise
        path.touch(mode=0o600)
        with self.assertRaises(LedgerError) as caught:
            self.claim()
        self.assertEqual(caught.exception.code, 'ownership_changed')

    def test_parallel_connections_atomically_claim_one_operation_under_one_managed_writer(self):
        barrier = threading.Barrier(2)
        def attempt(owner):
            ledger = OperationLedger(self.root, create=False)
            try:
                barrier.wait(timeout=10)
                return self.claim(ledger=ledger, owner=owner)[0]
            finally:
                ledger.close()
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(attempt, owner) for owner in ('owner-a', 'owner-b')]
            self.assertEqual(sorted(future.result(timeout=10) for future in futures), [False, True])

    def test_prompt_is_keyed_and_no_raw_prompt_is_written_to_the_store(self):
        sentinel = 'SYNTHETIC_PROMPT_PAYLOAD_MUST_NOT_BE_PERSISTED'
        self.claim(sentinel)
        digest = self.ledger.db.execute('SELECT fingerprint FROM operations').fetchone()[0]
        self.assertNotEqual(digest, hashlib.sha256(sentinel.encode()).digest())
        self.lease.close()
        self.ledger.close()
        self.assertTrue(all(sentinel.encode() not in path.read_bytes()
                            for path in self.root.rglob('*') if path.is_file()))
        self.ledger = OperationLedger(self.root, create=False)

    def test_unknown_control_metadata_and_changed_store_are_rejected_without_payload_echo(self):
        wire = dict(schema=SCHEMA, id='operation-1', storeId=self.operation.store_id,
                    conversationId='conversation-1', turnId='turn-1', generation='generation-1')
        with self.assertRaises(LedgerError) as caught:
            OperationContext.parse(dict(wire, auth='SYNTHETIC_AUTH_VALUE'))
        self.assertNotIn('SYNTHETIC_AUTH_VALUE', str(caught.exception))
        with self.assertRaises(LedgerError) as changed:
            self.ledger.lookup('session-1', 'coding', replace(self.operation, store_id='unknown-store'))
        self.assertEqual(changed.exception.code, 'store_changed')

    def test_missing_status_store_does_not_create_a_new_namespace(self):
        missing = self.root.with_name('missing-state')
        with self.assertRaises(LedgerError):
            OperationLedger(missing, create=False)
        self.assertFalse(missing.exists())

    def test_inconsistent_terminal_or_missing_writer_is_rejected_before_claim(self):
        with self.assertRaises(LedgerError):
            self.ledger.claim('session-1', 'coding', self.operation, 'prompt', 'owner-1', None)
        self.claim()
        with self.assertRaises(LedgerError):
            self.ledger.finish('session-1', self.operation.operation_id, 'owner-1', 'completed', 'native_error')
        self.assertEqual(self.ledger.lookup('session-1', 'coding', self.operation)['state'], 'pending')

    def test_corrupt_terminal_is_not_returned_as_a_success(self):
        self.claim()
        self.ledger.finish('session-1', self.operation.operation_id, 'owner-1', 'completed', 'end_turn')
        self.ledger.db.execute('UPDATE operations SET stop_reason=NULL')
        with self.assertRaises(LedgerError) as caught:
            self.ledger.lookup('session-1', 'coding', self.operation)
        self.assertEqual(caught.exception.code, 'store_unavailable')


if __name__ == '__main__':
    unittest.main()
