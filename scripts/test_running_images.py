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

from spike_image_window import EXTENSION, META, MIB, Probe, ProbeServer, digest, sse
from spike_image_window_review import StderrCapture

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
        # Keep the generic worker's native pipe limit when reopening without H-1.
        if not self.server.host_bytes:
            return await self.native_spawn(*args, **kwargs)
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
        # The full window must count this ordinary history as well as the
        # actual H-1 queue and ordinary provider tool results.
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

    async def fresh(self, binding):
        await self.server.dispatch("session/close", {"sessionId": self.session_id})
        self.binding = self.server.binding = binding
        opened = await self.server.dispatch("session/new", self.open_params())
        self.session_id = self.server.session_id = opened["sessionId"]
        await self.config("autoRetryEnabled", "false")

    async def final_request_budgets(self):
        for side in (8000, 8001):
            await self.fresh(f"dimension-{side}")
            label = f"dimension-{side}"
            self.add_image(f"{label}.png", dimensions=(side, 1))
            start = len(self.requests)
            await self.prompt(label)
            assert len(self.requests) == start + 1
            kept = side == 8000
            self.assert_request(start, [f"{label}.png"] if kept else [], omitted=int(not kept))
            state = await self.state()
            assert state["reasons"] == ({} if kept else {"image-dimensions": 1}), state

        (policy,) = self.server.image_policies
        observed = self.root / "provider-body.jsonl"
        prompt = '1c 몸체 "quote" \\ slash\n next\t tab \x01 control'
        images = ["tool.png", "tool-added.png"]

        async def request(binding, *, fail=False, **budget):
            self.server.image_policies = ({**policy, **budget},)
            await self.fresh(binding)
            observed.write_text("", encoding="ascii")
            start = len(self.requests)
            self.additional_tool_calls = 1
            result = await self.prompt(prompt, fail=fail)
            measurements = [json.loads(row) for row in observed.read_text(encoding="ascii").splitlines()]
            assert len(measurements) == 2, measurements
            if fail:
                assert result == -33002
                assert len(self.requests) == start + 1
                self.assert_request(start, [], omitted=0)
            else:
                assert len(self.requests) == start + 2
                body = self.assert_request(start + 1, images, omitted=0)
                assert any(prompt in message["blocks"] for message in body["messages"]), body["messages"]
                assert measurements[-1] == [body["httpBytes"], body["httpSha256"]], (measurements, body)
            return measurements[-1]

        try:
            measured = await request("calibrate-body")
            row = self.requests[-1]
            wire_size = row["httpBytes"]
            context_cost = (
                wire_size - row["base64Bytes"] + len(images) * policy["imageTokens"]
                + max(policy["outputTokens"], row["outputTokens"])
            )
            assert await request("body-at-limit", bodyBytes=wire_size) == measured
            assert await request("body-over-limit", bodyBytes=wire_size - 1, fail=True) == measured
            assert await request("tokens-at-limit", contextTokens=context_cost) == measured
            assert await request("tokens-over-limit", contextTokens=context_cost - 1, fail=True) == measured
            self.results["finalRequestBudgets"] = {"bodyBytes": wire_size, "contextCost": context_cost}
        finally:
            self.server.image_policies = (policy,)
            observed.unlink(missing_ok=True)

    async def request_boundaries(self):

        await self.fresh("twenty-plus-queue")
        await self.prompt("twenty ordinary images", ["f0.png"] * 20)
        assert len(self.requests[-1]["images"]) == 20 and self.requests[-1]["placeholders"] == 0
        self.gate.clear()
        self.server.grants["boundary"] = "f1.png"
        # Hold a text-only provider response so the next native request adds
        # exactly one queued image and no tool-result images.
        with patch("spike_image_window.sse", side_effect=lambda model, **_: sse(model)):
            turn = asyncio.create_task(self.prompt("run tool"))
            try:
                await asyncio.wait_for(self.arrivals.get(), 8)
                receipt = await self.relay(
                    "input/follow-up-images",
                    {
                        "turnId": await self.running_turn(),
                        "requestId": key(31),
                        "message": "queued boundary image",
                        "grants": ["boundary"],
                    },
                )
                assert receipt["accepted"]
            finally:
                self.gate.set()
            await turn
        assert self.requests[-1]["images"] == ["f0.png"] * 19 + ["f1.png"]
        assert self.requests[-1]["placeholders"] == 1

        await self.fresh("twenty-six")
        await self.prompt("twenty-six ordinary images", ["f0.png"] * 26)
        assert len(self.requests[-1]["images"]) == 20
        assert self.requests[-1]["placeholders"] == 6

        await self.fresh("raw-boundary")
        large_labels = [f"boundary-{index}.png" for index in range(3)]
        for label in large_labels:
            self.add_image(label, 3_303_999)
        await self.prompt("three ordinary large images", large_labels)
        assert self.requests[-1]["images"] == large_labels[1:]
        assert self.requests[-1]["rawBytes"] == 6_607_998
        assert self.requests[-1]["placeholders"] == 1
        self.results["fullRequestBoundaries"] = {
            "twentyPlusQueued": 20,
            "twentySix": 20,
            "threeLargeRawBytes": 6_607_998,
        }

    async def image_after_last_drain(self):
        await self.fresh("image-after-last-drain")
        self.server.grants = {"late-drain": "f2.png"}
        reached = asyncio.Queue()
        listener = await asyncio.start_server(lambda _, writer: reached.put_nowait(writer), "127.0.0.1", 0)
        gate = self.root / "hold-after-drain"
        gate.write_text(str(listener.sockets[0].getsockname()[1]), encoding="ascii")
        start = len(self.requests)
        writes = self.native_writes["steer"]
        first = asyncio.create_task(self.prompt("explicit first prompt"))
        writer = None
        lookup = {"requestId": key(32)}
        async with listener:
            try:
                # The pinned native emits agent_end only after the final drains.
                writer = await asyncio.wait_for(reached.get(), 10)
                active = await self.relay("input/state", {})
                assert active["active"], active
                receipt = await self.relay(
                    "input/steer-images",
                    {**lookup, "turnId": active["turnId"], "message": "late image", "grants": ["late-drain"]},
                )
                assert receipt["accepted"] and receipt["inputKind"] == "pixels", receipt
                assert len(self.requests) == start + 1
                self.assert_request(start, [], omitted=0)
            finally:
                gate.unlink(missing_ok=True)
                if writer is not None:
                    writer.write(b"!")
                    await writer.drain()
                    writer.close()
                    await writer.wait_closed()
                await first
        assert len(self.requests) == start + 1
        await self.prompt("explicit next prompt")
        assert len(self.requests) == start + 2
        # The loopback maps each decoded body image to its fixture SHA-256.
        self.assert_request(start + 1, ["f2.png"], omitted=0)
        assert await self.relay("requests/lookup", lookup) == {"status": "completed", "result": receipt}
        assert self.native_writes["steer"] == writes + 1
        self.results["imageAfterLastDrain"] = {"firstPrompt": 0, "nextPrompt": 1, "nativeWrites": 1}

    async def normal_image_comparison(self):
        # Compare the previous empty production route list with the candidate
        # hook enabled on the same unqualified model and native HTTP sink.
        await self.server.dispatch("session/close", {"sessionId": self.session_id})
        policies = self.server.image_policies
        observations = {}
        normal = "steer-before.png"
        for label, enabled_policies in (("base", ()), ("candidate", policies)):
            self.server.image_policies = enabled_policies
            self.binding = self.server.binding = "normal-" + label
            opened = await self.server.dispatch("session/new", self.open_params())
            self.session_id = self.server.session_id = opened["sessionId"]
            await self.config("model", "fixture/two")
            await self.prompt("normal image", [normal])
            row = self.requests[-1]
            observations[label] = {key: row[key] for key in ("status", "images", "placeholders", "rawBytes")}
            if label == "base":
                await self.server.dispatch("session/close", {"sessionId": self.session_id})
        assert observations["base"] == observations["candidate"]
        assert observations["candidate"]["images"] == [normal] and observations["candidate"]["status"] == 200
        self.results["unverifiedNormalPrompt"] = {"identicalToBase": True, "images": 1, "placeholders": 0}

        # Repeat the exact bytes via a running-input grant. Reopening with an
        # unqualified model must remove only the queued occurrence.
        await self.config("model", "fixture/one")
        self.server.grants["same-bytes"] = normal
        self.gate.clear()
        prompt = asyncio.create_task(self.prompt("run tool"))
        await asyncio.wait_for(self.arrivals.get(), 8)
        receipt = await self.relay(
            "input/steer-images",
            {
                "turnId": await self.running_turn(),
                "requestId": key(40),
                "message": "add the same image through running input",
                "grants": ["same-bytes"],
            },
        )
        assert receipt["accepted"]
        self.gate.set()
        await prompt
        assert self.requests[-1]["images"].count(normal) == 2
        await self.server.dispatch("session/close", {"sessionId": self.session_id})
        self.server.host_bytes = None
        self.server.relay_enabled = False
        capture = StderrCapture()
        debug_variables = {"OPENAI_LOG", "ANTHROPIC_LOG", "GEMINI_NEXT_GEN_API_LOG"}
        quiet_env = {name: value for name, value in os.environ.items() if name not in debug_variables}
        with (
            patch.dict(os.environ, quiet_env, clear=True),
            patch.object(self.server, "forward_stderr", capture.write),
        ):
            await self.server.dispatch(
                "session/load", {"sessionId": self.session_id, "cwd": str(self.root), "mcpServers": []}
            )
            assert (Path(self.worker.socket_dir) / "image-ready").is_file()
            await self.config("model", "fixture/two")
            _, encoded = self.encoded(normal)
            restored = (await self.worker.rpc("get_messages"))["messages"]
            sources = [
                block.get("source") or ""
                for message in restored
                if message["role"] == "user"
                for block in message["content"]
                if block.get("data") == encoded
            ]
            assert sources == ["", image_policy.RUNNING_IMAGE_SOURCE]
            await self.prompt("model switch after reopen")
            assert self.requests[-1]["images"].count(normal) == 1
            assert self.requests[-1]["placeholders"] == 1
            await self.server.dispatch("session/close", {"sessionId": self.session_id})
        assert b"data:image/" not in capture.data and b"HTTP Request:" not in capture.data
        self.results["unverifiedHistoryAfterLoad"] = {
            "hostNegotiated": False,
            "ordinaryImage": 1,
            "queuedImageOmitted": 1,
            "debugImageMarkers": 0,
        }

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
                await self.request_boundaries()
                await self.final_request_budgets()
                await self.image_after_last_drain()
                # All requests so far are for the negotiated, verified route.
                assert all(
                    row["rawBytes"] <= 8 * MIB and len(row["images"]) <= 20 and row["httpBytes"] <= 12 * MIB
                    for row in self.requests
                )
                await self.normal_image_comparison()
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
