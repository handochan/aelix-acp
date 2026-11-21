"""Check ACP #44 with the unmodified Studio wheels and a loopback provider."""

import argparse
import asyncio
import base64
import json
import os
import tempfile
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace
from unittest.mock import patch

from spike_image_window import EXTENSION, META, MIB, Probe, ProbeServer, digest

from aelix_acp import image_policy
from aelix_acp.runtime import host_bytes


def key(number):
    return f"019b0000-0000-7000-8000-{number:012x}"


class HostFixture(ProbeServer):
    def __init__(self, args, root):
        super().__init__(args)
        self.root = root
        self.acquired = {}
        self.reads = 0
        self.reverse_hold = None

    async def client_request(self, method, params, **kwargs):
        assert method == host_bytes.METHOD
        assert params["bindingId"] == self.binding and params["sessionId"] == self.session_id
        ref, offset = params["ref"], params["offset"]
        data = (self.root / "images" / self.grants[ref]).read_bytes()
        self.reads += 1
        if self.reverse_hold is not None:
            entered, release = self.reverse_hold
            entered.set()
            await release.wait()
        reader = self.acquired.setdefault(ref, params["readerId"])
        assert reader == params["readerId"]
        if params.get("finish"):
            assert offset == len(data)
            return {"released": True}
        chunk = data[offset : offset + params["maxBytes"]]
        return {
            "offset": offset,
            "byteLength": len(data),
            "sha256": digest(data),
            "eof": offset + len(chunk) == len(data),
            "data": base64.b64encode(chunk).decode("ascii"),
        }


class RunningImages(Probe):
    async def startup(self):
        self.listener = await asyncio.start_server(self.endpoint, "127.0.0.1", 0)
        endpoint = f"http://127.0.0.1:{self.listener.sockets[0].getsockname()[1]}/v1"
        model = {
            "id": "one",
            "name": "Fixture",
            "input": ["text", "image"],
            "contextWindow": 196608,
            "maxTokens": 4096,
        }
        (self.root / "agent/models.json").write_text(
            json.dumps(
                {
                    "providers": {
                        "fixture": {
                            "baseUrl": endpoint,
                            "api": "openai-completions",
                            "apiKey": "fixture-key",
                            "models": [model, {**model, "id": "two"}],
                        }
                    }
                }
            )
        )
        (self.root / "agent/settings.json").write_text(
            json.dumps(
                {
                    "defaultProvider": "fixture",
                    "defaultModel": "one",
                    "compaction": {"enabled": False, "keepRecentTokens": 0},
                }
            )
        )
        self.server = HostFixture(
            SimpleNamespace(
                aelix=str(self.executable),
                state_dir=str(self.root / "state"),
                profile="coding",
                provider=None,
                model=None,
                extension=[str(EXTENSION)],
                permission_mode="yolo",
                trust_project=False,
                turn_timeout=60,
            ),
            self.root,
        )
        self.server.image_policies = (
            {
                "route": ["fixture", "one", "openai-completions", endpoint],
                "imageBytes": 3_750_000,
                "sidePixels": 8000,
                "rawBytes": 8 * MIB,
                "count": 20,
                "bodyBytes": 12 * MIB,
                "contextTokens": 196608,
                "imageTokens": 8192,
                "outputTokens": 4096,
            },
        )
        response = await self.server.dispatch(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {
                    "_meta": {META: {"version": 1, "notifications": False}, host_bytes.META_KEY: {"version": 1}}
                },
            },
        )
        assert response["agentCapabilities"]["_meta"][host_bytes.META_KEY] == {"version": 1}
        self.binding = self.server.binding = "wire-binding"
        created = await self.server.dispatch("session/new", self.open_params())
        self.session_id = self.server.session_id = created["sessionId"]
        for name in ("autoRetryEnabled", "autoCompactionEnabled"):
            await self.config(name, "false")
        assert (Path(self.worker.socket_dir) / "image-ready").exists()

    async def spawn(self, *args, **kwargs):
        # Reuse the spike's write tap without replacing the product's pipe limit.
        assert kwargs["limit"] == 65536
        self.reader_limit = kwargs["limit"]
        process = await super().spawn(*args, **kwargs)
        read = process.stdout.read
        frame_size = 0
        prefix = bytearray()

        async def measured_read(size):
            nonlocal frame_size, prefix
            raw = await read(size)
            newline_count = raw.count(b"\n")
            for index, line in enumerate(raw.split(b"\n")):
                if len(prefix) < 128:
                    prefix.extend(line[: 128 - len(prefix)])
                frame_size += len(line)
                if index < newline_count:
                    kind = (
                        "agent_end"
                        if prefix.startswith(b'{"messages":')
                        else "response"
                        if b'"response"' in prefix
                        else "event"
                    )
                    self.native_lines.append((kind, frame_size + 1))
                    frame_size, prefix = 0, bytearray()
            return raw

        process.stdout.read = measured_read
        return process

    async def relay(self, method, payload):
        return await self.server.dispatch(
            "_aelix/session/" + method,
            {"sessionId": self.session_id, "bindingId": self.binding, "payload": payload},
        )

    async def running_turn(self):
        for _ in range(200):
            state = await self.relay("input/state", {})
            if state["active"]:
                return state["turnId"]
            await asyncio.sleep(0.01)
        raise AssertionError("No active turn")

    async def assert_queue_order(self, mode, payload, text_id, text):
        sent = self.native_writes["steer" if mode == "steer" else "follow_up"]
        entered, release = asyncio.Event(), asyncio.Event()
        self.server.reverse_hold = (entered, release)
        image = asyncio.create_task(self.relay(f"input/{mode}-images", payload))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            later = asyncio.create_task(
                self.relay(
                    f"input/{mode}", {"turnId": payload["turnId"], "requestId": key(text_id), "message": text}
                )
            )
            await asyncio.sleep(0)
            assert self.native_writes["steer" if mode == "steer" else "follow_up"] == sent and not later.done()
        finally:
            self.server.reverse_hold = None
            release.set()
        receipt, text_receipt = await asyncio.gather(image, later)
        assert receipt["accepted"] and text_receipt["accepted"]
        return receipt

    async def exercise(self):
        # Fill real file history with 32 MiB raw, then add real tool results.
        for index in range(8):
            await self.prompt(f"history batch {index}", ["a.png", "b.png"])
        assert self.requests[-1]["rawBytes"] == 8 * MIB
        assert (await self.state())["omittedCount"] == 12
        self.hold_tool_request = True
        task = asyncio.create_task(self.prompt("run tool"))
        await asyncio.wait_for(self.arrivals.get(), 10)
        self.server.grants = {
            "steer": "steer-before.png",
            "followup": "f0.png",
            "large1": "large-a.png",
            "large2": "large-b.png",
            "late": "f1.png",
        }
        turn = await self.running_turn()
        payload = {"turnId": turn, "requestId": key(1), "message": " ", "grants": ["steer"]}
        accepted = await self.assert_queue_order("steer", payload, 11, "text after steer image")
        assert accepted["captionAdded"] and accepted["inputKind"] == "pixels"
        reads = self.server.reads
        assert await self.relay("input/steer-images", payload) == accepted and self.server.reads == reads
        assert (await self.relay("requests/lookup", {"requestId": key(1)}))["result"] == accepted
        follow_up = {**payload, "requestId": key(2), "message": "queued follow-up", "grants": ["followup"]}
        assert not (await self.assert_queue_order("follow-up", follow_up, 12, "text after follow-up image"))[
            "captionAdded"
        ]
        too_large = {**payload, "requestId": key(3), "grants": ["large1", "large2"]}
        assert await self.relay("input/steer-images", too_large) == {
            "accepted": False,
            "reason": "native-line-limit",
        }
        assert self.native_writes["steer"] == 2
        self.gate.set()
        self.additional_tool_calls = 2
        await task
        assert any(image_policy.CAPTION in row["blocks"] for req in self.requests for row in req["messages"])
        assert any("f0.png" in req["images"] for req in self.requests)
        provider_rows = [req["messages"] for req in self.requests]

        def first(text):
            return next(
                (mi, ri)
                for mi, rows in enumerate(provider_rows)
                for ri, row in enumerate(rows)
                if text in row["blocks"]
            )

        tool = next(
            (mi, ri)
            for mi, rows in enumerate(provider_rows)
            for ri, row in enumerate(rows)
            if row["role"] == "tool"
        )
        assert tool < first(image_policy.CAPTION) < first("text after steer image")
        assert first("text after steer image")[0] < first("queued follow-up")[0]
        assert first("queued follow-up") < first("text after follow-up image")
        assert self.server.tool_image_base64_bytes == []
        # Post-projection injection and actual HTTP rejection must both permit
        # the next explicit input in the same file/session.
        received = len(self.requests)
        await self.prompt("reinsert after context", fail=True)
        assert len(self.requests) == received
        self.fail_next = (400, "private-provider-error-fixture", None)
        await self.prompt("explicit provider rejection", fail=True)
        assert self.requests[-1]["status"] == 400
        assert (await self.state())["status"] == "unavailable"
        await self.prompt("explicit recovery")
        assert self.requests[-1]["status"] == 200
        self.results["largeHistoryRPCMessages"] = len((await self.worker.rpc("get_messages"))["messages"])

        # Closing and loading must resume the original context.
        await self.server.dispatch("session/close", {"sessionId": self.session_id})
        self.binding = self.server.binding = "resume-binding"
        await self.server.dispatch("session/load", {"sessionId": self.session_id, **self.open_params()})
        assert (await self.state())["status"] == "unavailable"
        await self.prompt("first resume")
        assert self.requests[-1]["placeholders"] and not self.worker.rpc_closed
        await self.config("model", "fixture/two")
        assert (await self.state())["reason"] == "model-unverified"
        await self.prompt("unverified model")
        assert self.requests[-1]["images"] == []
        await self.config("model", "fixture/one")

        self.gate.clear()
        task = asyncio.create_task(self.prompt("run tool", stop_reason="cancelled"))
        await asyncio.wait_for(self.arrivals.get(), 10)
        turn = await self.running_turn()
        late = {**payload, "turnId": turn, "requestId": key(4), "grants": ["late"]}
        late_receipt = await self.relay("input/follow-up-images", late)
        start = perf_counter()
        await self.server.dispatch("session/cancel", {"sessionId": self.session_id})
        await task
        self.results["stopWithLargeHistoryMs"] = round((perf_counter() - start) * 1000)
        assert (await self.state())["status"] == "unavailable" and not self.worker.rpc_closed
        largest = max(size for kind, size in self.native_lines if kind == "agent_end")
        assert largest > 46 * MIB, largest
        self.gate.set()
        await self.prompt("after Stop")
        assert self.requests[-1]["status"] == 200
        assert (await self.relay("requests/lookup", {"requestId": key(4)}))["result"] == late_receipt
        assert self.native_writes["follow_up"] == 3

        # Small images hit the count limit independently of the raw budget.
        for index in range(6):
            await self.prompt(f"count boundary {index}", ["f0.png", "f1.png", "f2.png", "f3.png"])
        assert len(self.requests[-1]["images"]) == 20
        sample = await self.state()
        assert sample["keptCount"] == 20 and "image_count" in sample["reasons"]

    async def run_live(self, stop_trials):
        self.fixtures(overflow=True)
        # Use the large tool case once at a time so the pinned window is
        # driven by real tool results with model requests still below 8 MiB.
        (self.root / "overflow-tool").unlink()
        env = {
            name: os.environ[name]
            for name in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")
            if name in os.environ
        }
        env.update(
            HOME=str(self.root),
            USERPROFILE=str(self.root),
            AELIX_CODING_AGENT_DIR=str(self.root / "agent"),
            OPENAI_LOG="debug",
            ANTHROPIC_LOG="debug",
            GEMINI_NEXT_GEN_API_LOG="debug",
        )
        with patch.dict(os.environ, env, clear=True), patch.object(asyncio, "create_subprocess_exec", self.spawn):
            try:
                await self.startup()
                await self.exercise()
            finally:
                self.gate.set()
                if hasattr(self, "server"):
                    await asyncio.gather(*(worker.close() for worker in list(self.server.sessions.values())))
                    await asyncio.gather(*self.server.cleanup_tasks)
                    self.server.db.close()
                self.listener.close()
                await self.listener.wait_closed()
        if stop_trials:
            from stop_pipe_probe import measure

            self.results["pipeTrials"] = await measure(stop_trials)
        assert all(
            row["rawBytes"] <= 8 * MIB and len(row["images"]) <= 20 and row["httpBytes"] <= 12 * MIB
            for row in self.requests
        )
        return {
            **self.results,
            "httpRequests": len(self.requests),
            "maxProviderBytes": max(r["httpBytes"] for r in self.requests),
            "maxAgentEndBytes": max(size for kind, size in self.native_lines if kind == "agent_end"),
            "queueWrites": {k: self.native_writes[k] for k in ("steer", "follow_up")},
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aelix", type=Path, required=True)
    parser.add_argument("--stop-trials", type=int, default=0)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="acp-running-images-") as directory:
        result = asyncio.run(
            RunningImages(Path(directory).resolve(), args.aelix.resolve()).run_live(args.stop_trials)
        )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
