"""Regression controls for the ACP #46 review, using installed native and loopback HTTP."""
import argparse
import asyncio
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from time import perf_counter
from unittest.mock import patch

from aelix_ai.messages import ImageContent, TextContent, UserMessage

from aelix_acp import session
from aelix_acp.runtime import session_relay
from aelix_acp.wire.errors import ProtocolError

from fixtures import image_window_extension as window
from spike_image_window import MIB, Probe, ProbeServer


UNAVAILABLE = {"status": "unavailable"}
MARKER = "fixture-provider-body /fixture/private.png"


def check_projection_cost():
    pixels = base64.b64encode(bytes(2 * MIB)).decode()
    block = ImageContent(data=pixels, mime_type="image/png")
    message = UserMessage(content=[TextContent(text="retained text"), block])
    rows = []
    for active_mib in (4, 32, 128):
        messages = [message] * (active_mib // 2)
        with patch.object(window, "image_raw_bytes", wraps=window.image_raw_bytes) as decoded:
            _, stats = window.project_images(messages, raw_bytes=4 * MIB, count=20)
        assert decoded.call_count == min(len(messages), 3)
        assert stats["keptRawBytes"] == 4 * MIB
        assert stats["omittedCount"] == len(messages) - 2
        assert messages[-1].content[1] is block
        rows.append({"activeMiB": active_mib, "blocksDecoded": decoded.call_count})

    with patch.object(window, "image_raw_bytes", wraps=window.image_raw_bytes) as decoded:
        legacy = UserMessage(content=[ImageContent(source="https://example.invalid/too-old.png")])
        projected, stats = window.project_images([legacy, message], raw_bytes=8 * MIB, count=1)
    assert decoded.call_count == 1 and stats["reasons"] == {"image_count": 1}
    assert projected[0].content[0].text == window.PLACEHOLDER
    assert window.image_raw_bytes(ImageContent(source="eHl6")) == 3
    assert window.image_raw_bytes(ImageContent(source="data:image/png;base64,eHl6")) == 3
    for source in ("", "http", "http://example.invalid/pixel", "data:image/png,xyz", "e!l6"):
        try:
            window.image_raw_bytes(ImageContent(source=source))
        except ValueError as exc:
            assert str(exc) == "Unmeasurable image in request context"
        else:
            raise AssertionError("Invalid or unmeasurable image was accepted")
    return rows


async def legacy_and_state(probe):
    probe.add_image("legacy-large.png", 9 * MIB)
    await probe.prompt("legacy large")
    sent = probe.assert_request(0, [], omitted=1)
    legacy_sample = await probe.assert_state(sent, 1, 1, "raw_bytes")
    assert legacy_sample["turnSequence"] == 1
    message = next(row["message"] for line in probe.disk() if (row := json.loads(line))["type"] == "message")
    source = message["content"][1]["source"]
    assert probe.raw_image("image/png", source.split(",", 1)[1]) == ("legacy-large.png", 9 * MIB)

    await probe.prompt("legacy remote", fail=True)
    assert len(probe.requests) == 1 and await probe.state() == UNAVAILABLE
    # A newest image uses the only slot, so neither old unmeasurable image is sent.
    probe.policy(count=1)
    await probe.prompt("recover with a bounded suffix", ["n0.png"])
    recovered = probe.assert_request(1, ["n0.png"], omitted=2)
    state = await probe.assert_state(recovered, 2, 2, "image_count")
    assert state["turnSequence"] == 3 and state["workerGeneration"] == legacy_sample["workerGeneration"]

    await probe.prompt("reinsert after context", fail=True)
    assert len(probe.requests) == 2 and await probe.state() == UNAVAILABLE
    await probe.prompt("no late payload mutation")
    restored = await probe.assert_state(probe.assert_request(2, ["n0.png"], omitted=2), 4, 2, "image_count")
    await probe.config("model", "fixture/two")
    assert await probe.state() == UNAVAILABLE
    await probe.prompt("with alternate model")
    alternate = await probe.assert_state(probe.assert_request(3, ["n0.png"], omitted=2), 5, 2, "image_count")
    assert alternate["model"] == "fixture/two" and alternate["workerGeneration"] == restored["workerGeneration"]

    binding = probe.binding
    await probe.worker.restart(trust_project=False)
    assert probe.worker.input_relay.binding_id == binding and await probe.state() == UNAVAILABLE
    await probe.prompt("same binding after worker replacement")
    restarted = await probe.assert_state(probe.assert_request(4, ["n0.png"], omitted=2), 1, 2, "image_count")
    assert restarted["workerGeneration"] != alternate["workerGeneration"] and restarted["turnSequence"] == 1
    return {"legacyRawBytes": 9 * MIB, "legacyWireRawBytes": sent["rawBytes"],
            "unmeasurableAndLateMutationHttpWrites": 0, "sameBindingNewGeneration": True}


async def enable_native_recovery(probe):
    await probe.config("autoRetryEnabled", "true")
    await probe.config("autoCompactionEnabled", "true")


async def count_rejection_with_recovery_enabled(probe):
    await enable_native_recovery(probe)
    await probe.prompt("two pixels", ["n0.png", "n1.png"])
    worker, filename = probe.worker, probe.transcript_path()
    probe.provider_cap = 1
    await probe.prompt("stale count limit", fail=True)
    assert [row["status"] for row in probe.requests] == [200, 400]
    assert await probe.state() == UNAVAILABLE
    probe.policy(count=1)
    await probe.prompt("explicit turn after limit refresh")
    await probe.assert_state(probe.assert_request(2, ["n1.png"], omitted=1), 3, 1, "image_count")
    assert probe.worker is worker and probe.transcript_path() == filename
    assert not any(kind == "compaction_start" for kind, _ in probe.native_lines)
    assert probe.native_writes["prompt"] == 3
    assert not (probe.native_writes["steer"] or probe.native_writes["follow_up"])
    return {"httpStatuses": [row["status"] for row in probe.requests], "explicitPrompts": 3}


async def automatic_overflow(probe):
    await enable_native_recovery(probe)
    await probe.prompt("early one", ["n0.png"])
    await probe.prompt("early two", ["n1.png"])
    worker, filename = probe.worker, probe.transcript_path()
    probe.fail_next = (413, "request_too_large", "request_too_large")
    await probe.prompt("native recovers within this prompt", ["n2.png"])
    assert probe.worker is worker and probe.transcript_path() == filename
    statuses = [row["status"] for row in probe.requests]
    assert statuses == [200, 200, 413, 200, 200, 200], statuses
    assert [len(row["images"]) for row in probe.requests] == [1, 2, 3, 0, 0, 0]
    assert sum(kind == "compaction_end" for kind, _ in probe.native_lines) == 1
    assert probe.native_writes["prompt"] == 3
    assert not (probe.native_writes["steer"] or probe.native_writes["follow_up"])
    state = await probe.assert_state(probe.requests[-1], 4, 0)
    assert state["turnSequence"] == 3
    return {"httpStatuses": statuses, "explicitPrompts": 3, "nativeCompactions": 1}


async def receipt_control(probe):
    async def relay(method, payload):
        return await probe.server.dispatch(session_relay.METHOD_PREFIX + method, {
            "sessionId": probe.session_id, "bindingId": probe.binding, "payload": payload})

    async def turn_id():
        async with asyncio.timeout(5):
            while True:
                state = await relay("input/state", {})
                if state["active"]:
                    return state["turnId"]
                await asyncio.sleep(0.01)

    async def send(command, **kwargs):
        if command == "follow_up":
            mime, data = probe.encoded("n0.png")
            result = await real_rpc(command, images=[{"mimeType": mime, "data": data}], **kwargs)
            native_ack.set()
            await release_ack.wait()
            return result
        return await real_rpc(command, **kwargs)

    async def held_turn(*, stop_reason="end_turn"):
        probe.gate.clear()
        native_ack.clear()
        prompt = asyncio.create_task(probe.prompt("run tool", stop_reason=stop_reason))
        await asyncio.wait_for(probe.arrivals.get(), timeout=10)
        return prompt, await turn_id()

    def input_payload(turn, number):
        return {"turnId": turn, "requestId": f"01a1f392-16c0-7000-8000-{number:012x}",
                "message": f"queue fixture {number}"}

    def lookup(payload):
        return relay("requests/lookup", {"requestId": payload["requestId"]})

    probe.hold_tool_request = True
    probe.policy(count=1)
    native_ack, release_ack = asyncio.Event(), asyncio.Event()
    real_rpc = probe.worker.rpc
    with patch.object(probe.worker, "rpc", new=send):
        prompt, turn = await held_turn()
        missing_reply = input_payload(turn, 1)
        original = asyncio.create_task(relay("input/follow-up", missing_reply))
        duplicate = None
        try:
            await asyncio.wait_for(native_ack.wait(), timeout=5)
            assert await lookup(missing_reply) == {"status": "pending"}
            duplicate = asyncio.create_task(relay("input/follow-up", missing_reply))
        finally:
            release_ack.set()
            probe.gate.set()
            await original  # Simulate dropping the original response at the caller.
            await prompt
        assert duplicate is not None
        completed = {"status": "completed", "result": {
            "turnId": turn, "accepted": True, "delivery": "native-queue"}}
        assert await duplicate == completed["result"]
        assert await lookup(missing_reply) == completed
        assert probe.native_writes["follow_up"] == 1
        assert probe.requests[-1]["images"] == ["n0.png"]
        probe.hold_tool_request = False
        await probe.prompt("newer image omits the queued image", ["n1.png"])
        assert probe.requests[-1]["images"] == ["n1.png"]
        assert await lookup(missing_reply) == completed

        probe.hold_tool_request = True
        prompt, turn = await held_turn(stop_reason="cancelled")
        stopped = input_payload(turn, 2)
        try:
            receipt = await relay("input/follow-up", stopped)
            assert receipt == {"turnId": turn, "accepted": True, "delivery": "native-queue"}
            await probe.server.dispatch("session/cancel", {"sessionId": probe.session_id})
            assert await prompt == {"stopReason": "cancelled"}
            assert await lookup(stopped) == {"status": "completed", "result": receipt}
            assert await relay("input/follow-up", stopped) == receipt
            assert await relay("input/follow-up", input_payload(turn, 3)) == {
                "accepted": False, "reason": "stale-turn"}
            assert probe.native_writes["follow_up"] == 2
        finally:
            probe.gate.set()
            await prompt
        assert b"queue fixture 2" not in probe.transcript_path().read_bytes()
    return {"nativeQueueWrites": 2, "afterOmissionReceiptUnchanged": True, "afterStopReceiptUnchanged": True}


async def large_file_session(probe):
    samples = []
    finished = asyncio.Event()
    ps = shutil.which("ps") if os.name != "nt" else None
    worker = probe.worker
    pid = str(worker.proc.pid)
    measuring = probe.reader_limit > session.FRAME_LIMIT

    async def sample_rss():
        if not ps:
            return
        while not finished.is_set():
            result = await asyncio.to_thread(
                subprocess.run, [ps, "-o", "rss=", "-p", pid], capture_output=True, check=False)
            if not result.stdout.strip():
                break
            samples.append(int(result.stdout) * 1024)
            try:
                await asyncio.wait_for(finished.wait(), timeout=0.05)
            except TimeoutError:
                pass

    sampler = asyncio.create_task(sample_rss())
    try:
        for index in range(8):
            await probe.prompt(f"history batch {index}", ["a.png", "b.png"])
        assert probe.requests[-1]["rawBytes"] == 8 * MIB and probe.requests[-1]["placeholders"] == 12
        probe.hold_tool_request = True
        probe.gate.set()
        probe.additional_tool_calls = 2
        start = perf_counter()
        await probe.prompt("run tool")
        elapsed = perf_counter() - start
        assert len(probe.requests) == 12
        assert probe.native_writes["prompt"] == 9
        assert sum(kind == "tool_execution_end" for kind, _ in probe.native_lines) == 3
        assert all(row["rawBytes"] <= 8 * MIB for row in probe.requests)

        # Drain the previous gated request notification before timing Stop.
        probe.arrivals.get_nowait()
        probe.gate.clear()
        stdout_start = len(probe.native_lines)
        prompt = asyncio.create_task(probe.prompt("run tool", fail=not measuring, stop_reason="cancelled"))
        try:
            await asyncio.wait_for(probe.arrivals.get(), timeout=10)
            start = perf_counter()
            await probe.server.dispatch("session/cancel", {"sessionId": probe.session_id})
            outcome = await prompt
            stopped = perf_counter() - start
            if measuring:
                assert outcome == {"stopReason": "cancelled"} and await probe.state() == UNAVAILABLE
                cancel_event = next(size for kind, size in probe.native_lines[stdout_start:] if kind == "agent_end")
                assert cancel_event > session.FRAME_LIMIT
                observation = {"cancellationAgentEndBytes": cancel_event}
            else:
                assert outcome == -33002 and worker.rpc_closed and "limit" in worker.failure.lower()
                observation = {"acpError": outcome, "rpcClosed": True}
        finally:
            probe.gate.set()
            await prompt
    finally:
        finished.set()
        await sampler
    assert sum(kind == "compaction_end" for kind, _ in probe.native_lines) == 0
    return {**observation, "initialActiveRawBytes": 32 * MIB, "consecutiveTools": 3,
            "toolLoopMs": round(elapsed * 1_000), "stopToSettledMs": round(stopped * 1_000),
            "sampledPeakNativeRssBytes": max(samples) if samples else None, "rssSamples": len(samples)}


async def measure_large_file_session(probe):
    return await large_file_session(probe)


class StderrCapture:
    def __init__(self):
        self.data = bytearray()

    async def write(self, data):
        self.data.extend(data)


async def trace_controls(probe):
    probe.hold_tool_request = True
    probe.gate.set()
    await probe.prompt("run tool", ["n0.png"])
    expected_lengths = [1_398_104, 92]
    assert probe.server.tool_image_base64_bytes == expected_lengths
    probe.fail_next = (400, MARKER, "invalid_request")
    try:
        await probe.server.dispatch("session/prompt", {"sessionId": probe.session_id, "prompt": [
            {"type": "text", "text": "fixture-private-prompt /fixture/source.png"}]})
    except ProtocolError as exc:
        assert exc.code == -33002 and MARKER in str(exc)
    else:
        raise AssertionError("The failure control did not reach ACP")
    stderr = probe.stderr.data
    assert b"data:image/png;base64," in stderr
    assert b"fixture-private-prompt /fixture/source.png" in stderr
    return {"ordinaryAcpImageBase64Bytes": expected_lengths, "nativeStderrDataUrlDetected": True,
            "nativeStderrPromptAndPathDetected": True, "acpProviderErrorMarkerDetected": True}


class ReviewProbe(Probe):
    def __init__(self, root, executable, case):
        super().__init__(root, executable)
        self.case = case
        if case is measure_large_file_session:
            self.reader_limit = 64 * MIB
        if case is trace_controls:
            self.native_env["OPENAI_LOG"] = "debug"
            self.stderr = StderrCapture()

    async def exercise(self):
        self.results.update(await self.case(self))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aelix", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = {"cutoffCost": check_projection_cost()}
    for case in (legacy_and_state, count_rejection_with_recovery_enabled, automatic_overflow,
                 receipt_control, trace_controls, large_file_session, measure_large_file_session):
        with tempfile.TemporaryDirectory(prefix="acp43-review-", dir="/tmp" if os.name != "nt" else None) as directory:
            probe = ReviewProbe(Path(directory).resolve(), args.aelix.resolve(), case)
            if case is trace_controls:
                with patch.object(ProbeServer, "forward_stderr", new=lambda _server, raw: probe.stderr.write(raw)):
                    result = asyncio.run(probe.run())
            else:
                result = asyncio.run(probe.run())
            report[case.__name__] = {name: result[name] for name in probe.results if name != "relayAdvertisement"}
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
