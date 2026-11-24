import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from aelix_acp import session
from aelix_acp.wire.errors import ProtocolError, SOCKET_PATH_TOO_LONG


class SessionTempDirTests(unittest.TestCase):
    def test_default_tempdir_at_socket_byte_limit(self):
        for platform, ascii_chars in (("darwin", 69), ("freebsd14", 69), ("linux", 73)):
            tmpdir = "/é" + "a" * ascii_chars
            with (self.subTest(platform=platform),
                  patch.object(session, "sys", SimpleNamespace(platform=platform)),
                  patch.object(session.tempfile, "gettempdir", return_value=tmpdir),
                  patch.object(session.tempfile, "mkdtemp", return_value="session-dir") as mkdtemp):
                self.assertEqual(session._make_session_dir(tcp=False), "session-dir")
                mkdtemp.assert_called_once_with(prefix="aelix-acp-", dir=tmpdir)

    @unittest.skipUnless(os.name == "posix", "POSIX short temp directory")
    def test_one_byte_over_socket_limit_falls_back_to_tmp(self):
        for platform, ascii_chars in (("darwin", 70), ("linux", 74)):
            tmpdir = "/é" + "a" * ascii_chars
            with (self.subTest(platform=platform),
                  patch.object(session, "sys", SimpleNamespace(platform=platform)),
                  patch.object(session.tempfile, "gettempdir", return_value=tmpdir),
                  patch.object(session.tempfile, "mkdtemp", return_value="session-dir") as mkdtemp):
                self.assertEqual(session._make_session_dir(tcp=False), "session-dir")
                mkdtemp.assert_called_once_with(prefix="aelix-acp-", dir="/tmp")

    def test_no_candidate_fits_reports_limit_without_creating_directory(self):
        tmpdir = "/private-tmpdir-" + "a" * 140
        with (patch.object(session, "sys", SimpleNamespace(platform="darwin")),
              patch.object(session, "_SHORT_TEMP_DIR", tmpdir),
              patch.object(session.tempfile, "gettempdir", return_value=tmpdir),
              patch.object(session.tempfile, "mkdtemp") as mkdtemp,
              self.assertRaisesRegex(ProtocolError, "104-byte AF_UNIX limit.*Set TMPDIR") as caught):
            session._make_session_dir(tcp=False)
        mkdtemp.assert_not_called()
        error = caught.exception
        self.assertEqual(error.code, SOCKET_PATH_TOO_LONG)
        self.assertEqual(error.data, {"kind": "socket_path_too_long", "limitBytes": 104, "retryable": False})
        self.assertNotIn(tmpdir, str(error))

    @unittest.skipUnless(os.name == "posix", "POSIX short temp directory")
    def test_unavailable_tmp_reports_socket_path_limit(self):
        tmpdir = "/" + "a" * 139
        with (patch.object(session, "sys", SimpleNamespace(platform="linux")),
              patch.object(session.tempfile, "gettempdir", return_value=tmpdir),
              patch.object(session.tempfile, "mkdtemp", side_effect=PermissionError("/tmp")) as mkdtemp,
              self.assertRaisesRegex(ProtocolError, "108-byte AF_UNIX limit.*Set TMPDIR") as caught):
            session._make_session_dir(tcp=False)
        mkdtemp.assert_called_once_with(prefix="aelix-acp-", dir="/tmp")
        self.assertEqual(caught.exception.data["kind"], "socket_path_too_long")

    @unittest.skipUnless(os.name == "posix", "POSIX short temp directory")
    def test_tempdir_lookup_failure_still_tries_tmp(self):
        with (patch.object(session.tempfile, "gettempdir", side_effect=FileNotFoundError("no usable temp directory")),
              patch.object(session.tempfile, "mkdtemp", return_value="session-dir") as mkdtemp):
            self.assertEqual(session._make_session_dir(tcp=False), "session-dir")
        mkdtemp.assert_called_once_with(prefix="aelix-acp-", dir="/tmp")

    @unittest.skipUnless(os.name == "posix", "POSIX short temp directory")
    def test_tempdir_lookup_and_fallback_failures_hide_paths(self):
        with (patch.object(session, "sys", SimpleNamespace(platform="darwin")),
              patch.object(session.tempfile, "gettempdir", side_effect=FileNotFoundError("/private/default")),
              patch.object(session.tempfile, "mkdtemp", side_effect=PermissionError("/private/fallback")) as mkdtemp,
              self.assertRaisesRegex(ProtocolError, "104-byte AF_UNIX limit.*Set TMPDIR") as caught):
            session._make_session_dir(tcp=False)
        mkdtemp.assert_called_once_with(prefix="aelix-acp-", dir="/tmp")
        error = caught.exception
        self.assertEqual(error.code, SOCKET_PATH_TOO_LONG)
        self.assertEqual(error.data, {"kind": "socket_path_too_long", "limitBytes": 104, "retryable": False})
        self.assertNotIn("/private/", str(error))

    def test_tcp_uses_default_tempdir_without_socket_path_check(self):
        with (patch.object(session.tempfile, "gettempdir") as gettempdir,
              patch.object(session.tempfile, "mkdtemp", return_value="session-dir") as mkdtemp):
            self.assertEqual(session._make_session_dir(tcp=True), "session-dir")
        gettempdir.assert_not_called()
        mkdtemp.assert_called_once_with(prefix="aelix-acp-")
