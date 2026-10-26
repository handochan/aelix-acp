"""OS references, rather than parent PID or heartbeat, fence managed writers."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from aelix_acp.operations.lease import LeaseBusy, OwnershipError, WriterLease, directory_identity


class LeaseTests(unittest.TestCase):
    def test_an_actual_child_keeps_ownership_after_the_parent_reference_closes(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'native-writer.lock'
            ready = Path(temporary) / 'ready'
            lease = WriterLease(path).acquire()
            child = None
            try:
                with lease.inheritance() as (reference, options):
                    child = subprocess.Popen([sys.executable, '-c',
                        'import sys;from pathlib import Path;from aelix_acp.operations.lease import inherited_identity;'
                        'p=Path(sys.argv[2]);t=p.with_suffix(".new");'
                        't.write_text(inherited_identity(int(sys.argv[1])));t.replace(p);sys.stdin.readline()',
                        str(reference), str(ready)], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                        env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src')),
                        **options)
                deadline = time.monotonic() + 10
                while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists(), 'Child did not verify its inherited OS reference')
                self.assertEqual(ready.read_text(), lease.identity)
                lease.close()
                with self.assertRaises(LeaseBusy):
                    WriterLease(path).acquire()
                child.kill()  # OS-process interruption, not cooperative Python cleanup.
                child.wait(timeout=10)
                replacement = WriterLease(path).acquire()
                replacement.close()
            finally:
                lease.close()
                if child is not None:
                    if child.poll() is None:
                        child.kill()
                    child.wait(timeout=10)
                    child.stdin.close()

    def test_busy_or_repeated_acquisition_does_not_truncate_ownership_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'writer.lock'
            path.write_bytes(b'owned metadata')
            path.chmod(0o600)
            lease = WriterLease(path).acquire()
            try:
                with self.assertRaises(OwnershipError):
                    lease.acquire()
                with self.assertRaises(LeaseBusy):
                    WriterLease(path).acquire()
            finally:
                lease.close()
            self.assertEqual(path.read_bytes(), b'owned metadata')

    def test_different_native_sessions_are_independent(self):
        with tempfile.TemporaryDirectory() as temporary:
            leases = [WriterLease(Path(temporary) / name).acquire() for name in ('one', 'two')]
            try:
                self.assertNotEqual(leases[0].identity, leases[1].identity)
            finally:
                for lease in leases:
                    lease.close()

    def test_replaced_state_directory_has_a_different_physical_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'state'
            root.mkdir()
            identity = directory_identity(root)
            root.rename(root.with_name('retained-state'))
            root.mkdir()
            self.assertNotEqual(directory_identity(root), identity)

    def test_missing_directory_does_not_expose_the_operator_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            sentinel = 'SYNTHETIC_PRIVATE_ROOT_MUST_NOT_BE_RETURNED'
            with self.assertRaises(OwnershipError) as caught:
                directory_identity(Path(temporary) / sentinel)
            self.assertNotIn(sentinel, str(caught.exception))
            self.assertTrue(caught.exception.__suppress_context__)

    def test_lease_lookup_error_does_not_expose_raw_filesystem_details(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(Path, 'is_symlink', side_effect=PermissionError('SYNTHETIC_PRIVATE_PATH')):
                with self.assertRaises(OwnershipError) as caught:
                    WriterLease(Path(temporary) / 'writer').acquire()
            self.assertNotIn('SYNTHETIC_PRIVATE_PATH', str(caught.exception))
            self.assertTrue(caught.exception.__suppress_context__)

    @unittest.skipIf(os.name == 'nt', 'File symlink creation needs Windows developer/admin policy')
    def test_a_symlink_is_refused_instead_of_locking_a_substituted_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / 'target'
            target.touch(mode=0o600)
            link = root / 'lease'
            link.symlink_to(target)
            with self.assertRaises(OwnershipError):
                WriterLease(link).acquire()


if __name__ == '__main__':
    unittest.main()
