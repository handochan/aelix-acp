"""ACP #7: actual public-wheel, sessionless spike and pinned AionCore wire checks.

No production helper/host forwarding is installed. Model responses and OAuth are
synthetic. Run using a disposable public native wheel Python (without -O).
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
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

from beta2_identity import verify
from beta2_wire import Fixture, Peer, isolated_environment
from qualify_beta2 import adapter_receipt
from management_model_fixture import ManagementFixture

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
HOST_DIGEST = "04404864ca622159ffcc79ef9cb468e82fc820b91f16d565c5a5efdb0b0c4cb1"


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


async def helper_probe(python):
    with tempfile.TemporaryDirectory(prefix="acp7-helper-") as temporary:
        root = Path(temporary).resolve()
        for name in ("home", "workspace", "agent/agents"):
            (root / name).mkdir(parents=True, exist_ok=True)
        env = isolated_environment(root)
        # Deliberately disagree: public CLI ignores AELIX_AUTH_PATH in its explicit constructor.
        env["AELIX_AUTH_PATH"] = str(root / "different/auth.json")
        (root / "agent/agents/probe.md").write_text("---\nname: probe\ndescription: Synthetic profile\ntools: [read]\n---\nSynthetic role.\n")
        argv = [python, "-I", "-B", SCRIPTS / "management_probe_helper.py", root]
        first = await Peer.start(argv, root / "workspace", env, acp=True)
        second = await Peer.start(argv, root / "workspace", env, acp=True)
        checks = []
        responses = []

        async def call(peer, method, **params):
            frame = await peer.request(method, **params)
            responses.append(frame)
            return frame
        try:
            roots = (await call(first, "probe/roots"))["result"]
            assert roots["cli"] == str(root / "agent/auth.json") and roots["cli"] != roots["implicit"]
            assert not (root / "sessions").exists()
            checks.append("explicit_cli_auth_root_differs_from_implicit_storage")
            assert not (await call(first, "auth/status", provider="probe"))["result"]["configured"]
            sentinel = "SYNTHETIC_API_KEY_DO_NOT_LOG_7"
            assert "result" in await call(second, "auth/set_api_key", provider="probe", key=sentinel)
            assert not (await call(first, "probe/held_status", provider="probe"))["result"]["configured"]
            assert (await call(first, "auth/status", provider="probe"))["result"] == {"configured": True, "source": "stored", "label": None}
            assert json.loads((root / "agent/auth.json").read_text())["probe"]["key"] == sentinel
            assert not Path(roots["implicit"]).exists()
            assert (root / "agent/auth.json").stat().st_mode & 0o777 == 0o600
            checks += ["llm_free_key_registration_native_store", "two_process_held_auth_stale_until_reload", "native_auth_file_private"]
            await call(first, "auth/set_api_key", provider="other", key="synthetic-other")
            await call(second, "auth/logout", provider="probe")
            stored = json.loads((root / "agent/auth.json").read_text())
            assert "probe" not in stored and "other" in stored
            checks.append("sequential_two_process_provider_merge_and_logout")
            login = asyncio.create_task(call(first, "probe/login"))
            await first.until(lambda: any(e.get("params", {}).get("kind") == "input" for e in first.events))
            key = next(e["params"]["requestId"] for e in first.events if e.get("params", {}).get("kind") == "input")
            await first.send({"jsonrpc": "2.0", "method": "probe/cancel", "params": {"requestId": key}})
            assert (await login)["error"]["code"] == -32800
            assert "probe-oauth" not in json.loads((root / "agent/auth.json").read_text())
            checks.append("synthetic_native_oauth_callback_cancel_no_commit")
            first.events.clear()
            login = asyncio.create_task(call(first, "probe/login"))
            await first.until(lambda: any(e.get("params", {}).get("kind") == "input" for e in first.events))
            key = next(e["params"]["requestId"] for e in first.events if e.get("params", {}).get("kind") == "input")
            await first.send({"jsonrpc": "2.0", "method": "probe/reply", "params": {"requestId": key, "value": "synthetic-code"}})
            assert (await login)["result"]["configured"]
            await call(first, "auth/logout", provider="probe-oauth")
            checks.append("synthetic_native_oauth_callback_commit_and_logout")
            first.events.clear()
            login = asyncio.create_task(call(first, "probe/login"))
            await first.until(lambda: any(e.get("params", {}).get("kind") == "input" for e in first.events))
            first.process.stdin.close()  # Require graceful EOF, not Peer.close's kill fallback.
            assert (await asyncio.wait_for(login, 5))["error"]["code"] == -32800
            assert await asyncio.wait_for(first.process.wait(), 5) == 0
            await first.close()
            assert "probe-oauth" not in json.loads((root / "agent/auth.json").read_text())
            first = await Peer.start(argv, root / "workspace", env, acp=True)
            checks.append("helper_eof_cancels_pending_oauth_without_commit")
            assert (await call(first, "profiles/resolve", name="probe"))["result"]["tools"] == ["read"]
            assert (await call(first, "profiles/resolve", name="missing"))["error"]["code"] == -32603
            assert (await call(first, "packages/list"))["result"]["liveLoaded"] == "unknown"
            assert (await call(first, "session/prompt"))["error"]["code"] == -32601
            checks += ["native_sessionless_profile_resolution_and_failure", "native_package_inventory_without_worker", "helper_refuses_live_session_methods"]
            public = json.dumps(first.events + second.events + responses)
            assert sentinel not in public and "synthetic-access" not in public and "synthetic-code" not in public
            checks.append("status_and_results_omit_secret_values")
            return {"checksPassed": checks, "llmWorkersStarted": 0, "productionHelper": False,
                    "oauth": "registered synthetic provider invoking real AuthStorage.login callbacks; real providers untested",
                    "sharedMutation": "sequential only; concurrent legacy writes unsupported",
                    "profileCrud": "not implemented", "packageMutation": "not exercised"}
        finally:
            await first.close()
            await second.close()


async def auth_lock_probe(python):
    with tempfile.TemporaryDirectory(prefix="acp7-lock-") as temporary:
        root = Path(temporary).resolve()
        (root / "home").mkdir()
        env = isolated_environment(root)
        argv = [python, "-I", "-B", SCRIPTS / "auth_lock_probe.py"]
        holder = await asyncio.create_subprocess_exec(*map(str, [*argv, "holder", root / "auth.json"]),
            cwd=root, env=env, stdin=-1, stdout=-1, stderr=-1)
        contender = None
        try:
            result = json.loads(await asyncio.wait_for(holder.stdout.readline(), 10))
            assert result["heldDescriptorInodeChanged"] is True
            contender = await asyncio.create_subprocess_exec(*map(str, [*argv, "contender", root / "auth.json"]),
                cwd=root, env=env, stdout=-1, stderr=-1)
            stdout, stderr = await asyncio.wait_for(contender.communicate(), 5)
            assert contender.returncode == 0 and not stderr
            assert json.loads(stdout)["publicSetterCompleted"] is True
            return {"checksPassed": ["native_rename_changes_locked_inode", "second_process_public_setter_completes_while_old_fd_locked"],
                    "scope": "native private IO primitive counterexample; not a full public-setter lost-update/crash stress test",
                    "concurrentSharedMutationSupported": False, "windows": "not run"}
        finally:
            holder.stdin.close()
            for process in (holder, contender):
                if process and process.returncode is None:
                    try:
                        await asyncio.wait_for(process.wait(), 3)
                    except TimeoutError:
                        process.kill()
                        await process.wait()


async def adapter_probe(adapter, native):
    with tempfile.TemporaryDirectory(prefix="acp7-adapter-") as temporary:
        root = Path(temporary).resolve()
        fixture = Fixture(root)
        await fixture.start()
        argv = [adapter, "--aelix", native, "--provider", "fixture", "--model", "one", "--state-dir", root / "state"]
        peer = await Peer.start(argv, root / "workspace", fixture.env, acp=True)
        try:
            initialized = (await peer.request("initialize", protocolVersion=1, clientCapabilities={}))["result"]
            assert initialized["authMethods"] == []
            assert (await peer.request("authenticate", methodId="probe"))["error"]["code"] == -32602
            assert (await peer.request("_aelix/manage", action="auth/status"))["error"]["code"] == -32601
            session = (await peer.request("session/new", cwd=str(root / "workspace"), mcpServers=[]))["result"]["sessionId"]
            params = {"sessionId": session, "prompt": [{"type": "text", "text": "SYNTHETIC_DUPLICATE"}], "_meta": {"aelix/requestId": "same-key"}}
            for _ in range(2):
                assert (await peer.request("session/prompt", **params))["result"]["stopReason"] == "end_turn"
            assert len(fixture.requests) == 2, "Current pin has no deduplication ledger"
            pending = asyncio.create_task(peer.request("session/prompt", sessionId=session, prompt=[{"type": "text", "text": "QUALIFICATION_HOLD"}]))
            await asyncio.wait_for(fixture.entered.wait(), 15)
            busy = await peer.request("session/prompt", sessionId=session, prompt=[{"type": "text", "text": "SYNTHETIC_BUSY"}])
            assert busy["error"]["code"] == -32602
            await peer.send({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": session}})
            assert (await pending)["result"]["stopReason"] == "cancelled"
            await peer.close()
            peer = await Peer.start(argv, root / "workspace", fixture.env, acp=True)
            await peer.request("initialize", protocolVersion=1, clientCapabilities={})
            assert "result" in await peer.request("session/load", sessionId=session, cwd=str(root / "workspace"), mcpServers=[])
            assert any(e.get("params", {}).get("update", {}).get("sessionUpdate") == "user_message_chunk" for e in peer.events)
            return {"checksPassed": ["auth_not_advertised_authenticate_rejected", "custom_manage_method_not_found",
                     "same_request_id_executes_twice_no_dedup", "busy_prompt_rejected", "notification_cancel_native_terminal",
                     "disconnect_new_process_history_resume"], "busyCode": busy["error"]["code"],
                    "deduplication": False, "reconnect": "history replay after closing old process; no live attach or pending-operation replay"}
        finally:
            await peer.close()
            await fixture.close()


def http(port, route, method="GET", body=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(f"http://127.0.0.1:{port}{route}", data=data, method=method,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            result = json.loads(raw)
        except ValueError:
            result = {}
        return error.code, result


async def host_auth_boundary(binary):
    with tempfile.TemporaryDirectory(prefix="acp7-webui-") as temporary:
        root = Path(temporary).resolve()
        (root / "home").mkdir()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        process = await asyncio.create_subprocess_exec(binary, "--port", str(port), "--data-dir", str(root / "data"),
            "--managed-resources-mode", "bundled", cwd=root, env=isolated_environment(root), stdout=-3, stderr=-3)
        try:
            async with asyncio.timeout(30):
                while True:
                    try:
                        status, _ = await asyncio.to_thread(http, port, "/api/assistants")
                        break
                    except OSError:
                        if process.returncode is not None:
                            raise RuntimeError("Authenticated host startup failed")
                        await asyncio.sleep(.1)
            assert status == 401
            return {"checksPassed": ["nonlocal_host_rejects_unauthenticated_facade_request"], "httpStatus": status,
                    "providerOAuth": "independent from WebUI login", "helperRoute": "production helper not implemented"}
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 10)
                except TimeoutError:
                    process.kill()
                    await process.wait()


async def host_probe(binary, adapter, native):
    assert digest(binary) == HOST_DIGEST, "Only the Studio #4 pinned AionCore binary qualifies"
    with tempfile.TemporaryDirectory(prefix="acp7-host-") as temporary:
        root = Path(temporary).resolve()
        fixture = ManagementFixture(root)
        await fixture.start()
        wire = root / "wire.jsonl"
        wire.touch()
        env = dict(fixture.env, PROBE_WIRE_LOG=str(wire), AIONUI_DISABLE_AUTO_UPDATE="1",
                   AIONUI_EXTENSIONS_PATH=str(root / "extensions"), AIONUI_EXTENSION_STATES_FILE=str(root / "extension-state.json"))
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        process = await asyncio.create_subprocess_exec(binary, "--local", "--port", str(port), "--data-dir", str(root / "host"),
            "--managed-resources-mode", "bundled", cwd=root / "workspace", env=env, stdout=-3, stderr=-3, start_new_session=True)
        async def api(route, method="GET", body=None, expected=(200, 201, 202)):
            status, response = await asyncio.to_thread(http, port, route, method, body)
            assert status in expected, (route, status, sorted(response))
            return status, response.get("data", response)

        async def wait_idle(conversation):
            async with asyncio.timeout(30):
                while True:
                    _, state = await api(f"/api/conversations/{conversation}/runtime/ensure", "POST")
                    if state["runtime"]["state"] == "idle":
                        return state
                    await asyncio.sleep(.1)

        async def wait_permission(route):
            async with asyncio.timeout(20):
                while True:
                    _, confirmations = await api(route + "/confirmations")
                    if confirmations:
                        return confirmations[0]
                    await asyncio.sleep(.1)

        try:
            async with asyncio.timeout(30):
                while True:
                    try:
                        await api("/api/assistants")
                        break
                    except (OSError, AssertionError):
                        if process.returncode is not None:
                            raise RuntimeError("AionCore startup failed")
                        await asyncio.sleep(.1)
            await api("/api/agents/custom", "POST", {"name": "ACP7 wire qualification", "command": sys.executable,
                "args": ["-I", "-B", str(SCRIPTS / "host_wire_tap.py"), str(adapter), "--aelix", str(native),
                         "--provider", "fixture", "--model", "one", "--state-dir", str(root / "acp")], "env": [], "icon": "🧪"})
            _, assistants = await api("/api/assistants")
            assistant = next(a for a in assistants if a["name"] == "ACP7 wire qualification")
            _, created = await api("/api/conversations", "POST", {"name": "ACP7 synthetic wire", "assistant": {"id": assistant["id"]},
                "extra": {"workspace": str(root / "workspace"), "custom_workspace": True}})
            conv = created["id"]
            route = f"/api/conversations/{conv}"
            _, initial = await api(route + "/runtime/ensure", "POST")
            assert next(o for o in initial["config_options"] if o["id"] == "model")["current_value"] == "fixture/one"
            assert initial["runtime"]["supports_midturn_delivery"] is False
            # HTTP arbitrary forwarding is NOT an existing contract; these candidate routes must not succeed.
            custom_status, _ = await api(route + "/_aelix/manage", "POST", {"action": "auth/status"}, expected=(404, 405))
            unsupported, _ = await api(route + "/config-options/model", "PUT", {"value": "fixture/two"}, expected=(400,))
            for _ in range(2):
                await api(route + "/messages", "POST", {"content": "SYNTHETIC_HOST_DUPLICATE", "_meta": {"aelix/requestId": "same-host-key"}})
                await wait_idle(conv)
            assert len(fixture.requests) == 2
            await api(route + "/messages", "POST", {"content": "SYNTHETIC_TOOL_DENY"})
            permission = await wait_permission(route)
            offered = [o["value"] for o in permission["options"]]
            assert "reject-once" in offered
            await api(route + f"/confirmations/{permission['call_id']}/confirm", "POST", {"msg_id": permission["id"], "data": "reject-once"})
            await wait_idle(conv)
            assert not (root / "workspace/must-not-exist.txt").exists()
            _, permission_turn = await api(route + "/messages", "POST", {"content": "SYNTHETIC_TOOL_CANCEL"})
            await wait_permission(route)
            _, waiting = await api(route + "/runtime/ensure", "POST")
            assert waiting["runtime"]["pending_confirmations"] > 0
            permission_cancel_start = len(wire.read_text().splitlines())
            await api(route + "/cancel", "POST", {"turn_id": permission_turn["turn_id"]})
            await wait_idle(conv)
            assert any(json.loads(line).get("stopReason") == "cancelled"
                       for line in wire.read_text().splitlines()[permission_cancel_start:])
            assert not (root / "workspace/must-not-exist.txt").exists()
            _, cleared = await api(route + "/confirmations")
            assert not cleared
            _, turn = await api(route + "/messages", "POST", {"content": "QUALIFICATION_HOLD"})
            await asyncio.wait_for(fixture.entered.wait(), 20)
            await asyncio.sleep(3)  # Bounded observation of the unsupported synthetic reverse RPC.
            _, active = await api(route + "/runtime/ensure", "POST")
            busy_status, _ = await api(route + "/messages", "POST", {"content": "SYNTHETIC_HOST_BUSY"}, expected=(400, 409, 422))
            before_stale = wire.read_text().count('"method": "session/cancel"')
            stale_status, stale = await api(route + "/cancel", "POST", {"turn_id": "stale-turn-id"})
            assert stale["runtime"]["state"] == "running"
            assert wire.read_text().count('"method": "session/cancel"') == before_stale
            provider_cancel_start = len(wire.read_text().splitlines())
            await api(route + "/cancel", "POST", {"turn_id": turn["turn_id"]})
            await wait_idle(conv)
            assert any(json.loads(line).get("stopReason") == "cancelled"
                       for line in wire.read_text().splitlines()[provider_cancel_start:])
            _, messages = await api(route + "/messages")
            assert "QUALIFICATION_OK" in json.dumps(messages)
            _, restarted = await api(route + "/runtime/restart", "POST")
            assert restarted["runtime"]["state"] == "idle"
            _, restored_messages = await api(route + "/messages")
            assert messages == restored_messages
            frames = [json.loads(line) for line in wire.read_text().splitlines()]
            prompts = [f for f in frames if f["direction"] == "host_to_adapter" and f["method"] == "session/prompt"]
            assert len(prompts) == 5 and all("_meta" not in f["paramKeys"] for f in prompts)
            assert any(f["method"] == "session/cancel" and not f["hasId"] for f in frames)
            assert any(f.get("stopReason") == "cancelled" for f in frames)
            injected = next(f for f in frames if f["direction"] == "fixture_to_host")
            reverse_replies = [f for f in frames if f["direction"] == "host_to_adapter" and f["fixtureReverseReply"]]
            assert not reverse_replies, "Known custom-agent route gap changed; requalify before claiming forwarding"
            reverse_observation = frames[-1]["observedAtMonotonic"] - injected["observedAtMonotonic"]
            assert any(f["method"] == "session/load" for f in frames)
            return {"checksPassed": ["actual_host_basic_session", "legacy_selector_translation", "unsupported_model_rejected",
                    "same_http_request_id_runs_twice_metadata_lost", "busy_delivery_rejected", "stale_turn_cancel_noop",
                    "native_tool_denial_no_write_effect", "cancel_pending_permission_no_write_effect",
                    "host_cancel_notification_and_cancelled_terminal", "host_history_and_runtime_restart",
                    "synthetic_custom_reverse_request_no_reply_observed"], "hostBinarySha256": digest(binary),
                    "hostMode": "isolated local embedded HTTP facade; no renderer UI acceptance",
                    "http": {"customRoute": custom_status, "modelUnsupported": unsupported, "busy": busy_status, "staleCancel": stale_status},
                    "activeRuntimeState": active["runtime"]["state"], "wire": frames,
                    "providerRequests": len(fixture.requests), "customReverseRequest": {
                        "source": "test-only injected frame, not native capability", "replyObserved": False,
                        "observationSeconds": reverse_observation, "supported": False}}
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 10)
                except TimeoutError:
                    os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()
            await fixture.close()


async def run(args, report):
    python, native = Path(sys.executable), Path(sys.executable).parent / "aelix"
    manifest = json.loads((ROOT / "docs/beta2/manifest.json").read_text())
    report["stage"] = "identity_before"
    report["identityBefore"] = verify(manifest, args.artifacts)
    report["adapterBefore"] = adapter_receipt(args.adapter)
    report["stage"] = "sessionless_native_spike"
    report["helper"] = await helper_probe(python)
    report["sharedAuth"] = await auth_lock_probe(python)
    report["stage"] = "installed_adapter_wire"
    report["adapter"] = await adapter_probe(args.adapter, native)
    if args.native_only:
        report["host"] = report["hostAuth"] = {"status": "not_run", "checksPassed": [], "reason": "native-only CI mode"}
    else:
        report["stage"] = "actual_host_wire"
        report["host"] = await host_probe(args.host, args.adapter, native)
        report["hostAuth"] = await host_auth_boundary(args.host)
    report["stage"] = "identity_after"
    report["identityAfter"] = verify(manifest, args.artifacts)
    report["adapterAfter"] = adapter_receipt(args.adapter)
    assert report["identityBefore"] == report["identityAfter"]
    assert report["adapterBefore"] == report["adapterAfter"]
    report.update(stage="complete", status="passed")


def main():
    if not __debug__:
        raise RuntimeError("Qualification requires Python without -O")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter", type=Path, required=True)
    parser.add_argument("--host", type=Path, help="Exact Studio #4 darwin-arm64 AionCore binary")
    parser.add_argument("--native-only", action="store_true", help="Run native/helper/ACP checks, explicitly omit host acceptance")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, default=ROOT / ".local/beta2-wheels")
    args = parser.parse_args()
    if (args.host is None) == (not args.native_only):
        parser.error("Choose either --host BINARY or --native-only")
    args.adapter = args.adapter.resolve()
    if args.host:
        args.host = args.host.resolve()
    report = {"schema": "aelix-acp.management-qualification/1", "status": "failed",
              "recordedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "platform": platform.system() + "/" + platform.machine(), "python": platform.python_version(),
              "scope": "test-only helper spike, installed ACP, actual AionCore HTTP/stdio; synthetic model and OAuth",
              "mode": "native-only" if args.native_only else "native-and-host",
              "nativePatches": [], "realProvider": False, "installer": False,
              "sourceCommit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
              "harnessSourceSha256": {p.name: digest(p) for p in (Path(__file__), SCRIPTS / "management_probe_helper.py", SCRIPTS / "host_wire_tap.py", SCRIPTS / "auth_lock_probe.py", SCRIPTS / "management_model_fixture.py")}}
    try:
        asyncio.run(run(args, report))
    finally:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "checks": {k: len(report[k]["checksPassed"]) for k in ("helper", "adapter", "host")}}))


if __name__ == "__main__":
    main()
