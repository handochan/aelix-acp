"""Regression checks for receipts that must reject metadata-only compatibility."""
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from beta2_identity import check_wheel, compare_payload
from beta2_wire import isolated_environment
from qualify_beta2 import native_help


class WheelIdentityTests(unittest.TestCase):
    def test_help_cannot_read_the_calling_projects_dotenv(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project = root / "project"
            project.mkdir()
            (project / ".env").write_text("SYNTHETIC_PRIVATE_SENTINEL")
            executable = root / "help-probe"
            executable.write_text("#!/usr/bin/env python3\nfrom pathlib import Path\n"
                                  "print(Path('.env').read_text() if Path('.env').exists() else 'ISOLATED_HELP')\n")
            executable.chmod(0o700)
            previous = Path.cwd()
            try:
                os.chdir(project)
                self.assertEqual(native_help([sys.executable, str(executable)]).strip(), "ISOLATED_HELP")
            finally:
                os.chdir(previous)

    def test_children_do_not_inherit_user_provider_keys_or_pythonpath(self):
        with patch.dict(os.environ, {"PATH": "/fixture/bin", "ANTHROPIC_API_KEY": "sentinel",
                                     "OPENAI_API_KEY": "sentinel", "PYTHONPATH": "/private/source",
                                     "AELIX_AUTH_PATH": "/private/auth"}, clear=True):
            env = isolated_environment(Path("/fixture/root"))
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("PYTHONPATH", env)
        self.assertEqual(env["AELIX_AUTH_PATH"], str(Path('/fixture/root') / 'agent/auth.json'))

    def test_same_version_patched_payload_does_not_qualify(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wheel = root / "fixture.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("native.py", "PUBLIC_RELEASE = True\n")
                archive.writestr("native.dist-info/METADATA", "Version: 0.1.0b2\n")
                archive.writestr("native.dist-info/RECORD", "installer rewrites this\n")
            installed = root / "site-packages"
            (installed / "native.dist-info").mkdir(parents=True)
            (installed / "native.py").write_bytes(b"PUBLIC_RELEASE = True\n")
            (installed / "native.dist-info/METADATA").write_bytes(b"Version: 0.1.0b2\n")
            self.assertEqual(compare_payload(wheel, installed), 2)
            (installed / "native.py").write_text("PRIVATE_CORE_PATCH = True\n")
            with self.assertRaisesRegex(ValueError, "Installed wheel member differs"):
                compare_payload(wheel, installed)

    def test_download_must_match_digest_and_size(self):
        with tempfile.TemporaryDirectory() as directory:
            wheel = Path(directory) / "fixture.whl"
            wheel.write_bytes(b"public artifact")
            spec = {"distribution": "fixture", "size": len(wheel.read_bytes()),
                    "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()}
            check_wheel(wheel, spec)
            wheel.write_bytes(b"private artifact")
            with self.assertRaisesRegex(ValueError, "digest/size mismatch"):
                check_wheel(wheel, spec)

    def test_missing_member_is_not_accepted_as_an_installed_wheel(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wheel = root / "fixture.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("native.py", "required")
            with self.assertRaisesRegex(ValueError, "Installed wheel member differs"):
                compare_payload(wheel, root / "empty")

    def test_wheel_paths_cannot_escape_the_install(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wheel = root / "fixture.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("../outside.py", "outside")
            with self.assertRaisesRegex(ValueError, "Unexpected wheel member"):
                compare_payload(wheel, root / "site-packages")
