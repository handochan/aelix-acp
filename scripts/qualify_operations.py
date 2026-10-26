"""Installed ACP + unchanged public native operation qualification.

Synthetic loopback provider and temporary credentials/files only. This does not
qualify host/UI/installers, legacy CLI writer exclusion, power loss, or exactly
once external effects. Process interruption uses actual owned OS processes.
"""
import argparse
import asyncio
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import signal
import sys
import tempfile

from beta2_identity import verify
from qualify_beta2 import adapter_receipt
from qualify_permissions import PermissionPeer, ToolFixture

ROOT = Path(__file__).resolve().parents[1]
META = 'aelix/operation'


async def probe():
    checks = []
    with tempfile.TemporaryDirectory(prefix='acp-managed-operations-') as temporary:
        root = Path(temporary).resolve()
        fixture = ToolFixture(root)
        await fixture.start()
        # Same installed public entry point, with explicit interpreter routing
        # like Studio's packaged launch.py. The actual native PID is observable
        # without exposing one through the product protocol.
        wrapper = root / 'native-entry.py'
        starts = root / 'native-starts'
        wrapper.write_text('import os\nfrom pathlib import Path\n'
            f'with Path({str(starts)!r}).open("a") as f:f.write(str(os.getpid())+"\\n")\n'
            'from aelix_coding_agent.cli.entry import main_sync\nmain_sync()\n', encoding='utf-8')
        program = [sys.executable, '-I', '-B', '-c', 'from aelix_acp.server import main;main()',
            '--aelix', sys.executable, '--aelix-arg=-I', '--aelix-arg=' + str(wrapper),
            '--state-dir', root / 'state', '--provider', 'fixture', '--model', 'one']
        peers = []

        async def connect(status_only=False, legacy=False, extra=()):
            flags = [] if legacy else ['--operations-status-only' if status_only else '--managed-operations']
            peer = await PermissionPeer.start([*program, *flags, *extra], root / 'workspace', fixture.env, acp=True)
            peers.append(peer)
            init = await peer.request('initialize', protocolVersion=1)
            assert 'result' in init
            return peer, init['result']['agentCapabilities']

        async def status(peer, sid, op):
            return (await peer.request('_aelix/operation_status', sessionId=sid, operation=op))['result']['operation']

        async def opened(peer, cap):
            result = (await peer.request('session/new', cwd=str(root / 'workspace'), mcpServers=[],
                _meta={'aelix/operationStore': {'schema': cap['schema'], 'storeId': cap['storeId']}}))['result']
            scope = result['_meta']['aelix/operationStore']
            return result['sessionId'], dict(schema=cap['schema'], id='operation-1', storeId=cap['storeId'],
                conversationId='conversation-1', turnId='turn-1', generation=scope['workerGeneration'])

        def params(sid, op, text):
            return dict(sessionId=sid, prompt=[dict(type='text', text=text)], _meta={META: op})

        def launches():
            return starts.read_text().splitlines() if starts.exists() else []

        try:
            peer, capabilities = await connect()
            cap = capabilities['_meta']['aelix/operations']
            assert cap['schema'] == 'aelix.operation/1' and cap['statusMethod'] == '_aelix/operation_status'
            sid, op = await opened(peer, cap)
            checks += ['negotiated_exact_capability', 'actual_native_inherited_lease_and_binding']
            target = root / 'workspace/effect.txt'
            fixture.next_tool = ('write', dict(path=str(target), content='synthetic'))
            peer.policy = 'allow'
            accepted = params(sid, op, 'SYNTHETIC_MANAGED_EFFECT')
            reply = await peer.request('session/prompt', **accepted)
            assert reply['result']['stopReason'] == 'end_turn' and target.read_text() == 'synthetic', reply
            original_mtime, request_count = target.stat().st_mtime_ns, len(fixture.requests)
            checks.append('actual_native_model_and_tool_effect_terminal_committed')
            replay = await peer.request('session/prompt', **accepted)
            assert replay['result']['_meta'][META]['replayed'] is True, replay
            assert len(fixture.requests) == request_count and target.stat().st_mtime_ns == original_mtime
            checks.append('completed_duplicate_no_model_or_tool_reexecution')
            conflict = await peer.request('session/prompt', **params(sid, op, 'DIFFERENT'))
            assert conflict['error']['code'] == -32041
            checks.append('same_key_different_accepted_body_conflicts')

            # Keep the original managed native alive while trying the reviewed
            # flag-off session/new + explicit history-prefix bypass.
            import sqlite3
            database = sqlite3.connect(root / 'state/sessions.sqlite3')
            try:
                native_file = database.execute('SELECT file FROM sessions WHERE id=?', (sid,)).fetchone()[0]
            finally:
                database.close()
            before_starts = launches()
            bypass, _ = await connect(legacy=True, extra=['--aelix-arg=--session', '--aelix-arg=' + native_file])
            denied = await bypass.request('session/new', cwd=str(root / 'workspace'), mcpServers=[])
            assert denied['error']['code'] == -32602 and launches() == before_starts
            await bypass.close()
            checks.append('flag_off_new_history_prefix_refused_before_native_start')

            fixture.entered.clear()
            hold_op = dict(op, id='hold-operation', turnId='turn-2')
            held = asyncio.create_task(peer.request('session/prompt', **params(sid, hold_op, 'QUALIFICATION_HOLD')))
            await asyncio.wait_for(fixture.entered.wait(), 15)
            hold_count = len(fixture.requests)
            duplicate = await peer.request('session/prompt', **params(sid, hold_op, 'QUALIFICATION_HOLD'))
            assert duplicate['error']['code'] == -32045 and len(fixture.requests) == hold_count
            assert (await status(peer, sid, hold_op))['state'] == 'pending'
            busy_op = dict(op, id='busy-operation', turnId='turn-3')
            busy = await peer.request('session/prompt', **params(sid, busy_op, 'SYNTHETIC_BUSY'))
            assert busy['error']['code'] == -32602
            assert (await status(peer, sid, busy_op))['state'] == 'not_found'
            stale = await peer.request('session/cancel', sessionId=sid, _meta={META: dict(hold_op, turnId='stale')})
            assert stale['result'] == dict(cancelled=False, reason='stale_operation') and not held.done()
            checks += ['pending_duplicate_before_busy_no_reexecution', 'different_busy_key_not_claimed', 'stale_scoped_cancel_noop']
            await peer.request('session/cancel', sessionId=sid, _meta={META: hold_op})
            terminal = (await held)['result']['_meta'][META]
            assert terminal['state'] == 'cancelled' and terminal['effectsUnknown'] is True
            checks.append('native_cancel_terminal_keeps_effect_uncertainty')
            fixture.release.set()
            async with asyncio.timeout(10):
                while fixture.handlers:
                    await asyncio.sleep(.01)

            # Pending consent must be denied before native abort; never infer
            # absence of arbitrary external effects merely from cancel ack.
            consent_op = dict(op, id='consent-operation', turnId='turn-4')
            consent_target = root / 'workspace/consent-effect.txt'
            fixture.next_tool = ('write', dict(path=str(consent_target), content='synthetic'))
            peer.policy = 'hold'
            peer.permission_entered.clear()
            consent = asyncio.create_task(peer.request('session/prompt', **params(sid, consent_op, 'SYNTHETIC_CONSENT')))
            await asyncio.wait_for(peer.permission_entered.wait(), 15)
            await peer.request('session/cancel', sessionId=sid, _meta={META: consent_op})
            assert (await consent)['result']['_meta'][META]['state'] == 'cancelled'
            assert not consent_target.exists()
            checks.append('actual_pending_consent_cancel_denies_test_write')
            await peer.close()

            launches_before = launches()
            reader, caps = await connect(status_only=True)
            assert caps['loadSession'] is False
            assert (await status(reader, sid, op))['state'] == 'completed'
            assert (await reader.request('session/load', sessionId=sid, cwd=str(root / 'workspace')))['error']['code'] == -32601
            assert launches() == launches_before
            checks.append('restarted_status_only_no_native_start_or_load')
            legacy, _ = await connect(legacy=True)
            assert (await legacy.request('session/load', sessionId=sid, cwd=str(root / 'workspace')))['error']['code'] == -32042
            assert launches() == launches_before
            checks.append('flag_off_cannot_reopen_persisted_managed_history')
            await legacy.close()

            # A real hard parent kill, unlike EOF/finally, may leave a native
            # writer alive on POSIX. The inherited lease must remain exclusive.
            owner, _ = await connect()
            killed_sid, killed_op = await opened(owner, cap)
            killed_op['id'] = 'owner-kill-operation'
            fixture.entered.clear()
            fixture.release.clear()
            interrupted = asyncio.create_task(owner.request('session/prompt', **params(killed_sid, killed_op, 'QUALIFICATION_HOLD')))
            await asyncio.wait_for(fixture.entered.wait(), 15)
            native_pid = int(launches()[-1])
            owner.process.kill()
            await asyncio.wait_for(owner.process.wait(), 10)
            await asyncio.gather(interrupted, return_exceptions=True)
            observed = await status(reader, killed_sid, killed_op)
            if os.name != 'nt':
                assert observed['state'] == 'pending' and observed['executionObservation'] == 'recovery_waiting', observed
                os.kill(native_pid, signal.SIGKILL)
                checks.append('actual_native_lease_outlives_hard_parent_kill')
            # Windows kill-on-close Job may settle the native lifetime before
            # the first lookup; poll only the already owned operation barrier.
            async with asyncio.timeout(10):
                while observed['state'] == 'pending':
                    await asyncio.sleep(.02)
                    observed = await status(reader, killed_sid, killed_op)
            assert observed['state'] == 'interrupted' and observed['effectsUnknown'] and observed['automaticRetry'] is False
            checks.append('actual_owner_exit_fresh_barrier_settles_interrupted')
            fixture.release.set()
            fresh, _ = await connect()
            before_count, before_starts = len(fixture.requests), launches()
            refused = await fresh.request('session/prompt', **params(killed_sid, killed_op, 'QUALIFICATION_HOLD'))
            assert refused['error']['code'] == -32046
            assert len(fixture.requests) == before_count and launches() == before_starts
            checks.append('interrupted_duplicate_never_automatically_replays')

            # EOF exercises graceful adapter shutdown separately from hard kill.
            eof_sid, eof_op = await opened(fresh, cap)
            eof_op['id'] = 'eof-operation'
            fixture.entered.clear()
            fixture.release.clear()
            pending = asyncio.create_task(fresh.request('session/prompt', **params(eof_sid, eof_op, 'QUALIFICATION_HOLD')))
            await asyncio.wait_for(fixture.entered.wait(), 15)
            fresh.process.stdin.close()
            await asyncio.wait_for(fresh.process.wait(), 15)
            await asyncio.gather(pending, return_exceptions=True)
            eof_status = await status(reader, eof_sid, eof_op)
            assert eof_status['state'] == 'interrupted' and eof_status['automaticRetry'] is False
            checks.append('actual_stdin_eof_stops_owned_worker_without_replay')
            fixture.release.set()

            # Only allowlisted receipts/digests, no body/credential/tool content.
            for candidate in (root / 'state').glob('operations.sqlite3*'):
                data = candidate.read_bytes()
                for secret in (b'synthetic-fixture-key', b'SYNTHETIC_MANAGED_EFFECT', b'QUALIFICATION_HOLD'):
                    assert secret not in data
            checks.append('ledger_and_wal_exclude_test_credentials_and_prompt_bodies')
            return dict(checksPassed=checks, providerRequests=len(fixture.requests), nativeLaunches=len(launches()),
                        parentKillObservation=observed['state'], nativeLaunch='installed public main_sync via explicit interpreter')
        finally:
            fixture.release.set()
            for peer in peers:
                await peer.close()
            await fixture.close()


def main():
    if not __debug__:
        raise RuntimeError('Qualification assertions require Python without -O')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--adapter', required=True, type=Path)
    parser.add_argument('--artifacts', type=Path, default=ROOT / '.local/beta2-wheels')
    parser.add_argument('--report', required=True, type=Path)
    args = parser.parse_args()
    if Path(sys.prefix).resolve() != args.adapter.absolute().parent.parent.resolve():
        raise ValueError('Run qualification with the adapter installed-wheel environment Python')
    manifest = json.loads((ROOT / 'docs/beta2/manifest.json').read_text())
    report = dict(schemaVersion=1, status='failed', scope='installed ACP + actual public native; synthetic provider; no host/UI/installer acceptance',
                  platform=platform.system() + '/' + platform.machine(), python=platform.python_version(), nativePatches=[],
                  recordedAt=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  harnessSha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    try:
        report['identityBefore'] = verify(manifest, args.artifacts)
        report['adapterBefore'] = adapter_receipt(args.adapter.resolve())
        report['operations'] = asyncio.run(probe())
        report['adapterAfter'] = adapter_receipt(args.adapter.resolve())
        report['identityAfter'] = verify(manifest, args.artifacts)
        assert report['identityBefore'] == report['identityAfter'] and report['adapterBefore'] == report['adapterAfter']
        report['status'] = 'passed'
    finally:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(dict(status=report['status'], checks=len(report['operations']['checksPassed']), report=str(args.report))))


if __name__ == '__main__':
    main()
