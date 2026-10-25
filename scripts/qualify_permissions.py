"""Public beta.2 permission effects and installed ACP/actual host qualification.

All tool effects and credentials are synthetic in temporary roots. Extra native
modes are audited without advertising them as production ACP modes.
"""
import argparse
import asyncio
import contextlib
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile

from beta2_identity import verify
from beta2_wire import Fixture, Peer, native_data
from qualify_beta2 import adapter_receipt
from qualify_management import http, HOST_DIGEST

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent


class ToolFixture(Fixture):
    next_tool = None

    async def respond(self, reader, writer):
        task = asyncio.current_task()
        self.handlers.add(task)
        try:
            header = (await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)).decode()
            length = int(next(line.split(":", 1)[1] for line in header.split("\r\n") if line.lower().startswith("content-length:")))
            request = json.loads(await reader.readexactly(length))
            self.requests.append(request)
            if "QUALIFICATION_HOLD" in json.dumps(request["messages"][-1]):
                self.entered.set()
                await self.release.wait()
            tool, self.next_tool = self.next_tool, None
            if tool:
                name, args = tool
                delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": "synthetic-call-" + str(len(self.requests)),
                    "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}
                finish = "tool_calls"
            else:
                delta, finish = {"role": "assistant", "content": "PERMISSION_OK"}, "stop"
            chunks = [{"id": "fixture", "object": "chat.completion.chunk", "created": 0, "model": "one",
                       "choices": [{"index": 0, "delta": d, "finish_reason": f}]} for d, f in ((delta, None), ({}, finish))]
            body = ("".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n").encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\nContent-Length: "
                         + str(len(body)).encode() + b"\r\n\r\n" + body)
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
            self.handlers.discard(task)


class PermissionPeer(Peer):
    def __init__(self, process, acp=False):
        self.policy, self.approvals = "reject", []
        self.permission_entered = asyncio.Event()
        super().__init__(process, acp)

    async def read(self):
        try:
            while line := await self.process.stdout.readline():
                frame = json.loads(line)
                if frame.get("method") == "session/request_permission":
                    self.approvals.append(frame)
                    self.permission_entered.set()
                    if self.policy != "hold":
                        await self.send({"jsonrpc": "2.0", "id": frame["id"], "result": {
                            "outcome": {"outcome": "selected", "optionId": "allow-once" if self.policy == "allow" else "reject-once"}}})
                elif (future := self.pending.get(frame.get("id"))) is not None:
                    if not future.done(): future.set_result(frame)
                else:
                    self.events.append(frame)
                    self.changed.set()
        finally:
            for future in self.pending.values():
                if not future.done(): future.set_exception(RuntimeError("Permission probe protocol closed"))


def wrapper(root, native):
    path = root / "native-wrapper"
    extension = SCRIPTS / "permission_fixture_extension.py"
    path.write_text(f"#!{sys.executable}\nimport os,sys,time\nfrom pathlib import Path\nroot=Path({str(root)!r})\n"
                    "if (root/'fail-start').exists(): sys.exit(73)\n"
                    "if (root/'hold-start').exists():\n (root/'startup-entered').touch()\n time.sleep(30)\n"
                    f"args=[{str(native)!r},*sys.argv[1:],'-e',{str(extension)!r}]\n"
                    "if os.environ.get('PERMISSION_PROBE_TOOLS'): args+=['--tools',os.environ['PERMISSION_PROBE_TOOLS']]\n"
                    f"os.execv({str(native)!r},args)\n")
    path.chmod(0o700)
    return path


async def native_audit(native):
    results = []
    for mode in ("default", "yolo", "plan", "auto-accept-edits", "auto"):
        with tempfile.TemporaryDirectory(prefix="permission-native-") as temporary:
            root = Path(temporary).resolve()
            fixture = ToolFixture(root)
            await fixture.start()
            env = dict(fixture.env, PERMISSION_PROBE_ROOT=str(root))
            peer = await Peer.start([native, "--mode", "rpc", "--offline", "--no-extensions", "--no-agents", "--no-skills",
                "--no-context-files", "--permission-mode", mode, "-e", SCRIPTS / "permission_fixture_extension.py",
                "--provider", "fixture", "--model", "one"], root / "workspace", env)
            cases = {}
            try:
                native_data(await peer.request("get_state"))
                native_data(await peer.request("set_auto_retry", enabled=False))
                for case in ("inside_write", "outside_write", "shell_redirect", "unknown_extension_mutation"):
                    target = root / ("workspace/" if case != "outside_write" else "") / (case + ".txt")
                    if case == "shell_redirect": tool = ("bash", {"command": "printf synthetic > " + str(target)})
                    elif case == "unknown_extension_mutation": tool = ("probe_mutator", {"path": str(target)})
                    else: tool = ("write", {"path": str(target), "content": "synthetic"})
                    fixture.next_tool = tool
                    start = len(peer.events)
                    native_data(await peer.request("prompt", message="SYNTHETIC_NATIVE_PERMISSION"))
                    await peer.until(lambda: any(e.get("type") == "agent_end" for e in peer.events[start:]))
                    async with asyncio.timeout(10):
                        while native_data(await peer.request("get_state")).get("isStreaming"):
                            await asyncio.sleep(.02)
                    expected = mode != "plan" or case == "unknown_extension_mutation"
                    assert target.exists() == expected, (mode, case, target.exists())
                    cases[case] = {"fileEffectObserved": target.exists()}
                results.append({"mode": mode, "cases": cases, "scope": "stock native RPC without ACP approval bridge"})
            finally:
                await peer.close()
                await fixture.close()
    return results


async def adapter_probe(adapter, native):
    with tempfile.TemporaryDirectory(prefix="permission-acp-") as temporary:
        root = Path(temporary).resolve()
        fixture = ToolFixture(root)
        await fixture.start()
        env = dict(fixture.env, PERMISSION_PROBE_ROOT=str(root))
        launch = wrapper(root, native)
        argv = [adapter, "--aelix", launch, "--provider", "fixture", "--model", "one", "--state-dir", root / "acp"]
        peer = await PermissionPeer.start(argv, root / "workspace", env, acp=True)
        checks = []
        try:
            await peer.request("initialize", protocolVersion=1, clientCapabilities={})
            created = (await peer.request("session/new", cwd=str(root / "workspace"), mcpServers=[]))["result"]
            sid = created["sessionId"]
            assert {m["id"] for m in created["modes"]["availableModes"]} == {"default", "yolo"}
            assert created["modes"]["currentModeId"] == "default"
            checks.append("standard_modes_default_to_ask")
            async def effect(case, exists, policy="reject", tool="write", args=None):
                target = root / "workspace" / (case + ".txt")
                before = len(peer.approvals)
                peer.policy = policy
                fixture.next_tool = (tool, args or {"path": str(target), "content": "synthetic"})
                reply = await peer.request("session/prompt", sessionId=sid, prompt=[{"type": "text", "text": "SYNTHETIC_PERMISSION_TOOL"}])
                assert reply["result"]["stopReason"] == "end_turn", reply
                assert target.exists() == exists, case
                return len(peer.approvals) - before
            assert await effect("ask-deny", False) == 1
            assert await effect("ask-allow", True, "allow") == 1
            checks.append("ask_actual_deny_and_allow_effects")
            peer.policy = "hold"
            peer.permission_entered.clear()
            cancelled_path = root / "workspace/cancelled.txt"
            fixture.next_tool = ("write", {"path": str(cancelled_path), "content": "synthetic"})
            pending = asyncio.create_task(peer.request("session/prompt", sessionId=sid, prompt=[{"type": "text", "text": "SYNTHETIC_PERMISSION_HOLD"}]))
            await asyncio.wait_for(peer.permission_entered.wait(), 15)
            assert (await peer.request("session/set_mode", sessionId=sid, modeId="yolo"))["error"]["code"] == -32000
            assert (await peer.request("session/load", sessionId=sid, cwd=str(root / "workspace"), mcpServers=[]))["error"]["code"] == -32000
            await peer.request("session/cancel", sessionId=sid)
            assert (await pending)["result"]["stopReason"] == "cancelled" and not cancelled_path.exists()
            checks.append("busy_mode_and_load_refused_consent_cancel_no_write")
            assert "result" in await peer.request("session/set_mode", sessionId=sid, modeId="yolo")
            assert await effect("yolo-write", True) == 0
            assert await effect("yolo-custom", True, tool="probe_mutator", args={"path": str(root / "workspace/yolo-custom.txt")}) == 0
            checks.append("yolo_tools_run_without_client_prompt")
            pid_file, stopped_file = root / "workspace/shell.pid", root / "workspace/late-write.txt"
            fixture.next_tool = ("bash", {"command": f"echo $$ > {pid_file}; sleep 30; printf synthetic > {stopped_file}"})
            running = asyncio.create_task(peer.request("session/prompt", sessionId=sid, prompt=[{"type": "text", "text": "SYNTHETIC_YOLO_STOP"}]))
            async with asyncio.timeout(15):
                while not pid_file.exists(): await asyncio.sleep(.02)
            shell_pid = int(pid_file.read_text())
            assert (await peer.request("session/set_mode", sessionId=sid, modeId="default"))["error"]["code"] == -32000
            await peer.request("session/cancel", sessionId=sid)
            assert (await running)["result"]["stopReason"] == "cancelled"
            async with asyncio.timeout(5):
                while True:
                    try: os.kill(shell_pid, 0)
                    except ProcessLookupError: break
                    await asyncio.sleep(.02)
            assert not stopped_file.exists()
            checks.append("yolo_actual_shell_stop_reaps_process_without_late_write")
            await effect("guard", False, args={"path": str(root / "workspace/.env"), "content": "synthetic"})
            assert not (root / "workspace/.env").exists()
            (root / "deny-write").touch()
            assert await effect("extension-deny", False) == 0
            (root / "deny-write").unlink()
            checks.append("native_guardrail_and_other_extension_deny_remain")
            await effect("drop", False, tool="probe_drop_bridge", args={"unused": "synthetic"})
            assert await effect("channel-failure", False) == 0
            checks.append("yolo_bridge_channel_fault_blocks_effect")
            assert "result" in await peer.request("session/set_mode", sessionId=sid, modeId="default")
            assert await effect("ask-after-restart", False) == 1
            assert "result" in await peer.request("session/set_mode", sessionId=sid, modeId="yolo")
            await peer.close()
            peer = await PermissionPeer.start(argv, root / "workspace", env, acp=True)
            await peer.request("initialize", protocolVersion=1, clientCapabilities={})
            loaded = (await peer.request("session/load", sessionId=sid, cwd=str(root / "workspace"), mcpServers=[]))["result"]
            assert loaded["modes"]["currentModeId"] == "yolo"
            assert any(e.get("params", {}).get("update", {}).get("sessionUpdate") == "user_message_chunk" for e in peer.events)
            assert await effect("yolo-restored", True) == 0
            assert "result" in await peer.request("session/set_mode", sessionId=sid, modeId="default")
            checks.append("same_identity_history_and_saved_mode_restore")
            assert "result" in await peer.request("session/set_mode", sessionId=sid, modeId="yolo")
            (root / "fail-start").touch()
            failed = await peer.request("session/set_mode", sessionId=sid, modeId="default")
            assert failed["error"]["code"] == -32603 and "saved mode is Ask" in failed["error"]["message"]
            assert failed["error"]["data"] == {"state": "stopped", "savedMode": "default", "recoveryMethod": "session/load"}
            assert "error" in await peer.request("session/prompt", sessionId=sid, prompt=[{"type": "text", "text": "SYNTHETIC_STOPPED"}])
            (root / "fail-start").unlink()
            assert (await peer.request("session/load", sessionId=sid, cwd=str(root / "workspace"), mcpServers=[]))["result"]["modes"]["currentModeId"] == "default"
            assert await effect("ask-after-failed-downgrade", False) == 1
            checks.append("real_worker_failed_downgrade_stopped_and_reloads_ask")
            (root / "hold-start").touch()
            transition = asyncio.create_task(peer.request("session/set_mode", sessionId=sid, modeId="yolo"))
            async with asyncio.timeout(15):
                while not (root / "startup-entered").exists(): await asyncio.sleep(.02)
            await asyncio.gather(peer.request("session/cancel", sessionId=sid), peer.request("session/cancel", sessionId=sid))
            assert (await transition)["error"]["code"] == -32800
            (root / "hold-start").unlink()
            assert (await peer.request("session/load", sessionId=sid, cwd=str(root / "workspace"), mcpServers=[]))["result"]["modes"]["currentModeId"] == "default"
            checks.append("real_worker_repeated_stop_during_mode_start_cleans_up")
            await peer.close()
            peer = await PermissionPeer.start(argv, root / "workspace", dict(env, PERMISSION_PROBE_TOOLS="read"), acp=True)
            await peer.request("initialize", protocolVersion=1, clientCapabilities={})
            await peer.request("session/load", sessionId=sid, cwd=str(root / "workspace"), mcpServers=[])
            await peer.request("session/set_mode", sessionId=sid, modeId="yolo")
            assert await effect("restricted-write", False) == 0
            assert any("read" in json.dumps(request.get("tools", [])) and '"name": "write"' not in json.dumps(request.get("tools", [])) for request in fixture.requests[-2:])
            checks.append("yolo_restart_preserves_native_tool_selection")
            return {"checksPassed": checks, "nativePostureEvidence": "explicit argv plus actual tool behavior; no native RPC posture getter"}
        finally:
            await peer.close()
            await fixture.close()


def host_identity(binary, receipt_path=None, manifest_path=None):
    """Keep released bytes and a separately reviewed source candidate distinct."""
    digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    actual = digest(binary)
    if receipt_path is None and manifest_path is None:
        assert actual == HOST_DIGEST
        return {"kind": "released_studio4", "binarySha256": actual}
    if receipt_path is None or manifest_path is None:
        raise ValueError("Candidate host requires both --host-receipt and --host-source-manifest")
    receipt = json.loads(receipt_path.read_text())
    manifest = json.loads(manifest_path.read_text())
    assert receipt["schema"] == "aelix-studio.host-recovery-build/1" and receipt["status"] == "built_candidate"
    assert manifest["schema"] == "aelix-studio.host-recovery-source/1"
    assert receipt["sourceManifestSha256"] == digest(manifest_path)
    assert receipt["patchSha256"] == manifest["patch"]["sha256"]
    assert digest(manifest_path.parent / manifest["patch"]["filename"]) == receipt["patchSha256"]
    assert receipt["upstream"] == manifest["upstream"]
    assert manifest["upstream"]["commit"] == "47e66d0d151123e973b3fd1e77afcb5671b3f8c5"
    assert receipt["cargoLockSha256"] == manifest["cargoLockSha256"]
    assert receipt["licenseFileSha256"] == manifest["upstreamLicenseSha256"]
    assert receipt["releasedHostBytes"] is False and receipt["nativePatches"] == []
    assert receipt["binarySha256"] == actual
    return {"kind": "source_built_recovery_candidate", "binarySha256": actual,
            "buildReceiptSha256": digest(receipt_path), "sourceManifestSha256": digest(manifest_path),
            "patchSha256": receipt["patchSha256"], "upstreamCommit": manifest["upstream"]["commit"]}


async def host_probe(binary, adapter, native, identity=None):
    identity = identity or host_identity(binary)
    candidate = identity["kind"] == "source_built_recovery_candidate"
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == identity["binarySha256"]
    with tempfile.TemporaryDirectory(prefix="permission-host-") as temporary:
        root = Path(temporary).resolve()
        fixture = ToolFixture(root)
        await fixture.start()
        launch = wrapper(root, native)
        env = dict(fixture.env, PERMISSION_PROBE_ROOT=str(root), AIONUI_DISABLE_AUTO_UPDATE="1",
                   AIONUI_EXTENSIONS_PATH=str(root / "extensions"), AIONUI_EXTENSION_STATES_FILE=str(root / "extension-states.json"))
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]
        async def start_host():
            return await asyncio.create_subprocess_exec(binary, "--local", "--port", str(port), "--data-dir", root / "host",
                "--managed-resources-mode", "bundled", cwd=root / "workspace", env=env, stdout=-3, stderr=-3, start_new_session=True)
        async def stop_host():
            if process.returncode is None:
                process.terminate()
                try: await asyncio.wait_for(process.wait(), 10)
                except TimeoutError:
                    os.killpg(process.pid, signal.SIGKILL); await process.wait()
        process = await start_host()
        async def api(route, method="GET", body=None, expected=(200, 201, 202)):
            status, result = await asyncio.to_thread(http, port, route, method, body)
            assert status in expected, (route, status)
            return result.get("data", result)
        async def idle(route):
            async with asyncio.timeout(30):
                while True:
                    observed = await api(route + "/runtime/ensure", "POST")
                    if observed["runtime"]["state"] == "idle": return observed
                    await asyncio.sleep(.05)
        async def confirmation(route):
            async with asyncio.timeout(15):
                while True:
                    items = await api(route + "/confirmations")
                    if items: return items[0]
                    await asyncio.sleep(.05)
        def current(observed):
            return next(o for o in observed["config_options"] if o["id"] == "mode")["current_value"]
        checks = []
        try:
            async with asyncio.timeout(30):
                while True:
                    try:
                        await api("/api/assistants"); break
                    except OSError:
                        if process.returncode is not None: raise RuntimeError("Host exited")
                        await asyncio.sleep(.05)
            await api("/api/agents/custom", "POST", {"name": "ACP5 synthetic permissions", "command": str(adapter),
                "args": ["--aelix", str(launch), "--provider", "fixture", "--model", "one", "--state-dir", str(root / "acp")], "env": [], "icon": "🧪"})
            assistants = await api("/api/assistants")
            assistant = next(a for a in assistants if a["name"] == "ACP5 synthetic permissions")
            conv = await api("/api/conversations", "POST", {"name": "ACP5 permissions", "assistant": {"id": assistant["id"]},
                "extra": {"workspace": str(root / "workspace"), "custom_workspace": True}})
            route = "/api/conversations/" + conv["id"]
            initial = await api(route + "/runtime/ensure", "POST")
            assert current(initial) == "default"
            checks.append("actual_host_standard_mode_selector_starts_ask")
            target = root / "workspace/host-write.txt"
            fixture.next_tool = ("write", {"path": str(target), "content": "synthetic"})
            await api(route + "/messages", "POST", {"content": "SYNTHETIC_HOST_ASK"})
            item = await confirmation(route)
            assert not target.exists()
            await api(route + f"/confirmations/{item['call_id']}/confirm", "POST", {"msg_id": item["id"], "data": "reject-once"})
            await idle(route)
            assert not target.exists()
            checks.append("actual_host_ask_denial_no_write")
            await api(route + "/config-options/mode", "PUT", {"value": "yolo"})
            assert current(await api(route + "/runtime/ensure", "POST")) == "yolo"
            fixture.next_tool = ("write", {"path": str(target), "content": "synthetic"})
            await api(route + "/messages", "POST", {"content": "SYNTHETIC_HOST_YOLO"})
            await idle(route)
            assert target.exists() and not await api(route + "/confirmations")
            checks.append("actual_host_yolo_selector_and_tool_effect")
            restarted = await api(route + "/runtime/restart", "POST")
            assert current(restarted) == "yolo"
            checks.append("actual_host_restart_restores_yolo")
            await api(route + "/config-options/mode", "PUT", {"value": "default"})
            assert current(await api(route + "/runtime/ensure", "POST")) == "default"
            target.unlink()
            fixture.next_tool = ("write", {"path": str(target), "content": "synthetic"})
            turn = await api(route + "/messages", "POST", {"content": "SYNTHETIC_HOST_ASK_RESTORED"})
            await confirmation(route)
            await api(route + "/cancel", "POST", {"turn_id": turn["turn_id"]})
            await idle(route)
            assert not target.exists() and not await api(route + "/confirmations")
            checks.append("actual_host_downgrade_ask_and_pending_consent_stop")
            messages = await api(route + "/messages")
            assert "SYNTHETIC_HOST_ASK" in json.dumps(messages) and "SYNTHETIC_HOST_YOLO" in json.dumps(messages)
            checks.append("actual_host_history_preserved_across_mode_restarts")
            await api(route + "/config-options/mode", "PUT", {"value": "yolo"})
            (root / "fail-start").touch()
            failed_status, failed_body = await asyncio.to_thread(http, port, route + "/config-options/mode", "PUT", {"value": "default"})
            assert failed_status == 502 and failed_body.get("code") == "BAD_GATEWAY"
            (root / "fail-start").unlink()
            recovered = await api(route + "/runtime/restart", "POST")
            expected_mode = "default" if candidate else "yolo"
            assert current(recovered) == expected_mode, "Host recovery display differs from the qualified identity"
            mismatch_target = root / "workspace/post-failure-probe.txt"
            fixture.next_tool = ("write", {"path": str(mismatch_target), "content": "synthetic"})
            mismatch_turn = await api(route + "/messages", "POST", {"content": "SYNTHETIC_POST_FAILURE_EFFECT"})
            await confirmation(route)
            assert not mismatch_target.exists()
            await api(route + "/cancel", "POST", {"turn_id": mismatch_turn["turn_id"]})
            await idle(route)
            assert not mismatch_target.exists()
            checks.append("failed_downgrade_recovers_display_and_effective_ask" if candidate else
                          "known_host_blocker_display_yolo_but_actual_tool_still_asks")
            if candidate:
                # Observe the real debounced SQLite write before a full process exit.
                async with asyncio.timeout(10):
                    while True:
                        with sqlite3.connect(f"file:{root / 'host/aionui-backend.db'}?mode=ro", uri=True) as db:
                            row = db.execute("SELECT session_config FROM acp_session WHERE conversation_id = ?", (conv["id"],)).fetchone()
                        runtime = json.loads(row[0]).get("runtime", {}) if row else {}
                        selections = runtime.get("config_selections", {})
                        if runtime.get("current_mode_id") == "default" and selections.get("mode") != "yolo": break
                        await asyncio.sleep(.05)
                checks.append("recovered_ask_and_cleaned_config_observed_in_host_sqlite")
                await stop_host()
                process = await start_host()
                async with asyncio.timeout(30):
                    while True:
                        try:
                            await api("/api/assistants"); break
                        except OSError: await asyncio.sleep(.05)
                assert current(await api(route + "/runtime/ensure", "POST")) == "default"
                fixture.next_tool = ("write", {"path": str(mismatch_target), "content": "synthetic"})
                turn = await api(route + "/messages", "POST", {"content": "SYNTHETIC_HOST_PROCESS_RELOAD"})
                await confirmation(route)
                assert not mismatch_target.exists()
                await api(route + "/cancel", "POST", {"turn_id": turn["turn_id"]})
                await idle(route)
                assert not mismatch_target.exists()
                checks.append("full_host_process_restart_restores_display_and_effective_ask")
                await api(route + "/config-options/mode", "PUT", {"value": "yolo"})
                assert current(await api(route + "/runtime/ensure", "POST")) == "yolo"
                fixture.next_tool = ("write", {"path": str(mismatch_target), "content": "synthetic"})
                await api(route + "/messages", "POST", {"content": "SYNTHETIC_LATER_EXPLICIT_YOLO"})
                await idle(route)
                assert mismatch_target.exists() and not await api(route + "/confirmations")
                checks.append("later_explicit_yolo_selection_still_controls_actual_effect")
            return {"checksPassed": checks, "identity": identity, "binarySha256": identity["binarySha256"], "renderer": "not run", "provider": "synthetic loopback",
                    "failedDowngradeHttpStatus": failed_status, "displayedModeAfterFailure": expected_mode,
                    "actualApprovalAfterFailure": "ask", "unapprovedWriteObserved": False,
                    "acceptance": "passed_with_source_built_candidate" if candidate else "blocked_on_host_display_effective_mismatch"}
        finally:
            await stop_host()
            await fixture.close()


async def run(args, report):
    native = Path(sys.executable).parent / "aelix"
    manifest = json.loads((ROOT / "docs/beta2/manifest.json").read_text())
    report["stage"] = "identity_before"
    report["nativeBefore"] = verify(manifest, args.artifacts)
    report["adapterBefore"] = adapter_receipt(args.adapter)
    report["stage"] = "native_mode_audit"
    report["nativeModes"] = await native_audit(native)
    report["stage"] = "installed_adapter"
    report["adapter"] = await adapter_probe(args.adapter, native)
    if args.host:
        report["stage"] = "actual_host"
        identity = host_identity(args.host, args.host_receipt, args.host_source_manifest)
        report["host"] = await host_probe(args.host, args.adapter, native, identity)
    else:
        report["host"] = {"status": "not_run", "checksPassed": [], "reason": "--host omitted (native/ACP-only CI)"}
    report["nativeAfter"] = verify(manifest, args.artifacts)
    report["adapterAfter"] = adapter_receipt(args.adapter)
    assert report["nativeBefore"] == report["nativeAfter"] and report["adapterBefore"] == report["adapterAfter"]
    blocked = args.host and report["host"]["acceptance"] == "blocked_on_host_display_effective_mismatch"
    report.update(stage="complete", status="qualified_with_host_blocker" if blocked else "passed")


def main():
    if not __debug__: raise RuntimeError("Permission qualification requires Python without -O")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--host", type=Path, help="Optional pinned Studio #4 AionCore binary for actual host checks")
    parser.add_argument("--host-receipt", type=Path, help="Explicit source-built candidate build receipt (not released host bytes)")
    parser.add_argument("--host-source-manifest", type=Path, help="Reviewed Studio recovery source manifest, with adjacent patch")
    parser.add_argument("--artifacts", type=Path, default=ROOT / ".local/beta2-wheels")
    args = parser.parse_args()
    args.adapter = args.adapter.resolve()
    if args.host: args.host = args.host.resolve()
    if (args.host_receipt or args.host_source_manifest) and not args.host:
        parser.error("Candidate identity requires --host")
    report = {"schema": "aelix-acp.permission-qualification/1", "status": "failed", "nativePatches": [],
              "platform": platform.system() + "/" + platform.machine(), "realProvider": False, "installer": False,
              "recordedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "checkoutCommit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
              "harnessSourceSha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in
                  (Path(__file__), SCRIPTS / "permission_fixture_extension.py")}}
    try: asyncio.run(run(args, report))
    finally:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "nativeModeCases": 20, "adapterChecks": len(report["adapter"]["checksPassed"])}))


if __name__ == "__main__": main()
