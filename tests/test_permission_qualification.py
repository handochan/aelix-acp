"""A local patched host must never be accepted as released baseline bytes."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from qualify_permissions import host_identity


class HostIdentityTests(unittest.TestCase):
    def test_arbitrary_host_requires_explicit_candidate_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary) / 'aioncore'
            binary.write_bytes(b'synthetic candidate')
            with self.assertRaises(AssertionError):
                host_identity(binary)
            with self.assertRaisesRegex(ValueError, 'both'):
                host_identity(binary, Path(temporary) / 'receipt.json')

    def test_candidate_receipt_binds_binary_and_adjacent_reviewed_patch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary, source, patch, receipt_path = (root / name for name in ('aioncore', 'source.json', 'recovery.patch', 'receipt.json'))
            binary.write_bytes(b'synthetic candidate')
            patch.write_text('synthetic patch')
            digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
            manifest = {'schema': 'aelix-studio.host-recovery-source/1',
                        'patch': {'filename': patch.name, 'sha256': digest(patch)},
                        'upstream': {'commit': '47e66d0d151123e973b3fd1e77afcb5671b3f8c5'},
                        'cargoLockSha256': 'synthetic-lock', 'upstreamLicenseSha256': 'synthetic-license'}
            source.write_text(json.dumps(manifest))
            receipt = {'schema': 'aelix-studio.host-recovery-build/1', 'status': 'built_candidate',
                       'sourceManifestSha256': digest(source), 'patchSha256': digest(patch),
                       'upstream': manifest['upstream'], 'cargoLockSha256': 'synthetic-lock',
                       'licenseFileSha256': 'synthetic-license', 'releasedHostBytes': False,
                       'nativePatches': [], 'binarySha256': digest(binary)}
            receipt_path.write_text(json.dumps(receipt))
            self.assertEqual(host_identity(binary, receipt_path, source)['kind'], 'source_built_recovery_candidate')
            binary.write_bytes(b'changed candidate')
            with self.assertRaises(AssertionError):
                host_identity(binary, receipt_path, source)
            binary.write_bytes(b'synthetic candidate')
            patch.write_text('unreviewed changed patch')
            with self.assertRaises(AssertionError):
                host_identity(binary, receipt_path, source)
