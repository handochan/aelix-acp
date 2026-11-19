"""The private startup guard can only protect its owning session once."""
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from uuid import uuid4


@unittest.skipUnless(os.environ.get("AELIX_ACP_TEST_AELIX"), "Requires the pinned native workspace")
class SelectorStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_wrong_nonce_session_storage_and_replay(self):
        from aelix_agent_core.session.fs import LocalFileSystem
        from aelix_agent_core.session.jsonl_storage import JsonlSessionStorage
        from aelix_agent_core.session.read_only import ReadOnlySessionStorage
        from aelix_agent_core.session.session import Session
        from aelix_agent_core.session.storage import SessionError
        from aelix_acp.bridge import register_selector_startup

        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory).resolve() / "session.jsonl"
            fs = LocalFileSystem()
            original = await JsonlSessionStorage.create(fs, str(filename), cwd=directory, session_id=str(uuid4()))
            other_file = filename.with_name("other.jsonl")
            other = Session(await JsonlSessionStorage.create(
                fs, str(other_file), cwd=directory, session_id=str(uuid4())))
            session = Session(original)
            read_only = Session(ReadOnlySessionStorage(original))
            current = session
            ctx = SimpleNamespace(session_manager=SimpleNamespace(get_session=lambda: current))
            handlers = {}
            bridge = SimpleNamespace(register_command=lambda name, **kwargs: handlers.update({name: kwargs["handler"]}))
            register_selector_startup(bridge, str(filename), "fixture-nonce")
            command = handlers["aelix-acp-selector-startup"]

            for arguments in ("begin wrong", "finish fixture-nonce"):
                with self.assertRaisesRegex(ValueError, "one-time authorization"):
                    await command(arguments, ctx)
            self.assertIs(session.get_storage(), original)
            current = other
            with self.assertRaisesRegex(ValueError, "bound native session"):
                await command("begin fixture-nonce", ctx)
            current = read_only
            with self.assertRaisesRegex(ValueError, "writable native JSONL"):
                await command("begin fixture-nonce", ctx)
            self.assertIsInstance(read_only.get_storage(), ReadOnlySessionStorage)

            current = session
            before = filename.read_bytes()
            self.assertEqual(await command("begin fixture-nonce", ctx), "selector-startup-ok")
            with self.assertRaises(SessionError) as error:
                await session.get_storage().set_leaf_id(None)
            self.assertEqual(error.exception.code, "read_only")
            with self.assertRaises(SessionError) as error:
                await session.append_custom_entry("guarded-write")
            self.assertEqual(error.exception.code, "read_only")
            await session.append_model_change("fixture", "two")
            await session.append_thinking_level_change("high")
            self.assertTrue(filename.read_bytes() == before)
            current = read_only
            with self.assertRaisesRegex(ValueError, "changed during startup"):
                await command("finish fixture-nonce", ctx)
            self.assertIsInstance(session.get_storage(), ReadOnlySessionStorage)
            current = session
            self.assertEqual(await command("finish fixture-nonce", ctx), "selector-startup-ok")
            self.assertIs(session.get_storage(), original)
            with self.assertRaisesRegex(ValueError, "one-time authorization"):
                await command("begin fixture-nonce", ctx)
            await session.append_thinking_level_change("medium")
            self.assertTrue(filename.read_bytes() != before)
