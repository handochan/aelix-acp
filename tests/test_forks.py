import contextlib
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from aelix_acp.runtime import forks
from aelix_acp.runtime.leases import acquire, acquire_file
from aelix_acp.runtime.store import open_store
from aelix_acp.wire.errors import ProtocolError


class PendingForkTests(unittest.TestCase):
    def test_recovery_waits_for_both_owners_and_reclaims_only_the_reserved_files(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.closing(open_store(directory)) as db:
            parent = Path(directory) / "parent.jsonl"
            parent.write_text("original", encoding="utf-8")
            with acquire(directory, "child"):
                draft = forks.reserve(db, directory, "child", "parent", parent)
                target = Path(draft["target"])
                staged = Path(draft["target"] + ".tmp")
                target.write_text(json.dumps({"type": "session", "id": draft["nativeId"]}), encoding="utf-8")
                Path(draft["receipt"]).write_text(hashlib.sha256(target.read_bytes()).hexdigest(), encoding="ascii")
                staged.write_text("interrupted write", encoding="utf-8")
                forks.recover(db, directory)
                self.assertTrue(target.is_file())
            with acquire_file(Path(str(target) + ".lock")):
                with self.assertRaisesRegex(ProtocolError, "needs cleanup"):
                    forks.recover(db, directory, "parent")
                self.assertTrue(target.is_file())
            forks.recover(db, directory)
            self.assertEqual(parent.read_text(encoding="utf-8"), "original")
            self.assertFalse(target.exists())
            self.assertFalse(staged.exists())
            self.assertIsNone(db.execute("SELECT 1 FROM pending_forks").fetchone())

    def test_recovery_keeps_a_published_fork_with_changed_or_damaged_contents(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.closing(open_store(directory)) as db:
            parent = Path(directory) / "parent.jsonl"
            parent.touch()
            with acquire(directory, "child"):
                draft = forks.reserve(db, directory, "child", "parent", parent)
                target = Path(draft["target"])
                header = json.dumps({"type": "session", "id": draft["nativeId"]}).encode() + b"\n"
                Path(draft["receipt"]).write_text(hashlib.sha256(header).hexdigest(), encoding="ascii")
                updated = header + b'{"type":"message","message":"new turn"}\n'
            for contents in (updated, b"[]\n"):
                with self.subTest(contents=contents):
                    target.write_bytes(contents)
                    forks.recover(db, directory)
                    with self.assertRaisesRegex(ProtocolError, "needs cleanup"):
                        forks.recover(db, directory, "parent")
                    self.assertEqual(target.read_bytes(), contents)
