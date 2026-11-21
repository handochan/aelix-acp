# Running image input

[ACP #44](https://github.com/handochan/aelix-acp/issues/44) implements the ACP side of [Studio #94 §10](https://github.com/handochan/aelix-studio/blob/865777ec4c13a031ecd7b20f6e42412bcd8da8e6/docs/studio-release-train/session-and-management-contracts.md#running-input-images). It uses the A-0 sliding window and the [Host #61 byte-source v1 contract](https://github.com/handochan/aelix-studio-host/blob/e98b749bb17961963e2b1a8dffadf4604a12d749/docs/host-32/byte-grants.md). Native wheels are unchanged.

- H1 and `aelix/hostBytes` v1 together advertise `input/steer-images`, `input/follow-up-images` and `input/image-state`. The image writes take `{turnId,requestId,message,grants}`, with ordered opaque IDs from Host prepare. Use the same ID for Host `prepareId` and ACP `requestId`. Re-preparing with the original key recovers the same immutable grant and expiry. After an uncertain dispatch, query `requests/lookup` with the original binding and key.
- A receipt with `accepted:true` means native queued the input. It includes `inputKind:"pixels"` and `captionAdded`. ACP records explicit pre-write failures as unissued and retains uncertain outcomes after a native pipe write. It sends at most one native queue write per receipt. The same binding and retained key return the original outcome even after Stop, an omitted image, or a late drain.
- Each binding writes steer in submission order and follow-up in submission order, for text and image inputs together. An image holds its place while ACP acquires the bytes. A later write can proceed after the earlier frame enters native stdin or its pre-write rejection settles. Native retains the separate steer and follow-up drain boundaries.
- ACP checks the turn, Stop generation, model, worker and binding before reverse dispatch, retries and the native write. Stop, turn end, worker or binding change cancel unfinished acceptance, drop any late reverse reply and release ACP buffers. An already committed reverse frame can still reach Host. H-1 v1 releases a snapshot on accepted finish or binding invalidation. A read abandoned before EOF retains its Host snapshot until the original 30-second reader deadline, and an unused grant expires at the 60-second idle deadline.
- `input/image-state` returns model eligibility, the request budget, and the effective cap for a fresh submission on the active turn. Each available sample describes the latest validated request with successful response headers for the current worker and model. `workerGeneration`, `requestTurnId` and `requestSequence` identify that sample. `omittedCount` and `reasons` report the older suffix replaced in that request.
- Qualified routes enforce the context window over ordinary prompts, queue drains, tool results and resumed history. The pinned Chat Completions hook rechecks image identity, count, size, dimensions, a conservative text/visual context budget and the whole provider payload before SDK dispatch. The loopback receiver checks HTTP bytes independently. The shipping route list is empty pending qualification on a production provider/model/API/endpoint and the reviewed H-1 contract. With the Host channel present, discovery exposes the wire methods and reports `model-unverified` for unqualified vision routes.
- On a negotiated connection ACP drains unused native terminal history frames in chunks, caps other events at 16 MiB and caps a correlated `get_messages` reply at 64 MiB. Native stderr stays in the bounded internal startup-refusal tail, tool updates omit raw payloads, and provider failure diagnostics use fixed text. An oversized tool-end event retains the root error and the immediate tool result error, while its payload is streamed past. The Host SDK trace fix is required for the paired acceptance.

## Verification

Run with the installed, unchanged wheels from Studio native `83714045439478b17ab0c61248dc5c414435927a`. The tests inject a policy for an isolated loopback model. They exercise the exact serialized 8 MiB rejection with 6 MiB raw, byte and count window boundaries, both image queues, response loss and Stop receipts, a model switch, a reducer and a late payload hook, HTTP rejection and explicit recovery, and loaded file history.

```sh
PYTHONPATH=src "$native/python" scripts/test_running_images.py --aelix "$native/aelix" --stop-trials 100
PYTHONPATH=src "$native/python" scripts/test_running_images_host.py --host "$host" --aelix "$native/aelix"
```

The review run used the deployed 64 KiB native stdout reader. The macOS arm64 pinned-wheel loopback covered 26 provider requests, the ordered text/image submissions in both native queues, and a 48,944,611 B `agent_end` frame. The worker settled Stop and served the next explicit request.

The paired Host probe confirms the protected byte grant and immutable re-prepare. It also verifies that standard assistant, tool and RPC error markers reach the Host wire, then checks Host SDK TRACE and HTTP diagnostics for raw and JSON-escaped copies. Use `--expect-sdk-leak` against the original H-1 head as a positive control. Renderer console and crash breadcrumbs require Studio integration evidence.

The pipe timing probe writes an 8,387,799 B steer line to an OS subprocess and immediately writes abort while the peer delays reading for 20 ms. It measures until the abort ACK. Native/provider settling is measured separately above.

| OS | Trials | p95 | Max |
| --- | ---: | ---: | ---: |
| macOS arm64 | 100 | 27.04 ms | 56.04 ms |
| Linux arm64 (container) | 100 | 23.28 ms | 91.40 ms |
| Windows x64 | 100 | 25.67 ms | 153.53 ms |

The Windows figures are from [push CI at b3105c9f](https://github.com/handochan/aelix-acp/actions/runs/44900834383/job/136836174740). The [same-head PR run](https://github.com/handochan/aelix-acp/actions/runs/44900838909/job/136836188402) hit an intermittent `sessions.sqlite3-shm` cleanup denial in the forced-adapter-death test. That test initializes with no Host byte-source offer, and its process-death assertion passed before temporary directory cleanup. The lifecycle code remains outside this change.

## Peak RSS with the deployed reader

```sh
PYTHONPATH=src "$native/python" scripts/measure_running_images.py --aelix "$native/aelix"
```

macOS arm64, pinned wheels and separate processes for ACP, native and the synthetic HTTP fixture. Each row uses fresh file-backed sessions, starts concurrent Stop requests while the provider is held, and verifies the next prompt succeeds. The peak figures are maxima from `ps` snapshots, sampled every 25 ms plus the sampling command's own runtime. Native RSS is the sum across the measured workers at each snapshot. The pipe-only peer is excluded.

| Raw history per worker | Concurrent workers | `get_messages` before Stop | Fixture peak MiB | ACP peak MiB | Native aggregate peak MiB | All Stop outcomes |
| --- | ---: | --- | ---: | ---: | ---: | ---: |
| 4 MiB | 1 | No | 73.2 | 390.9 | 506.2 | 41 ms |
| 32 MiB | 1 | No | 117.2 | 392.0 | 585.1 | 377 ms |
| 32 MiB | 1 | Yes | 112.9 | 397.4 | 527.2 | 356 ms |
| 32 MiB | 2 | No | 131.4 | 376.5 | 1,082.9 | 838 ms |
| 32 MiB | 2 | Yes, concurrent | 120.9 | 444.1 | 934.0 | 577 ms |

During the concurrent `get_messages` phase, ACP sampled 444.1 MiB and native workers sampled 887.3 MiB combined. The Stop samples measure session settling through ACP. The 100-trial figures above measure pipe acknowledgement timing.

Pixels remain default-off pending the H-1 diagnostic fix and review, Studio diagnostics, and production endpoint qualification.
