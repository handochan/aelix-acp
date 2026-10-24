"""Opt-in real-provider acceptance of an installed ACP + public beta.2.

Uses synthetic task data with a real Anthropic endpoint. Reads only the named
key from an optional env file, never the user's native auth/session files.
Small real API charges apply. No API key or raw transcript enters the report.
"""
import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import tempfile
import time
import uuid


def credential(env_file):
    value = os.environ.get("ANTHROPIC_API_KEY")
    if not value and env_file:
        for line in Path(env_file).read_text().splitlines():
            match = re.match(r"\s*(?:export\s+)?ANTHROPIC_API_KEY\s*=\s*(.*?)\s*$", line)
            if match:
                value = match[1]
                if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                break
    if not value:
        raise RuntimeError("ANTHROPIC_API_KEY is required via environment or --env-file")
    return value


def isolated_env(root, key):
    env = {name: os.environ[name] for name in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP") if name in os.environ}
    env.update(HOME=str(root / "home"), USERPROFILE=str(root / "home"),
               AELIX_CODING_AGENT_DIR=str(root / "agent"),
               AELIX_SETTINGS_PATH=str(root / "agent/settings.json"),
               AELIX_AUTH_PATH=str(root / "agent/auth.json"),
               ANTHROPIC_API_KEY=key, PYTHONUNBUFFERED="1")
    return env


class Client:
    def __init__(self, args, root, key):
        self.args, self.root, self.key = args, root, key
        self.pending, self.updates, self.permissions = {}, [], []
        self.counter = 0
        self.management = False
        self.write_grant = None
        self.delta = asyncio.Event()
        self.stderr = bytearray()

    async def start(self):
        self.process = await asyncio.create_subprocess_exec(str(self.args.acp),
            "--aelix", str(self.args.aelix), "--state-dir", str(self.root / "state"),
            "--turn-timeout", "90", "--command-timeout", "45",
            "--aelix-arg=--no-context-files", "--aelix-arg=--no-skills",
            "--aelix-arg=--thinking", "--aelix-arg=off",
            cwd=self.root / "workspace", env=isolated_env(self.root, self.key),
            stdin=-1, stdout=-1, stderr=-1, limit=16 * 1024 * 1024)
        self.tasks = [asyncio.create_task(self.read()), asyncio.create_task(self.read_errors())]
        await self.request("initialize", {"protocolVersion": 1, "clientCapabilities": {}})

    async def read_errors(self):
        while chunk := await self.process.stderr.read(4096):
            self.stderr.extend(chunk)
            del self.stderr[:-65536]

    async def send(self, data):
        self.process.stdin.write((json.dumps(data) + "\n").encode())
        await self.process.stdin.drain()

    async def read(self):
        try:
            while line := await self.process.stdout.readline():
                frame = json.loads(line)
                if frame.get("method") == "session/request_permission":
                    call = frame["params"]["toolCall"]
                    raw = call.get("rawInput") or {}
                    allow = self.management and call["toolCallId"].startswith("management-")
                    if self.write_grant and call.get("title") == "write":
                        path, content = self.write_grant
                        actual = Path(raw.get("path", ""))
                        actual = actual if actual.is_absolute() else self.root / "workspace" / actual
                        allow = actual.resolve() == path.resolve() and raw.get("content") == content
                    self.permissions.append({"tool": call.get("title"), "allowed": bool(allow)})
                    await self.send({"jsonrpc": "2.0", "id": frame["id"], "result": {"outcome": {
                        "outcome": "selected", "optionId": "allow-once" if allow else "reject-once"}}})
                elif "method" in frame:
                    self.updates.append(frame)
                    update = frame.get("params", {}).get("update", {})
                    if update.get("sessionUpdate") == "agent_message_chunk" and update.get("content", {}).get("text"):
                        self.delta.set()
                else:
                    future = self.pending.get(frame.get("id"))
                    if future and not future.done():
                        future.set_result(frame)
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(RuntimeError("Installed ACP closed its stdout"))

    async def request(self, method, params, timeout=120):
        self.counter += 1
        request_id = self.counter
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            frame = await asyncio.wait_for(future, timeout)
            if "error" in frame:
                raise RuntimeError(json.dumps(frame["error"]))
            return frame["result"]
        finally:
            self.pending.pop(request_id, None)

    def text(self, start=0):
        return "".join(frame.get("params", {}).get("update", {}).get("content", {}).get("text", "")
                       for frame in self.updates[start:]
                       if frame.get("params", {}).get("update", {}).get("sessionUpdate") == "agent_message_chunk")

    async def prompt(self, text):
        start = len(self.updates)
        result = await self.request("session/prompt", {"sessionId": self.session,
            "prompt": [{"type": "text", "text": text}], "_meta": {"aelix/requestId": uuid.uuid4().hex}})
        return result, self.text(start)

    async def config(self, key, value):
        return await self.request("session/set_config_option", {"sessionId": self.session, "configId": key, "value": value})

    async def manage(self, action, arguments=None):
        self.management = True
        try:
            return await self.request("_aelix/manage", {"sessionId": self.session, "action": action, "arguments": arguments or {}})
        finally:
            self.management = False

    async def close(self):
        if self.process.returncode is None:
            self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), 12)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


async def run(args, key, report):
    with tempfile.TemporaryDirectory(prefix="acp-live-beta2-", dir=args.temp_dir) as temporary:
        root = Path(temporary).resolve()
        for name in ("home", "agent", "workspace"):
            (root / name).mkdir()
        (root / "agent/settings.json").write_text(json.dumps({"defaultProvider": "anthropic", "defaultModel": args.model}))
        (root / "agent/auth.json").write_text("{}")
        client = Client(args, root, key)
        await client.start()
        def passed(name, **detail):
            report["checks"].append({"name": name, "status": "passed", **detail})
            print("PASS " + name, flush=True)
        try:
            created = await client.request("session/new", {"cwd": str(root / "workspace"), "mcpServers": []})
            client.session = created["sessionId"]
            options = next(o for o in created["configOptions"] if o["id"] == "model")
            assert {"anthropic/" + args.model, "anthropic/" + args.switch_model} <= {o["value"] for o in options["options"]}
            passed("public_beta2_authenticated_model_catalog")
            await client.config("autoRetryEnabled", "false")
            await client.config("autoCompactionEnabled", "false")
            await client.manage("tools/set_active", {"names": []})
            memory = "LIVE-MEMORY-" + uuid.uuid4().hex[:12]
            result, text = await client.prompt("Remember this validation memory marker: " + memory + ". Reply exactly LIVE_FIRST_OK.")
            assert result["stopReason"] == "end_turn" and "LIVE_FIRST_OK" in text
            passed("actual_provider_turn", provider="anthropic", model=args.model)
            await client.config("model", "anthropic/" + args.switch_model)
            _, text = await client.prompt("Reply exactly LIVE_SWITCH_OK.")
            assert "LIVE_SWITCH_OK" in text
            passed("actual_second_model_turn", model=args.switch_model)
            await client.config("model", "anthropic/" + args.model)
            await client.config("thinking_level", "low")
            _, text = await client.prompt("Calculate 17 plus 29. Return only the number.")
            assert "46" in text
            passed("actual_reasoning_turn", level="low")
            await client.config("thinking_level", "off")

            await client.manage("tools/set_active", {"names": ["write"]})
            target = root / "workspace/approved.txt"
            client.write_grant = (target, "LIVE_APPROVED\n")
            _, _ = await client.prompt(f'Call the write tool exactly once with path={json.dumps(str(target))} and content="LIVE_APPROVED\\n". Do not use other tools. Then reply WRITE_DONE.')
            assert target.read_text() == "LIVE_APPROVED\n"
            assert any(p["tool"] == "write" and p["allowed"] for p in client.permissions)
            passed("actual_model_tool_approved_write")
            client.write_grant = None
            denied = root / "workspace/denied.txt"
            before = len(client.permissions)
            await client.prompt(f'Call write exactly once to create {denied} with content DENIED. The test will deny it. After denial stop, do not retry or use alternatives, reply DENIAL_OBSERVED.')
            assert not denied.exists() and any(not p["allowed"] for p in client.permissions[before:])
            passed("actual_model_tool_denial_no_file")
            await client.manage("tools/set_active", {"names": []})
            assert (await client.manage("resources/list"))["activeTools"] == []
            passed("empty_tool_selection")

            skill = root / "workspace/selected-skill"
            skill.mkdir()
            (skill / "SKILL.md").write_text("---\nname: live-skill\ndescription: Live acceptance fixture\n---\nRespond exactly LIVE_SKILL_OK when invoked.\n")
            template = root / "workspace/live-template.md"
            template.write_text("---\ndescription: Live acceptance template\n---\nReply exactly LIVE_TEMPLATE_$1.\n")
            await client.manage("resources/configure", {"inheritSkills": False, "skills": [str(skill)], "promptTemplates": [str(template)]})
            _, text = await client.prompt("/live-template OK")
            assert "LIVE_TEMPLATE_OK" in text
            passed("actual_prompt_template_turn_after_replacement")
            _, text = await client.prompt("/skill:live-skill")
            assert "LIVE_SKILL_OK" in text
            passed("actual_skill_command_turn")
            await client.manage("resources/configure", {"inheritSkills": False, "skills": []})
            try:
                await client.prompt("/skill:live-skill")
            except RuntimeError as exc:
                assert json.loads(str(exc))["code"] == -32601
            else:
                raise AssertionError("Disabled skill command still accepted")
            passed("disabled_skill_command_rejected")

            marker = root / "workspace/extension-loaded"
            extension = root / "workspace/live_extension.py"
            extension.write_text("from pathlib import Path\ndef setup(api): Path(" + repr(str(marker)) + ").touch()\n")
            await client.manage("extensions/action", {"action": "enable", "target": str(extension)})
            assert marker.exists()
            marker.unlink()
            await client.manage("extensions/action", {"action": "disable", "target": str(extension)})
            assert not marker.exists()
            passed("actual_extension_enable_disable_replacement")

            content = ("---\nname: live-profile\ndescription: Isolated live fixture\nprovider: anthropic\nmodel: " + args.model +
                       "\nthinking: 'off'\ntools: [read]\ninherit_skills: false\ninherit_extensions: false\nsystem_prompt: append\n---\n"
                       "When asked LIVE_PROFILE_IDENTITY, respond exactly LIVE_PROFILE_OK.\n")
            await client.manage("agents/save", {"name": "live-profile", "content": content})
            await client.manage("agents/use", {"name": "live-profile"})
            _, text = await client.prompt("LIVE_PROFILE_IDENTITY")
            assert "LIVE_PROFILE_OK" in text
            try:
                await client.manage("tools/set_active", {"names": ["write"]})
            except RuntimeError:
                pass
            else:
                raise AssertionError("Profile launch grant was widened")
            passed("actual_native_profile_turn_and_grant_ceiling")

            client.delta.clear()
            task = asyncio.create_task(client.prompt("Write a numbered list of 2000 distinct short sentences, one per line. Start immediately at 1."))
            try:
                await asyncio.wait_for(client.delta.wait(), 30)
                started = time.monotonic()
                await client.send({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": client.session}})
                result, _ = await asyncio.wait_for(task, 15)
                assert result["stopReason"] == "cancelled"
                passed("actual_provider_stream_cancel", seconds=round(time.monotonic() - started, 3))
            finally:
                if not task.done():
                    await client.send({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": client.session}})
                await asyncio.gather(task, return_exceptions=True)
            _, text = await client.prompt("Reply exactly LIVE_AFTER_CANCEL_OK.")
            assert "LIVE_AFTER_CANCEL_OK" in text
            passed("actual_provider_turn_after_cancel")

            session_id = client.session
            await client.close()
            client = Client(args, root, key)
            await client.start()
            loaded = await client.request("session/load", {"cwd": str(root / "workspace"), "sessionId": session_id, "mcpServers": []})
            client.session = session_id
            assert memory in json.dumps(client.updates)
            _, text = await client.prompt("Return only the validation memory marker from the first user turn. It starts LIVE-MEMORY-.")
            assert memory in text
            assert (await client.manage("resources/list"))["activeTools"] == ["read"]
            passed("actual_provider_memory_after_adapter_restart", model=loaded["models"]["currentModelId"])
            _, state = await client.prompt("/session")
            transcript = Path(json.loads(state)["sessionFile"])
            messages = [json.loads(line).get("message", {}) for line in transcript.read_text().splitlines()]
            assistants = [m for m in messages if m.get("role") == "assistant"]
            seen = sorted({m.get("model") for m in assistants if m.get("model")})
            assert args.model in seen and args.switch_model in seen
            usages = [m.get("usage", {}) for m in assistants]
            assert any(u.get("output", 0) > 0 for u in usages)
            report["actualNativeTranscript"] = {"assistantMessages": len(assistants), "models": seen,
                "inputTokens": sum(u.get("input", 0) for u in usages), "outputTokens": sum(u.get("output", 0) for u in usages),
                "reportedCostUsd": round(sum(u.get("cost", {}).get("total", 0) for u in usages), 6)}
            passed("native_transcript_records_both_real_models_and_usage")
            await client.manage("sessions/close")
        finally:
            await client.close()
            report["isolatedTemporaryDataRemoved"] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--acp", type=lambda p: Path(p).absolute(), required=True)
    parser.add_argument("--aelix", type=lambda p: Path(p).absolute(), required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--model", default="claude-haiku-4-5")
    parser.add_argument("--switch-model", default="claude-sonnet-4-5")
    parser.add_argument("--temp-dir", type=Path, default=Path(".local"))
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    args.temp_dir.mkdir(parents=True, exist_ok=True)
    key = credential(args.env_file)
    report = {"schema": "aelix-acp.beta2-real-provider/1", "date": time.strftime("%Y-%m-%d"),
              "runtimeProfile": "beta2", "provider": "anthropic", "route": "native builtin https://api.anthropic.com; no base URL override",
              "keySource": "existing environment or explicit env file; value never recorded", "oauthTested": False,
              "syntheticModelEndpoint": False, "checks": [], "passed": False,
              "distributionVersions": {name: importlib.metadata.version(name) for name in
                  ("aelix", "aelix-ai", "aelix-agent-core", "aelix-coding-agent", "aelix-acp", "anthropic")}}
    for name in ("aelix", "aelix-ai", "aelix-agent-core", "aelix-coding-agent"):
        native = importlib.metadata.distribution(name)
        if native.version != "0.1.0b2" or native.read_text("direct_url.json") is not None:
            raise SystemExit("Live beta.2 acceptance requires the four public wheels")
    installed = importlib.metadata.distribution("aelix-acp")
    for source in Path("src/aelix_acp").rglob("*.py"):
        if source.read_bytes() != Path(installed.locate_file(source.relative_to("src"))).read_bytes():
            raise SystemExit("Installed ACP does not match this checkout: " + str(source))
    report["installedAdapterMatchesSource"] = True
    started = time.monotonic()
    try:
        asyncio.run(run(args, key, report))
        report["passed"] = True
    except Exception as exc:
        report["failure"] = {"type": type(exc).__name__, "message": str(exc).replace(key, "[REDACTED]")[:2000]}
        raise SystemExit("Live validation failed; inspect the sanitized JSON report") from None
    finally:
        report["seconds"] = round(time.monotonic() - started, 2)
        report["adapterSourceSha256"] = {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path("src/aelix_acp").rglob("*.py"))}
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
