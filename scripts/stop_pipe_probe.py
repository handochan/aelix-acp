"""Measure abort after an ~8 MiB write on a real OS subprocess pipe."""

import asyncio
import json
import sys
from pathlib import Path
from statistics import median
from time import perf_counter
from types import SimpleNamespace

from aelix_acp import image_policy
from aelix_acp.session import Session


# Delay the peer before reading each queue line so the pipe backs up. The
# subprocess models the pinned native JSONL framing and queues, without HTTP.
def peer():
    import time

    while True:
        time.sleep(0.02)
        queue = sys.stdin.buffer.readline()
        if not queue:
            return
        abort = sys.stdin.buffer.readline()
        for expected, raw in (("steer", queue), ("abort", abort)):
            command = json.loads(raw)
            assert command["type"] == expected
            print(
                json.dumps({"type": "response", "command": expected, "success": True, "id": command["id"]}),
                flush=True,
            )


async def measure(count):
    import platform

    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(Path(__file__).resolve()),
        "--peer",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    worker = SimpleNamespace(proc=proc, pending={}, interventions={}, rpc_closed=False, turn_active=False)
    image = {"type": "image", "mimeType": "image/png"}
    # Keep each encoded block within the file limit while approaching 8 MiB.
    encoded = "A" * (4 * ((image_policy.MAX_SUBMISSION_BYTES // 2) // 3) - 512)
    images = [{**image, "data": encoded}, {**image, "data": encoded}]
    started = asyncio.Event()
    write = proc.stdin.write
    queue_frame_bytes = 0

    def observe_write(raw):
        nonlocal queue_frame_bytes
        if len(raw) > 1_000_000:
            queue_frame_bytes = len(raw)
            started.set()
        write(raw)

    async def answers():
        while raw := await proc.stdout.readline():
            reply = json.loads(raw)
            worker.pending[reply["id"]].set_result({})

    proc.stdin.write = observe_write
    reader = asyncio.create_task(answers())
    samples = []
    try:
        async with asyncio.timeout(90):
            for _ in range(count):
                started.clear()
                queue = asyncio.create_task(
                    Session.rpc(
                        worker,
                        "steer",
                        message="pipe measurement",
                        images=images,
                        _image_write=(lambda: True, lambda: None),
                    )
                )
                await started.wait()
                start = perf_counter()
                await Session.rpc(worker, "abort")
                samples.append((perf_counter() - start) * 1000)
                await queue
    finally:
        proc.stdin.close()
        await proc.wait()
        await reader
    measured = sorted(samples)
    assert proc.returncode == 0 and 8_380_000 < queue_frame_bytes <= image_policy.MAX_NATIVE_LINE
    return {
        "os": platform.system(),
        "arch": platform.machine(),
        "count": count,
        "queueFrameBytes": queue_frame_bytes,
        "medianMs": round(median(samples), 2),
        "p95Ms": round(measured[(95 * count + 99) // 100 - 1], 2),
        "maxMs": round(measured[-1], 2),
    }


if __name__ == "__main__":
    if sys.argv[1:] == ["--peer"]:
        peer()
    else:
        count = int(sys.argv[1]) if len(sys.argv) == 2 else 100
        print(json.dumps(asyncio.run(measure(count)), indent=2))
