"""Host smoke: an actual AionCore host + this ACP + a real native runtime.

The model is a synthetic loopback endpoint; no provider credentials, user data
or internet model calls are used. The wire tap records frame structure only.

    python scripts/smoke_host.py --host /path/to/aioncore --aelix /path/to/native/aelix \
        --report .local/host-smoke.json [--control-acp-source /path/to/older/src]

Checks: delegated child progress on the parent `agent` tool card, a native lock
refusal reaching the host as a structured error, and a failure after native
dispatch not being replayed by the host. With --control-acp-source the replay
check also runs against that adapter, to show the host behavior being avoided.
"""
import argparse
import asyncio
import contextlib
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def http(port, path, method="GET", body=None):
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.loads(error.read())
        except ValueError:
            return error.code, {}


def tap(log, argv):
    """Transparent stdio relay that records frame structure, never payload text."""
    child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, start_new_session=True)

    def record(direction, frame):
        params = frame.get("params") if isinstance(frame.get("params"), dict) else {}
        update = params.get("update") if isinstance(params.get("update"), dict) else {}
        error = frame.get("error") if isinstance(frame.get("error"), dict) else {}
        entry = {"direction": direction, "method": frame.get("method"), "id": frame.get("id"),
                 "sessionUpdate": update.get("sessionUpdate"), "toolCallId": update.get("toolCallId"),
                 "status": update.get("status"), "metaKeys": sorted(update.get("_meta") or {}),
                 "errorCode": error.get("code"),
                 "errorKind": (error.get("data") or {}).get("kind") if isinstance(error.get("data"), dict) else None}
        with open(log, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry) + "\n")

    import threading

    def outgoing():
        for line in child.stdout:
            with contextlib.suppress(ValueError):
                record("adapter_to_host", json.loads(line))
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()

    thread = threading.Thread(target=outgoing, daemon=True)
    thread.start()
    for line in sys.stdin.buffer:
        with contextlib.suppress(ValueError):
            record("host_to_adapter", json.loads(line))
        child.stdin.write(line)
        child.stdin.flush()
    child.stdin.close()
    try:
        child.wait(10)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
    thread.join(5)


class Model:
    """OpenAI-compatible streaming fixture with scripted delegation and failure."""

    def __init__(self):
        self.requests = []

    async def start(self):
        self.server = await asyncio.start_server(self.respond, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def respond(self, reader, writer):
        try:
            header = (await reader.readuntil(b"\r\n\r\n")).decode()
            length = int(next(line.split(":", 1)[1] for line in header.split("\r\n") if line.lower().startswith("content-length:")))
            request = json.loads(await reader.readexactly(length))
            self.requests.append(request)
            messages = request.get("messages", [])
            last = messages[-1] if messages else {}
            content = json.dumps(last.get("content"))
            if last.get("role") == "user" and "FAIL_NOW" in content:
                body = b'data: {"error":{"message":"SYNTHETIC_TURN_FAILURE","type":"server_error"}}\n\n'
            else:
                if last.get("role") == "user" and "DELEGATE_NOW" in content:
                    delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": f"call_agent_{len(self.requests)}", "type": "function",
                             "function": {"name": "agent", "arguments": json.dumps({"profile": "reviewer", "task": "CHILD_TASK reply"})}}]}
                    stop = "tool_calls"
                elif last.get("role") == "user" and "CHILD_TASK" in content:
                    await asyncio.sleep(1.5)  # Long enough for live progress frames.
                    delta, stop = {"role": "assistant", "content": "CHILD_OK"}, "stop"
                else:
                    delta, stop = {"role": "assistant", "content": "SMOKE_OK"}, "stop"
                body = ("".join("data: " + json.dumps({"id": "f", "object": "chat.completion.chunk", "created": 0,
                        "model": request.get("model", "one"), "choices": [{"index": 0, "delta": d, "finish_reason": f}]}) + "\n\n"
                        for d, f in ((delta, None), ({}, stop))) + "data: [DONE]\n\n").encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\nContent-Length: "
                         + str(len(body)).encode() + b"\r\n\r\n" + body)
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    def count(self, marker):
        return sum(1 for request in self.requests if request.get("messages")
                   and request["messages"][-1].get("role") == "user" and marker in json.dumps(request["messages"][-1].get("content")))


class Host:
    def __init__(self, binary, root, env):
        self.binary, self.root, self.env = binary, root, env
        self.process = None

    async def start(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        log = (self.root / "host.log").open("ab")
        self.process = await asyncio.create_subprocess_exec(
            str(self.binary), "--local", "--port", str(self.port), "--data-dir", str(self.root / "host"),
            "--managed-resources-mode", "bundled", cwd=self.root / "workspace", env=self.env,
            stdout=log, stderr=log, start_new_session=True)
        async with asyncio.timeout(30):
            while True:
                try:
                    await self.api("/api/assistants")
                    return
                except OSError:
                    if self.process.returncode is not None:
                        raise RuntimeError("Host exited during startup")
                    await asyncio.sleep(0.05)

    async def stop(self):
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 10)
            except TimeoutError:
                os.killpg(self.process.pid, signal.SIGKILL)
                await self.process.wait()

    async def api(self, route, method="GET", body=None, expected=(200, 201, 202)):
        status, result = await asyncio.to_thread(http, self.port, route, method, body)
        assert status in expected, (route, status, result)
        return result.get("data", result) if isinstance(result, dict) else result

    async def idle(self, route, timeout=60):
        async with asyncio.timeout(timeout):
            while True:
                runtime = (await self.api(route)).get("runtime") or {}
                if runtime.get("state") in ("idle", None) and not runtime.get("is_processing") and runtime.get("turn_id") is None:
                    return
                await asyncio.sleep(0.1)

    def rows(self, conversation):
        with contextlib.closing(sqlite3.connect(f"file:{self.root / 'host/aionui-backend.db'}?mode=ro", uri=True)) as db:
            return [dict(id=row[0], type=row[1], content=row[2], status=row[3]) for row in db.execute(
                "SELECT id, type, content, status FROM messages WHERE conversation_id=? ORDER BY created_at", (conversation,))]


def frames(log):
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


async def scenario(args, source, label):
    with tempfile.TemporaryDirectory(prefix="acp-host-smoke-", dir="/tmp" if os.name != "nt" else None) as directory:
        root = Path(directory).resolve()
        for name in ("home", "agent/agents", "workspace", "host"):
            (root / name).mkdir(parents=True)
        model = Model()
        port = await model.start()
        (root / "agent/models.json").write_text(json.dumps({"providers": {"fixture": {
            "baseUrl": f"http://127.0.0.1:{port}/v1", "api": "openai-completions", "apiKey": "synthetic-fixture-key",
            "models": [{"id": "one", "name": "one", "reasoning": False, "input": ["text"], "contextWindow": 32768, "maxTokens": 1024}]}}}))
        # Native retry off: a provider failure reaches ACP after one model request.
        (root / "agent/settings.json").write_text(json.dumps({"defaultProvider": "fixture", "defaultModel": "one", "retry": {"enabled": False}}))
        (root / "agent/agents/reviewer.md").write_text("---\nname: reviewer\ndescription: Review\ntools: [read]\n---\nReview.\n")
        env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR", "TMP", "TEMP") if key in os.environ}
        env.update(HOME=str(root / "home"), USERPROFILE=str(root / "home"), AELIX_CODING_AGENT_DIR=str(root / "agent"),
                   AELIX_SETTINGS_PATH=str(root / "agent/settings.json"), AELIX_AUTH_PATH=str(root / "agent/auth.json"),
                   AIONUI_DISABLE_AUTO_UPDATE="1", AIONUI_EXTENSIONS_PATH=str(root / "extensions"),
                   AIONUI_EXTENSION_STATES_FILE=str(root / "extension-states.json"))
        log = root / "wire.jsonl"
        launcher = root / "acp-launch.py"
        launcher.write_text(f"import sys\nsys.path.insert(0, {str(source)!r})\nfrom aelix_acp.server import main\nmain()\n")
        adapter = [str(launcher), "--aelix", str(args.aelix), "--provider", "fixture", "--model", "one",
                   "--permission-mode", "yolo", "--enable-agents", "--state-dir", str(root / "acp")]
        if label == "control":
            adapter.remove("--permission-mode")
            adapter.remove("yolo")
        argv = [str(Path(__file__).resolve()), "tap", str(log), "--", args.python, *adapter]
        host = Host(args.host, root, env)
        checks, observed = [], {}
        await host.start()
        try:
            await host.api("/api/agents/custom", "POST", {"name": f"ACP20 {label}", "command": args.python, "args": argv,
                                                           "env": [], "icon": "A"})
            assistant = next(a for a in await host.api("/api/assistants") if a["name"] == f"ACP20 {label}")

            async def conversation(name):
                created = await host.api("/api/conversations", "POST", {"name": name, "assistant": {"id": assistant["id"]},
                                         "extra": {"workspace": str(root / "workspace"), "custom_workspace": True}})
                return created["id"], "/api/conversations/" + created["id"]

            # A failure after native dispatch: count prompts the host sends for one user message.
            failing, route = await conversation("failure")
            await host.api(route + "/runtime/ensure", "POST")
            mark = len(frames(log))
            await host.api(route + "/messages", "POST", {"content": "FAIL_NOW"})
            await host.idle(route)
            await asyncio.sleep(2)  # A replay would start right after the first terminal.
            await host.idle(route)
            after = frames(log)[mark:]
            replies = [f for f in after if f["direction"] == "adapter_to_host" and f["errorCode"] is not None]
            observed["failure"] = {"prompts": sum(f["method"] == "session/prompt" for f in after),
                                   "modelRequests": model.count("FAIL_NOW"),
                                   "hostRequests": [f["method"] for f in after if f["direction"] == "host_to_adapter" and f["method"]],
                                   "errorCodes": [f["errorCode"] for f in replies],
                                   "errorKinds": [f["errorKind"] for f in replies]}
            if label == "control":
                return {"label": label, "observed": observed}
            assert (observed["failure"]["prompts"], observed["failure"]["modelRequests"], observed["failure"]["errorCodes"],
                    observed["failure"]["errorKinds"]) == (1, 1, [-33002], ["turn_failed_after_dispatch"]), observed
            # The host may cancel the failed turn; it must not reload or prompt again.
            assert [m for m in observed["failure"]["hostRequests"] if m != "session/cancel"] == ["session/prompt"], observed
            checks.append("failure_after_dispatch_reaches_host_once_without_replay")

            # Delegation: child progress on the parent's tool card.
            delegating, route = await conversation("delegation")
            await host.api(route + "/runtime/ensure", "POST")
            await host.api(route + "/messages", "POST", {"content": "DELEGATE_NOW"})
            await host.idle(route)
            wire = [f for f in frames(log) if f["direction"] == "adapter_to_host" and f["toolCallId"]]
            parent = next(f["toolCallId"] for f in wire if f["sessionUpdate"] == "tool_call")
            progress = [f for f in wire if f["toolCallId"] == parent and "aelix/subagents" in f["metaKeys"]]
            assert any(f["status"] == "in_progress" for f in progress) and progress[-1]["status"] == "completed", progress
            checks.append("adapter_sends_child_progress_as_parent_tool_call_update_meta")
            row = next(r for r in host.rows(delegating) if r["id"] == parent)
            stored = json.loads(row["content"])
            children = stored["_meta"]["aelix/subagents"]["children"]
            assert row["type"] == "acp_tool_call" and children[0]["profile"] == "reviewer" and children[0]["state"] == "done", stored
            listed = await host.api(route + "/messages")
            listed_text = json.dumps(listed)
            assert "aelix/subagents" in listed_text and "reviewer" in listed_text
            observed["delegation"] = {"progressFrames": len(progress), "storedChild": {k: children[0][k] for k in ("profile", "state", "event")},
                                      "storedStatus": row["status"], "listedByMessagesApi": True}
            checks.append("host_persists_and_serves_parent_tool_card_with_child_meta")

            # Lock: native holds the session file while the host resumes it.
            await host.api(route + "/messages", "POST", {"content": "Respond"})
            await host.idle(route)
            with contextlib.closing(sqlite3.connect(root / "acp/sessions.sqlite3")) as db:
                native_file = db.execute("SELECT file FROM sessions WHERE file IS NOT NULL ORDER BY updated DESC").fetchone()[0]
            await host.stop()
            holder = subprocess.Popen([str(args.aelix), "--mode", "rpc", "--offline", "--no-extensions", "--session", native_file],
                                      cwd=root / "workspace", env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL)
            try:
                holder.stdin.write(b'{"id":"ready","type":"get_state"}\n')
                holder.stdin.flush()
                assert b'"ready"' in await asyncio.to_thread(holder.stdout.readline)
                await host.start()
                mark = len(frames(log))
                status, _ = await asyncio.to_thread(http, host.port, route + "/messages", "POST", {"content": "WHILE_LOCKED"})
                await host.idle(route)
                after = frames(log)[mark:]
                loads = [f for f in after if f["method"] == "session/load"]
                refused = [f for f in after if f["errorCode"] == -33001]
                assert loads and refused and refused[0]["errorKind"] == "session_locked", after
                assert not [f for f in after if f["method"] == "session/new"], "Host replaced the locked session"
                assert model.count("WHILE_LOCKED") == 0
                tips = [r for r in host.rows(delegating) if "-33001" in r["content"] or "session_locked" in r["content"]]
                observed["lock"] = {"sendStatus": status, "loads": len(loads), "lockedReplies": len(refused),
                                    "newSessionFallback": False, "hostRecordedStructuredError": bool(tips)}
                assert tips, "Host did not record the structured lock error"
                checks.append("native_lock_reaches_host_as_structured_error_without_new_session")
            finally:
                holder.stdin.close()
                holder.wait(10)
            await host.api(route + "/messages", "POST", {"content": "AFTER_UNLOCK"})
            await host.idle(route)
            assert model.count("AFTER_UNLOCK") == 1
            checks.append("released_lock_resumes_the_same_conversation")
            return {"label": label, "checksPassed": checks, "observed": observed}
        finally:
            await host.stop()
            model.server.close()


async def run(args):
    report = {"host": {"path": str(args.host), "sha256": hashlib.sha256(args.host.read_bytes()).hexdigest()},
              "native": str(args.aelix), "acpSource": str(args.acp_source), "model": "synthetic loopback",
              "renderer": "not run", "scenarios": [await scenario(args, args.acp_source, "candidate")]}
    if args.control_acp_source:
        report["scenarios"].append(await scenario(args, args.control_acp_source, "control"))
    return report


def main():
    if len(sys.argv) > 3 and sys.argv[1] == "tap" and sys.argv[3] == "--":
        tap(sys.argv[2], sys.argv[4:])
        return
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", type=Path, required=True)
    parser.add_argument("--aelix", type=Path, required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--acp-source", type=Path, default=ROOT / "src")
    parser.add_argument("--control-acp-source", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = asyncio.run(run(args))
    text = json.dumps(report, indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
