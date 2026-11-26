from contextlib import redirect_stderr
from io import StringIO
import json
import os
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from aelix_acp.runtime import session_lock
from aelix_acp.server import Server, main
from aelix_acp.wire.errors import ProtocolError, SESSION_LOCKED


class LaunchFlagTests(unittest.TestCase):
    def test_cli_rejects_both_reserved_flags_before_starting_the_adapter(self):
        for managed in ("0", "1"):
            for flag, value in ((session_lock.WRITER_FLAG, "studio"), (session_lock.ERROR_FLAG, "0" * 32)):
                for argument in (flag, f"{flag}={value}"):
                    with self.subTest(managed=managed, argument=argument):
                        stderr = StringIO()
                        argv = ["aelix-acp", f"--aelix-arg={argument}"]
                        with (patch.object(sys, "argv", argv), patch.dict(os.environ, AELIX_ACP_STUDIO_MANAGED=managed),
                              patch("aelix_acp.server.Server") as server, redirect_stderr(stderr),
                              self.assertRaises(SystemExit) as error):
                            main()
                        self.assertEqual(error.exception.code, 2)
                        self.assertIn("session writer attribution flags are reserved", stderr.getvalue())
                        server.assert_not_called()


class StartupLockTests(unittest.TestCase):
    nonce = "8a" * 16

    def test_requires_the_unique_final_correlated_record_and_clean_pipes(self):
        record = session_lock.STARTUP_PREFIX + json.dumps({
            "nonce": self.nonce, "kind": "session_locked", "holderKind": "cli",
        }).encode("ascii") + b"\n"
        diagnostic = b"Session is already open. " + b"x" * 20000 + b"\n"
        for stderr, returncode, stdout, eof, accepted in (
            (diagnostic + record, 1, False, True, True),
            (record + diagnostic + record, 1, False, True, False),
            (diagnostic + record.rstrip(b"\n"), 1, False, True, False),
            (diagnostic + record + b"\n", 1, False, True, False),
            (diagnostic + record.replace(self.nonce.encode(), b"00" * 16), 1, False, True, False),
            (diagnostic + record[:-1] + b" " * 256 + b"\n", 1, False, True, False),
            (diagnostic + record, 2, False, True, False),
            (diagnostic + record, 1, True, True, False),
            (diagnostic + record, 1, False, False, False),
        ):
            with self.subTest(returncode=returncode, stdout=stdout, eof=eof, size=len(stderr)):
                startup = session_lock.StartupLock(self.nonce)
                for offset in range(0, len(stderr), 13):
                    startup.read_stderr(stderr[offset:offset + 13])
                startup.stdout_eof = startup.stderr_eof = eof
                if stdout:
                    startup.saw_stdout()
                self.assertEqual(startup.refusal(returncode) is not None, accepted)

    def test_only_integer_one_offers_holder_details_and_unknown_kinds_stay_null(self):
        for value in (True, 1.0, "1", 2, None):
            self.assertIsNone(session_lock.holder({"holderKind": "cli"}, {"_meta": {session_lock.META: value}}))
        offer = {"_meta": {session_lock.META: 1}}
        self.assertEqual(session_lock.holder({"holderKind": "studio"}, offer), "studio")
        self.assertIsNone(session_lock.holder({"holderKind": "future"}, offer))


class BranchLockTests(unittest.IsolatedAsyncioTestCase):
    async def test_branch_refusal_keeps_only_a_settled_original_worker(self):
        for code, same_binding, close in (
            (SESSION_LOCKED, True, False),
            (SESSION_LOCKED, False, True),
            (-32007, True, True),
        ):
            with self.subTest(code=code, same_binding=same_binding):
                worker = SimpleNamespace()
                error = ProtocolError("branch did not complete", code)
                session = SimpleNamespace(
                    controls=SimpleNamespace(capabilities={"methods": ["sessions/branch"]}),
                    native_lock_errors=lambda: True,
                    proc=worker,
                    state={"sessionFile": "original.jsonl", "sessionId": "native-original"},
                    rpc=AsyncMock(side_effect=error),
                    verify_branch_binding=AsyncMock(return_value=same_binding),
                    close=AsyncMock(),
                )
                with self.assertRaises(ProtocolError) as refused:
                    await Server.branch_session(SimpleNamespace(args=SimpleNamespace()), session, {"kind": "clone"})
                self.assertIs(refused.exception, error)
                if code == SESSION_LOCKED:
                    session.verify_branch_binding.assert_awaited_once_with(worker, ("original.jsonl", "native-original"))
                else:
                    session.verify_branch_binding.assert_not_awaited()
                self.assertEqual(session.close.await_count, int(close))
