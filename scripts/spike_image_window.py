"""Exercise ACP #43 against an installed native wheel and a localhost HTTP fixture."""
import argparse
import asyncio
import base64
from collections import Counter, defaultdict
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import struct
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
import zlib

from PIL import Image

from aelix_acp import session
from aelix_acp.runtime import session_relay
from aelix_acp.server import Server
from aelix_acp.wire.errors import ProtocolError

MIB = 1 << 20
META = session_relay.META_KEY
IMAGE_STATE = "_aelix/session/input/image-state"
PLACEHOLDER = "[Earlier image omitted from this request due to the image limit.]"
EXTENSION = Path(__file__).resolve().parent / "fixtures/image_window_extension.py"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def make_image(label, size=None):
    output = io.BytesIO()
    color = tuple(hashlib.sha256(label.encode()).digest()[:3])
    image = Image.new("RGB", (1, 1), color)
    image.save(output, "JPEG" if label.endswith(".jpg") else "PNG")
    sample = output.getvalue()
    if size is None:
        return sample
    # Pad a valid PNG using a private ancillary chunk, without changing dimensions.
    chunk = b"npAD" + bytes(size - len(sample) - 12)
    return (sample[:-12] + struct.pack(">I", len(chunk) - 4) + chunk
            + struct.pack(">I", zlib.crc32(chunk)) + sample[-12:])


def sse(model, *, tool_call_id=None):
    delta = {"role": "assistant", "content": "fixture response"}
    if tool_call_id:
        delta = {"role": "assistant", "tool_calls": [
            {"index": 0, "id": tool_call_id, "type": "function",
             "function": {"name": "fixture_image", "arguments": "{}"}}]}
    frames = []
    for update, finish in [(delta, None), ({}, "tool_calls" if tool_call_id else "stop")]:
        frame = {"id": "fixture", "object": "chat.completion.chunk", "created": 0, "model": model,
                 "choices": [{"index": 0, "delta": update, "finish_reason": finish}]}
        frames.append("data: " + json.dumps(frame) + "\n\n")
    return ("".join(frames) + "data: [DONE]\n\n").encode()


async def image_state(relay, payload):
    # Test registration uses ACP's actual binding check. Read one immutable
    # snapshot without contacting native, admitting work, or sending a notice.
    session_relay.require_fields(payload, set())
    if not relay.session.socket_dir or relay.session.rpc_closed:
        return {"status": "unavailable"}
    path = Path(relay.session.socket_dir) / "image-window.json"
    try:
        return {"status": "available", **json.loads(path.read_text(encoding="utf-8"))}
    except FileNotFoundError:
        return {"status": "unavailable"}


class ProbeServer(Server):
    def __init__(self, args):
        super().__init__(args)
        self.methods = Counter()
        self.tool_image_base64_bytes = []

    async def send(self, message):
        method = message.get("method")
        self.methods[method] += 1
        if method == "session/update":
            output = message["params"]["update"].get("rawOutput") or {}
            self.tool_image_base64_bytes.extend(len(block["data"]) for block in output.get("content", [])
                                                if block.get("type") == "image")


class Probe:
    def __init__(self, root, executable):
        self.root = root
        self.executable = executable
        self.images = {}
        self.labels = {}
        self.requests = []
        self.native_lines = []
        self.native_writes = Counter()
        self.arrivals = asyncio.Queue()
        self.gate = asyncio.Event()
        self.hold_tool_request = False
        self.additional_tool_calls = 0
        self.provider_cap = 20
        self.provider_raw_cap = 8 * MIB
        self.deny_auth = False
        self.results = {}
        self.native_spawn = asyncio.create_subprocess_exec
        self.reader_limit = session.FRAME_LIMIT
        self.fail_next = None
        self.native_env = {}

    def fixtures(self, *, overflow):
        (self.root / "agent").mkdir()
        book = self.root / "images"
        book.mkdir()
        sizes = {f"{label}.png": 2 * MIB for label in "abcd"}
        sizes["tool.png"] = MIB
        if overflow:
            (self.root / "overflow-tool").touch()
            sizes.update({"large-a.png": 3 * MIB, "large-b.png": 3 * MIB,
                          "large-tool.png": 13 * MIB // 4, "large-tool-added.png": 13 * MIB // 4})
        small = ["tool-before.png", "tool-added.png", "steer-before.png", "steer.jpg", "steer-added.png"]
        small += [f"{label}{i}.png" for label, count in (("f", 4), ("n", 9)) for i in range(count)]
        for label, size in [*sizes.items(), *((label, None) for label in small)]:
            self.add_image(label, size)
        self.policy()
        # Use the real bundled bridge and extend its setup only in this
        # temporary ACP package. The native wheel and source checkout are read-only.
        package = self.root / "acp-package"
        shutil.copytree(session.PACKAGE / "profiles", package / "profiles")
        if os.name == "nt":
            shutil.copy2(session.PACKAGE / "windows.py", package)
        original = (session.PACKAGE / "bridge.py").read_text(encoding="utf-8")
        (package / "bridge.py").write_text(original + f"""
from runpy import run_path
_original_setup = setup
def setup(api):
    run_path({str(EXTENSION)!r})["register_window"](api)
    _original_setup(api)
""", encoding="utf-8")
        return package

    def add_image(self, label, size):
        data = make_image(label, size)
        (self.root / "images" / label).write_bytes(data)
        mime = "image/jpeg" if label.endswith(".jpg") else "image/png"
        fingerprint = digest(data)
        self.images[label] = {"mime": mime, "sha256": fingerprint, "rawBytes": len(data)}
        self.labels[fingerprint] = label

    def policy(self, *, count=20, raw=8 * MIB):
        (self.root / "policy.json").write_text(json.dumps({"count": count, "rawBytes": raw}))

    def raw_image(self, mime, encoded):
        data = base64.b64decode(encoded, validate=True)
        label = self.labels[digest(data)]
        assert self.images[label]["mime"] == mime, (label, mime)
        return label, len(data)

    def summarize(self, messages, *, provider=False):
        rows = []
        sizes = []
        for message in messages:
            role = message["role"]
            content = message.get("content")
            parts = [{"type": "text", "text": content}] if isinstance(content, str) else content or []
            blocks = []
            for part in parts:
                if part["type"] == "image_url":
                    header, encoded = part["image_url"]["url"].split(",", 1)
                    mime, marker = header.removeprefix("data:").split(";")
                    assert marker == "base64"
                elif part["type"] == "image":
                    mime, encoded = part.get("mimeType") or part["mime_type"], part["data"]
                else:
                    if role not in ("system", "developer"):
                        blocks.append(part.get("text", part["type"]))
                    continue
                label, size = self.raw_image(mime, encoded)
                blocks.append({"image": label})
                sizes.append((label, size, len(encoded)))
            row = {"role": role, "blocks": blocks}
            call = message.get("tool_call_id") or message.get("toolCallId")
            if call:
                row["toolCallId"] = call
            if provider and message.get("tool_calls"):
                row["calls"] = [tool["id"] for tool in message["tool_calls"]]
            rows.append(row)
        return rows, sizes

    async def endpoint(self, reader, writer):
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            length = next(int(line.split(b":", 1)[1]) for line in header.splitlines()
                          if line.lower().startswith(b"content-length:"))
            body = await reader.readexactly(length)
            request = json.loads(body)
            rows, sizes = self.summarize(request["messages"], provider=True)
            raw = sum(size for _, size, _ in sizes)
            images = [label for label, _, _ in sizes]
            placeholders = sum(block.count(PLACEHOLDER) for row in rows
                               for block in row["blocks"] if isinstance(block, str))
            tool_call_id = None
            if self.hold_tool_request and rows[-1]["blocks"][0] == "run tool":
                tool_call_id = "call_image"
                self.arrivals.put_nowait(None)
                await self.gate.wait()
            elif self.additional_tool_calls:
                tool_call_id = f"call_image_{self.additional_tool_calls}"
                self.additional_tool_calls -= 1
            error_code = None
            if self.fail_next:
                status, error, error_code = self.fail_next
                self.fail_next = None
            elif self.deny_auth:
                status, error = 401, "fixture credentials rejected"
            elif len(images) > self.provider_cap:
                status, error = 400, "fixture image count exceeded"
            elif raw > self.provider_raw_cap:
                status, error = 400, "fixture image bytes exceeded"
            else:
                status, error = 200, None
            wire_id = f"http-{len(self.requests) + 1}"
            self.requests.append({"status": status, "error": error, "httpBytes": length, "rawBytes": raw,
                                  "providerRequestId": wire_id, "model": request["model"],
                                  "base64Bytes": sum(b64 for _, _, b64 in sizes), "images": images,
                                  "placeholders": placeholders, "messages": rows})
            response = (json.dumps({"error": {"message": error, "type": "invalid_request_error",
                                              "code": error_code}}).encode()
                        if error else sse(request["model"], tool_call_id=tool_call_id))
            media = "application/json" if error else "text/event-stream"
            writer.write(f"HTTP/1.1 {status} Fixture\r\nContent-Type: {media}\r\nConnection: close\r\n"
                         f"X-Fixture-Request: {wire_id}\r\nContent-Length: {len(response)}\r\n\r\n".encode()
                         + response)
            await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    async def startup(self):
        self.listener = await asyncio.start_server(self.endpoint, "127.0.0.1", 0)
        port = self.listener.sockets[0].getsockname()[1]
        model = {"id": "one", "name": "Fixture", "input": ["text", "image"],
                 "contextWindow": 196608, "maxTokens": 4096}
        base_url = f"http://127.0.0.1:{port}/v1"
        (self.root / "agent/models.json").write_text(json.dumps({"providers": {"fixture": {
            "baseUrl": base_url, "api": "openai-completions", "apiKey": "fixture-key",
            "models": [model, {**model, "id": "two", "name": "Alternate Fixture"}]}}}))
        (self.root / "agent/settings.json").write_text(json.dumps({
            "defaultProvider": "fixture", "defaultModel": "one",
            "compaction": {"enabled": False, "keepRecentTokens": 0}}))
        self.server = ProbeServer(SimpleNamespace(aelix=str(self.executable), state_dir=str(self.root / "state"),
            profile="coding", provider=None, model=None, extension=[str(EXTENSION)], permission_mode="yolo",
            trust_project=False, turn_timeout=40))
        response = await self.server.dispatch("initialize", {"protocolVersion": 1,
            "clientCapabilities": {"_meta": {META: {"version": 1, "notifications": False}}}})
        advertisement = response["agentCapabilities"]["_meta"][META]
        assert IMAGE_STATE in advertisement["requests"] and advertisement["notifications"] == []
        self.results["relayAdvertisement"] = advertisement
        self.binding = "before-reconnect"
        created = await self.server.dispatch("session/new", self.open_params())
        self.session_id = created["sessionId"]
        assert created["_meta"][META]["bindingId"] == self.binding
        for name in ("autoRetryEnabled", "autoCompactionEnabled"):
            await self.config(name, "false")

    async def config(self, name, value):
        return await self.server.dispatch("session/set_config_option", {
            "sessionId": self.session_id, "configId": name, "value": value})

    async def spawn(self, *args, **kwargs):
        kwargs["limit"] = self.reader_limit
        process = await self.native_spawn(*args, **kwargs)
        write, readline = process.stdin.write, process.stdout.readline

        def tap_write(data):
            if data != b"\0":  # Windows worker gate
                self.native_writes[json.loads(data)["type"]] += 1
            write(data)

        async def tap_readline():
            line = await readline()
            if line:
                frame = json.loads(line)
                kind = frame["type"]
                if kind == "response":
                    kind += ":" + frame["command"]
                self.native_lines.append((kind, len(line)))
            return line

        process.stdin.write = tap_write
        process.stdout.readline = tap_readline
        return process

    def open_params(self):
        return {"cwd": str(self.root), "mcpServers": [], "_meta": {META: {"bindingId": self.binding}}}

    @property
    def worker(self):
        return self.server.sessions[self.session_id]

    async def state(self, binding=None):
        return await self.server.dispatch(IMAGE_STATE, {"sessionId": self.session_id,
            "bindingId": binding or self.binding, "payload": {}})

    def encoded(self, label):
        info = self.images[label]
        data = base64.b64encode((self.root / "images" / label).read_bytes()).decode("ascii")
        return info["mime"], data

    async def queue(self, command, text, labels):
        images = [{"mimeType": mime, "data": data} for mime, data in map(self.encoded, labels)]
        await self.worker.rpc(command, message=text, images=images)

    async def prompt(self, text, labels=(), *, fail=False, anchor=None, stop_reason="end_turn"):
        blocks = [{"type": "text", "text": text}]
        blocks += [{"type": "image", "mimeType": mime, "data": data}
                   for mime, data in map(self.encoded, labels)]
        try:
            answer = await self.server.dispatch("session/prompt", {
                "sessionId": self.session_id, "prompt": blocks,
                "_meta": {"aelix/forkHead": anchor} if anchor else {}})
        except ProtocolError as exc:
            if not fail:
                raise
            assert exc.code == -33002 and exc.data["retryable"] is False, (exc.code, exc.data)
            return exc.code
        assert not fail and answer == {"stopReason": stop_reason}, answer
        return answer

    def assert_request(self, index, names, *, omitted, status=200):
        actual = self.requests[index]
        assert actual["images"] == names, (index + 1, actual["images"], names)
        assert actual["rawBytes"] == sum(self.images[name]["rawBytes"] for name in names)
        assert actual["base64Bytes"] == sum(4 * ((self.images[name]["rawBytes"] + 2) // 3) for name in names)
        assert actual["placeholders"] == omitted, (index + 1, actual["placeholders"], omitted)
        assert actual["status"] == status, actual
        assert actual["rawBytes"] <= 8 * MIB and len(names) <= 20
        return actual

    async def assert_state(self, request, sequence, omitted, reason=None):
        before = self.native_writes.copy()
        actual = await self.state()
        assert actual == {"status": "available", "requestSequence": sequence,
            "workerGeneration": actual["workerGeneration"], "turnSequence": actual["turnSequence"],
            "providerRequestId": request["providerRequestId"], "model": f"fixture/{request['model']}",
            "keptCount": len(request["images"]), "keptRawBytes": request["rawBytes"], "omittedCount": omitted,
            "reasons": {reason: omitted} if omitted else {}}, actual
        assert actual["workerGeneration"] and actual["turnSequence"] > 0
        assert await self.state() == actual
        assert self.native_writes == before
        return actual

    def transcript_path(self):
        return Path(self.server.db.execute("SELECT file FROM sessions WHERE id=?", (self.session_id,)).fetchone()[0])

    def disk(self):
        return self.transcript_path().read_bytes().splitlines()

    async def originals(self, lines, labels):
        disk = [record["message"] for row in lines
                if (record := json.loads(row))["type"] == "message"]
        memory = (await self.worker.rpc("get_messages"))["messages"]
        summaries = []
        for messages in (disk, memory):
            assert PLACEHOLDER not in json.dumps(messages)
            rows, sizes = self.summarize(messages)
            actual = [label for label, _, _ in sizes]
            assert actual == labels, (actual, labels)
            summaries.append(rows)
        # Error assistants may have a different persistence policy. Compare
        # the image-bearing and tool-result messages across both surfaces.
        disk_rows, memory_rows = ([row for row in rows if row["role"] in ("user", "toolResult")]
                                  for rows in summaries)
        assert disk_rows == memory_rows, summaries
        return summaries[0]

    async def exercise(self):
        assert await self.state() == {"status": "unavailable"}
        a, b, c, d = big = [f"{label}.png" for label in "abcd"]
        tool = ["tool.png", "tool-added.png"]
        steer = ["steer.jpg", "steer-added.png"]
        follow = [f"f{i}.png" for i in range(4)]
        stored = big + tool + steer + follow
        await self.prompt("first normal", [a, b])
        await self.prompt("second normal", [c])
        first_entries = self.disk()
        stdout_start = len(self.native_lines)
        self.hold_tool_request = True
        prompt = asyncio.create_task(self.prompt("run tool", [d]))
        try:
            await asyncio.wait_for(self.arrivals.get(), timeout=10)
            active = await self.state()
            assert active == {"status": "unavailable"}  # HTTP receipt is still outstanding.
            self.results["beforeResponseState"] = active
            await self.queue("steer", "steer", ["steer-before.png"])
            await self.queue("follow_up", "follow-up", follow)
        finally:
            self.gate.set()
            await prompt
        self.hold_tool_request = False
        assert len(self.requests) == 5, self.requests
        self.assert_request(0, [a, b], omitted=0)
        self.assert_request(1, [a, b, c], omitted=0)
        exact = self.assert_request(2, big, omitted=0)
        assert exact["rawBytes"] == 8 * MIB
        self.assert_request(3, (big + tool + steer)[1:], omitted=1)
        byte_limit = self.assert_request(4, stored[1:], omitted=1)
        self.results["byteLimitState"] = await self.assert_state(byte_limit, 5, 1, "raw_bytes")
        assert [row["role"] for row in byte_limit["messages"]] == [
            "system", "user", "assistant", "user", "assistant", "user", "assistant",
            "tool", "user", "user", "assistant", "user"]
        assert byte_limit["messages"][1]["blocks"] == ["first normal", PLACEHOLDER, {"image": b}]
        assert byte_limit["messages"][6]["calls"] == ["call_image"]
        assert byte_limit["messages"][7] == {"role": "tool", "toolCallId": "call_image",
                                               "blocks": ["tool before\ntool after"]}
        assert byte_limit["messages"][9]["blocks"] == ["steer", *({"image": name} for name in steer)]
        assert byte_limit["messages"][11]["blocks"] == ["follow-up", *({"image": name} for name in follow)]
        before = self.disk()
        assert before[:len(first_entries)] == first_entries and len(before) > len(first_entries)
        await self.originals(before, stored)
        queue_run = self.native_lines[stdout_start:]
        self.results["fileSession"] = {"initialEntries": len(first_entries), "afterQueuesEntries": len(before),
            "queueRunStdoutBytes": next(size for kind, size in queue_run if kind == "agent_end")}
        assert self.native_writes["steer"] == self.native_writes["follow_up"] == 1

        await self.server.dispatch("session/close", {"sessionId": self.session_id})
        self.binding = "after-reconnect"
        loaded = await self.server.dispatch("session/load", {**self.open_params(), "sessionId": self.session_id})
        assert loaded["_meta"][META]["bindingId"] == self.binding
        try:
            await self.state("before-reconnect")
        except ProtocolError as exc:
            assert exc.code == session_relay.SCOPE_DENIED
            self.results["staleBindingError"] = exc.code
        else:
            raise AssertionError("Old binding was accepted")
        assert await self.state() == {"status": "unavailable"}
        assert self.disk() == before
        await self.originals(before, stored)
        await self.prompt("resumed")
        resumed = self.assert_request(5, stored[1:], omitted=1)
        self.results["resumedState"] = await self.assert_state(resumed, 1, 1, "raw_bytes")
        assert self.disk()[:len(before)] == before

        batch = [f"n{i}.png" for i in range(9)]
        for phase, items in enumerate((batch[:4], batch[4:8], batch[8:], [batch[8]]), 1):
            await self.prompt(f"count batch {phase}", items)
            stored.extend(items)
            names = stored[1:][-20:]
            self.assert_request(5 + phase, names, omitted=len(stored) - len(names))
        capped = self.requests[-1]
        assert len(stored[1:]) == 21 and len(capped["images"]) == 20
        assert capped["images"][-2:] == [batch[8], batch[8]]
        self.results["countLimitState"] = await self.assert_state(capped, 5, 2, "image_count")

        # The provider lowers its cap while the bridge still has the old cap.
        # Observe a real HTTP error and a settled ACP failure before updating
        # the policy for a deliberate next prompt on the exact same file/worker.
        self.provider_cap = 19
        prior_writes = self.native_writes.copy()
        path, worker = self.transcript_path(), self.worker
        rejected = self.disk()
        error = await self.prompt("reject historical image count", fail=True)
        request = self.assert_request(10, stored[-20:], omitted=2, status=400)
        assert request["error"] == "fixture image count exceeded"
        assert len(self.requests) == 11 and self.native_writes["prompt"] == prior_writes["prompt"] + 1
        assert self.disk()[:len(rejected)] == rejected
        assert await self.state() == {"status": "unavailable"}
        self.results["providerRejection"] = {"acpError": error, "retryable": False}
        self.policy(count=19)
        await self.prompt("recover by explicit input")
        recovery = self.assert_request(11, stored[-19:], omitted=3)
        assert self.worker is worker and self.transcript_path() == path
        self.results["recoveredState"] = await self.assert_state(recovery, 7, 3, "image_count")

        self.provider_raw_cap = MIB
        self.policy(count=19, raw=MIB)
        await self.prompt("lower raw ceiling")
        smaller = self.assert_request(12, stored[5:], omitted=5)
        tool_result = next(row for row in smaller["messages"] if row["role"] == "tool")
        assert tool_result == {"role": "tool", "toolCallId": "call_image",
                               "blocks": ["tool before\n" + PLACEHOLDER + "\ntool after"]}
        self.results["lowerRawState"] = await self.assert_state(smaller, 8, 5, "raw_bytes")

        self.deny_auth = True
        auth_error = await self.prompt("separate auth negative control", fail=True)
        self.assert_request(13, stored[5:], omitted=5, status=401)
        assert len(self.requests) == 14 and self.worker is worker
        self.results["authNegativeControl"] = {"acpError": auth_error, "httpStatus": 401}
        self.deny_auth = False
        await self.prompt("after credentials restored", anchor="probe-verified-head")
        self.assert_request(14, stored[5:], omitted=5)
        assert self.native_writes["steer"] == self.native_writes["follow_up"] == 1

        parent = self.session_id
        parent_lines = self.disk()
        await self.originals(parent_lines, stored)
        self.binding = "fork-binding"
        child = await self.server.dispatch("session/fork", {**self.open_params(), "sessionId": parent,
            "_meta": {**self.open_params()["_meta"], "aelix/expectedHead": "probe-verified-head"}})
        self.session_id = child["sessionId"]
        assert self.session_id != parent and child["_meta"][META]["bindingId"] == self.binding
        assert await self.state() == {"status": "unavailable"}
        child_lines = self.disk()
        await self.originals(child_lines, stored)
        assert path.read_bytes().splitlines() == parent_lines
        await self.prompt("on fork")
        fork = self.assert_request(15, stored[5:], omitted=5)
        assert self.disk()[:len(child_lines)] == child_lines
        assert len(self.requests) == 16
        self.results["forkState"] = await self.assert_state(fork, 1, 5, "raw_bytes")
        assert self.server.methods.keys() <= {"session/update"}
        self.results["fileSession"].update({"parentEntries": len(parent_lines), "forkEntries": len(child_lines),
            "storedImageOccurrences": len(stored), "storedRawBytes": sum(self.images[name]["rawBytes"] for name in stored),
            "originalsVerifiedIn": ["memory", "jsonl", "load", "fork"]})

        await self.prompt("/compact")
        summary_requests = list(range(16, len(self.requests)))
        assert summary_requests
        for index in summary_requests:
            self.assert_request(index, [], omitted=0)
        assert any("[image]" in block for index in summary_requests
                   for row in self.requests[index]["messages"] for block in row["blocks"])
        self.results["compactionHttpRequests"] = len(summary_requests)
        assert any(kind == "compaction_end" for kind, _ in self.native_lines)
        assert await self.state() == {"status": "unavailable"}
        compacted = self.disk()
        assert compacted[:len(child_lines)] == child_lines
        active = (await self.worker.rpc("get_messages"))["messages"]
        assert not self.summarize(active)[1]
        await self.prompt("after compact")
        assert len(self.requests) == summary_requests[-1] + 2
        next_request = self.assert_request(-1, [], omitted=0)
        self.results["postCompactState"] = await self.assert_state(next_request, 2, 0)

    async def overflow(self):
        large_user = ["large-a.png", "large-b.png"]
        large_tool = ["large-tool.png", "large-tool-added.png"]
        self.hold_tool_request = True
        self.gate.set()
        worker = self.worker
        measuring = self.reader_limit > session.FRAME_LIMIT
        error = await self.prompt("run tool", large_user, fail=not measuring)
        assert len(self.requests) == 2
        self.assert_request(0, large_user, omitted=0)
        self.assert_request(1, large_tool, omitted=2)
        if measuring:
            assert not worker.rpc_closed
            agent_end = next(size for kind, size in self.native_lines if kind == "agent_end")
            assert agent_end > session.FRAME_LIMIT
            return {"agentEndBytes": agent_end}
        assert worker.rpc_closed and "limit" in worker.failure.lower(), worker.failure
        return {"acpError": error, "rpcClosed": worker.rpc_closed, "readerLimitBytes": session.FRAME_LIMIT}

    async def run(self, *, overflow=False):
        package = self.fixtures(overflow=overflow)
        env = {name: os.environ[name] for name in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")
               if name in os.environ}
        env.update(HOME=str(self.root), USERPROFILE=str(self.root),
                   AELIX_CODING_AGENT_DIR=str(self.root / "agent"),
                   AELIX_SETTINGS_PATH=str(self.root / "agent/settings.json"), **self.native_env)
        with patch.dict(os.environ, env, clear=True), \
             patch.object(session, "PACKAGE", package), \
             patch.object(asyncio, "create_subprocess_exec", self.spawn), \
             patch.dict(session_relay.REQUESTS, {IMAGE_STATE: image_state}):
            try:
                await self.startup()
                async with asyncio.timeout(90):
                    if overflow:
                        self.results.update(await self.overflow())
                    else:
                        await self.exercise()
            finally:
                self.gate.set()
                if hasattr(self, "server"):
                    await asyncio.gather(*(worker.close() for worker in list(self.server.sessions.values())))
                    await asyncio.gather(*self.server.cleanup_tasks)
                    self.server.db.close()
                self.listener.close()
                await self.listener.wait_closed()
        stdout = defaultdict(int)
        for kind, size in self.native_lines:
            stdout[kind] = max(stdout[kind], size)
        return {**self.results, "httpRequests": self.requests, "nativeStdoutMaxLineBytes": dict(stdout),
                "nativeWrites": dict(self.native_writes), "fixtureImages": self.images,
                "ordinaryToolUpdateImageBase64Bytes": self.server.tool_image_base64_bytes}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aelix", type=Path, required=True, help="Pinned native executable")
    parser.add_argument("--report", type=Path, required=True, help="JSON evidence destination")
    args = parser.parse_args()

    def run(*, overflow=False, capture=False):
        with tempfile.TemporaryDirectory(prefix="acp43-", dir="/tmp" if os.name != "nt" else None) as directory:
            probe = Probe(Path(directory).resolve(), args.aelix.resolve())
            if capture:
                probe.reader_limit = 32 * MIB
            return asyncio.run(probe.run(overflow=overflow))

    result = run()
    result["stdoutOverflow"] = {"measured": run(overflow=True, capture=True), "fixedReader": run(overflow=True)}
    args.report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
