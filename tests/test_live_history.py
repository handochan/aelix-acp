import asyncio
from contextlib import closing, redirect_stderr
import io
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from aelix_acp.runtime.leases import acquire
from aelix_acp.runtime.store import LiveHistory, history, write_store
from aelix_acp.server import Server, Session


def chunk(text):
    return {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}


class LiveHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.server = Server(SimpleNamespace(state_dir=directory, profile="coding"))
        self.addCleanup(self.server.db.close)
        self.session = Session(self.server, "test-session", directory)
        self.recording = self.session.live_history
        self.addAsyncCleanup(self.recording.flush)

    def recorded(self):
        return list(history(self.server.db, self.session.id))

    def blocker(self):
        filename = Path(self.server.args.state_dir) / "sessions.sqlite3"
        db = self.enterContext(closing(sqlite3.connect(filename, timeout=0)))
        db.execute("BEGIN IMMEDIATE")
        return db

    async def test_close_waits_for_the_accepted_prefix_before_releasing_the_lease(self):
        self.session.lease = acquire(self.server.args.state_dir, self.session.id)
        self.server.sessions[self.session.id] = self.session
        before = chunk("before lock")
        self.recording.append(before)
        await self.recording.flush()
        blocked = self.blocker()
        closing_session = asyncio.create_task(self.session.close())
        first, last = chunk("first in backlog"), chunk("last in backlog")
        self.recording.append(first)
        self.recording.append(last)
        async with asyncio.timeout(5):
            try:
                while not self.session.ending:
                    await asyncio.sleep(0)
                self.assertEqual(self.recorded(), [before])
                self.assertFalse(closing_session.done())
                self.assertIsNotNone(self.session.lease)
            finally:
                blocked.rollback()
                await closing_session
        self.assertEqual(self.recorded(), [before, first, last])
        self.assertIsNone(self.session.lease)
        self.assertNotIn(self.session.id, self.server.sessions)

    async def test_backlog_limit_counts_records_and_utf8_bytes_and_keeps_a_prefix(self):
        first, second, later = chunk("먼저"), chunk("second"), chunk("later")
        size = len(json.dumps(first, ensure_ascii=False).encode("utf-8"))
        for limits in ({"max_events": 1}, {"max_bytes": size}):
            with self.subTest(limits=limits):
                recording = LiveHistory(self.server.args.state_dir, "bounded", **limits)
                with redirect_stderr(io.StringIO()) as diagnostics:
                    recording.append(first)
                    recording.append(second)
                    recording.append(later)
                    await recording.flush()
                self.assertEqual(diagnostics.getvalue(), "history_backlog_overflow\n")
                self.assertEqual(recording.failure, "history_backlog_overflow")
        self.assertEqual(list(history(self.server.db, "bounded")), [first, first])

    async def test_failed_batch_rolls_back_and_emits_a_fixed_code(self):
        first, bad, skipped = chunk("durable"), chunk("private-path"), chunk("suffix")
        self.recording.append(first)
        await self.recording.flush()
        insert = self.recording.insert_batch

        def fail_mid_batch(db, rows):
            insert(db, rows[:1])
            raise sqlite3.OperationalError("private-path")

        with patch.object(self.recording, "insert_batch", fail_mid_batch), redirect_stderr(io.StringIO()) as diagnostics:
            self.recording.append(bad)
            self.recording.append(skipped)
            await self.recording.flush()
            self.recording.append(skipped)
        self.assertEqual(diagnostics.getvalue(), "history_write_failed\n")
        self.assertEqual(self.recorded(), [first])

    async def test_repeated_cancellation_waits_for_the_started_write(self):
        released = Event()
        entered = asyncio.Event()
        loop = asyncio.get_running_loop()

        def gated_write(db):
            loop.call_soon_threadsafe(entered.set)
            if not released.wait(5):
                raise AssertionError("SQLite test writer was never released")
            self.recording.insert_batch(db, [(self.session.id, json.dumps(chunk("durable")).encode("utf-8"))])

        write = asyncio.create_task(write_store(self.server.args.state_dir, gated_write))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            for _ in range(2):
                write.cancel()
                await asyncio.sleep(0)
                self.assertFalse(write.done())
        finally:
            released.set()
        with self.assertRaises(asyncio.CancelledError):
            await write
        self.assertEqual(self.recorded(), [chunk("durable")])
