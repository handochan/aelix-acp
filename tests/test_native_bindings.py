"""Private binding filesystem boundaries; native behavior uses separate probes."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from aelix_acp.operations.bindings import NativeBindings
from aelix_acp.operations.ledger import OperationLedger
from aelix_acp.operations.lease import OwnershipError


class NativeBindingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.file = self.root / 'native/session.jsonl'
        self.file.parent.mkdir()
        self.file.write_text(json.dumps({'type': 'session', 'id': 'native-id-1',
                                        'cwd': 'SYNTHETIC_PRIVATE_PATH', 'version': 3}) + '\n')
        self.original = self.file.read_bytes()
        self.ledger = OperationLedger(self.root / 'state')
        self.bindings = NativeBindings(self.ledger)
        self.lease = self.bindings.prepare_writer('session-1', 'coding')

    def tearDown(self):
        self.lease.close()
        self.ledger.close()
        self.temporary.cleanup()

    def bind(self):
        self.bindings.bind_native('session-1', 'coding', self.file, self.lease,
                                 native_id='native-id-1', allow_create=True)
        return self.bindings.native_identity(self.file)[2]

    def test_binding_preserves_native_bytes_and_does_not_copy_header_payload(self):
        marker = self.bind()
        self.assertEqual(self.file.read_bytes(), self.original)
        self.assertNotIn(b'SYNTHETIC_PRIVATE_PATH', marker.read_bytes())
        self.assertEqual(self.ledger.session_binding('session-1')['native_id'], 'native-id-1')

    def test_a_different_state_namespace_cannot_adopt_the_same_native_file(self):
        self.bind()
        other = OperationLedger(self.root / 'other-state')
        try:
            with self.assertRaises(OwnershipError):
                NativeBindings(other).prepare_writer('other-session', 'coding', registered_file=self.file)
            self.assertFalse((other.root / 'writer-leases').exists())
        finally:
            other.close()

    def test_another_acp_id_cannot_become_a_second_writer_for_the_same_native_identity(self):
        self.bind()
        with self.assertRaises(OwnershipError):
            self.bindings.prepare_writer('different-acp-session', 'coding', registered_file=self.file)

    def test_registered_resume_reuses_the_original_lease_after_its_owner_exits(self):
        self.bind()
        original_id = self.lease.identity
        self.lease.close()
        self.lease = self.bindings.prepare_writer('session-1', 'coding', registered_file=self.file)
        self.assertEqual(self.lease.identity, original_id)
        self.assertEqual(self.file.read_bytes(), self.original)

    def test_missing_marker_cannot_silently_reenroll_a_managed_session(self):
        marker = self.bind()
        self.lease.close()
        marker.unlink()
        with self.assertRaises(OwnershipError):
            self.bindings.prepare_writer('session-1', 'coding', registered_file=self.file)
        self.assertFalse(marker.exists())

    def test_a_copied_marker_with_equal_bytes_does_not_replace_the_bound_file(self):
        marker = self.bind()
        self.lease.close()
        retained = marker.with_suffix('.retained')
        marker.rename(retained)
        shutil.copyfile(retained, marker)
        marker.chmod(0o600)
        with self.assertRaises(OwnershipError):
            self.bindings.prepare_writer('session-1', 'coding', registered_file=self.file)

    def test_same_header_copy_cannot_replace_the_bound_native_file(self):
        self.bind()
        self.lease.close()
        retained = self.file.with_suffix('.retained')
        self.file.rename(retained)
        shutil.copyfile(retained, self.file)
        with self.assertRaises(OwnershipError):
            self.bindings.prepare_writer('session-1', 'coding', registered_file=self.file)

    def test_partial_marker_is_not_deleted_or_recreated(self):
        marker = self.bind()
        self.lease.close()
        marker.write_bytes(b'{')
        with self.assertRaises(OwnershipError):
            self.bindings.prepare_writer('session-1', 'coding', registered_file=self.file)
        self.assertEqual(marker.read_bytes(), b'{')

    def test_hard_linked_native_aliases_are_refused_instead_of_claiming_a_canonical_path(self):
        os.link(self.file, self.file.with_name('alias.jsonl'))
        with self.assertRaises(OwnershipError):
            self.bindings.bind_native('session-1', 'coding', self.file, self.lease, allow_create=True)


if __name__ == '__main__':
    unittest.main()
