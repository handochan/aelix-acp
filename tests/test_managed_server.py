"""Server admission/publication boundaries; actual native wire is separate."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from aelix_acp.server import ProtocolError, Server, Session
from aelix_acp.operations.ledger import LedgerError, META_KEY, SCHEMA
from aelix_acp.operations.bindings import NativeBindings
from aelix_acp.operations.lease import OwnershipError


class ManagedServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.args = SimpleNamespace(state_dir=self.root / 'state', profile='coding', managed_operations=True)
        self.server = Server(self.args)
        self.session = Session(self.server, 'session-1', str(self.root))
        self.native = self.root / 'native.jsonl'
        self.native.write_text(json.dumps(dict(type='session', id='native-1')) + '\n')
        self.session.state = dict(sessionFile=str(self.native), sessionId='native-1')
        self.session.writer_lease = self.server.bindings.prepare_writer(self.session.id, 'coding')
        self.server.bindings.bind_native(self.session.id, 'coding', self.native, self.session.writer_lease,
                                         native_id='native-1', allow_create=True)
        self.session.save()
        self.server.sessions[self.session.id] = self.session
        self.dispatches = 0
        self.entered, self.release = asyncio.Event(), asyncio.Event()

        async def prompt(blocks, *, accepted_message=None):
            self.session.require_prompt_slot()
            self.session.prompt_active = True
            self.dispatches += 1
            self.entered.set()
            try:
                await self.release.wait()
                return dict(stopReason='end_turn')
            finally:
                self.session.prompt_active = False

        self.session.prompt = prompt
        self.operation = dict(schema=SCHEMA, id='operation-1', storeId=self.server.operations.store_id,
                              conversationId='conversation-1', turnId='turn-1', generation=self.session.worker_generation)
        self.params = dict(sessionId=self.session.id, prompt=[dict(type='text', text='SYNTHETIC_BODY')],
                           _meta={META_KEY: self.operation})

    async def asyncTearDown(self):
        await self.session.close()
        self.server.db.close()
        self.server.operations.close()
        self.temporary.cleanup()

    async def test_duplicate_is_pending_before_busy_and_never_dispatches_twice(self):
        task = asyncio.create_task(self.server.prompt(self.params))
        await self.entered.wait()
        with self.assertRaises(ProtocolError) as pending:
            await self.server.prompt(self.params)
        self.assertEqual(pending.exception.code, -32045)
        self.assertEqual(pending.exception.data[META_KEY]['state'], 'pending')
        self.assertEqual(self.dispatches, 1)
        changed = dict(self.params, prompt=[dict(type='text', text='DIFFERENT')])
        with self.assertRaises(LedgerError) as conflict:
            await self.server.prompt(changed)
        self.assertEqual(conflict.exception.code, 'operation_conflict')
        self.release.set()
        await task

    async def test_distinct_busy_operation_is_not_claimed(self):
        task = asyncio.create_task(self.server.prompt(self.params))
        await self.entered.wait()
        other = dict(self.operation, id='other-operation')
        with self.assertRaises(ProtocolError):
            await self.server.prompt(dict(self.params, _meta={META_KEY: other}))
        self.assertEqual(self.server.operation_status(dict(sessionId=self.session.id, operation=other))['operation']['state'], 'not_found')
        self.release.set()
        await task

    async def test_stale_generation_is_rejected_before_claim(self):
        other = dict(self.operation, generation='stale')
        with self.assertRaises(ProtocolError) as failure:
            await self.server.prompt(dict(self.params, _meta={META_KEY: other}))
        self.assertEqual(failure.exception.code, -32044)
        self.assertEqual(self.dispatches, 0)

    async def test_malformed_empty_metadata_never_falls_back_to_unkeyed_execution(self):
        for meta in ([], '', False, None):
            with self.subTest(meta=meta), self.assertRaises(ProtocolError):
                await self.server.prompt(dict(self.params, _meta=meta))
        self.assertEqual(self.dispatches, 0)

    async def test_agent_end_with_a_still_streaming_native_snapshot_is_not_a_terminal(self):
        async def rpc(command, **kwargs):
            if command == 'prompt':
                self.session.done.set_result(None)
                return {}
            self.assertEqual(command, 'get_state')
            return dict(isStreaming=True, isCompacting=False)

        self.session.rpc = rpc
        with self.assertRaisesRegex(ProtocolError, 'terminal state'):
            await Session.prompt(self.session, self.params['prompt'])

    async def test_completed_duplicate_without_loaded_worker_preserves_original_fences(self):
        self.release.set()
        original = await self.server.prompt(self.params)
        self.server.sessions.clear()
        replay = await self.server.prompt(dict(self.params, _meta={META_KEY: dict(self.operation, generation='later', turnId='later-turn')}))
        self.assertEqual(replay['_meta'][META_KEY]['turnId'], 'turn-1')
        self.assertEqual(original['stopReason'], replay['stopReason'])
        self.assertEqual(self.dispatches, 1)

    async def test_failed_stdout_publication_preserves_the_committed_terminal(self):
        self.release.set()

        async def publication_failure(message):
            raise BrokenPipeError('SYNTHETIC_PRIVATE_DIAGNOSTIC')

        self.server.send = publication_failure
        with self.assertRaises(BrokenPipeError):
            await self.server.handle(dict(jsonrpc='2.0', id='request-1', method='session/prompt', params=self.params))
        status = self.server.operation_status(dict(sessionId=self.session.id, operation=self.operation))
        self.assertEqual(status['operation']['state'], 'completed')
        self.assertEqual(self.dispatches, 1)

    async def test_stale_scoped_cancel_cannot_cancel_current_operation(self):
        task = asyncio.create_task(self.server.prompt(self.params))
        await self.entered.wait()
        result = await self.server.dispatch('session/cancel', dict(sessionId=self.session.id,
            _meta={META_KEY: dict(self.operation, turnId='stale-turn')}))
        self.assertEqual(result, dict(cancelled=False, reason='stale_operation'))
        self.assertTrue(self.session.prompt_active)
        self.release.set()
        await task

    async def test_flag_off_refuses_a_persisted_managed_mapping(self):
        legacy = Server(SimpleNamespace(state_dir=self.args.state_dir, profile='coding'))
        try:
            with self.assertRaises(ProtocolError):
                legacy.validate_session_store(self.session.id)
            with self.assertRaises(OwnershipError):
                NativeBindings.require_unmanaged(self.native)
        finally:
            legacy.db.close()

    async def test_flag_off_new_cannot_select_an_owned_history_through_launch_prefix(self):
        legacy = Server(SimpleNamespace(state_dir=self.args.state_dir, profile='coding',
                                        aelix_arg=['--session', str(self.native)]))
        try:
            with self.assertRaisesRegex(ProtocolError, 'session selection'):
                await Session(legacy, 'different-session', str(self.root)).start()
        finally:
            legacy.db.close()

    async def test_missing_managed_database_is_not_recreated(self):
        # Model a partial state loss with the session mapping still present.
        self.server.operations.close()
        database = self.args.state_dir / 'operations.sqlite3'
        database.rename(database.with_suffix('.retained'))
        with self.assertRaises(LedgerError):
            Server(self.args)
        self.assertFalse(database.exists())


if __name__ == '__main__':
    unittest.main()
