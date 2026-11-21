# Running image input

[ACP #44](https://github.com/handochan/aelix-acp/issues/44) implements the ACP side of [Studio #94 §10](https://github.com/handochan/aelix-studio/blob/865777ec4c13a031ecd7b20f6e42412bcd8da8e6/docs/studio-release-train/session-and-management-contracts.md#running-input-images). It uses the A-0 sliding window and the [Host #61 byte-source v1 contract](https://github.com/handochan/aelix-studio-host/blob/e98b749bb17961963e2b1a8dffadf4604a12d749/docs/host-32/byte-grants.md). Native wheels are unchanged.

- H1 and `aelix/hostBytes` v1 together advertise `input/steer-images`, `input/follow-up-images` and `input/image-state`. The image writes take `{turnId,requestId,message,grants}`, with ordered opaque IDs from Host prepare. Use the same ID for Host `prepareId` and ACP `requestId`. Re-preparing with the original key recovers the same immutable grant and expiry. After an uncertain dispatch, query `requests/lookup` with the original binding and key.
- A receipt with `accepted:true` means native queued the input. It includes `inputKind:"pixels"` and `captionAdded`. ACP records explicit pre-write failures as unissued and retains uncertain outcomes after a native pipe write. It sends at most one native queue write per receipt. The same binding and retained key return the original outcome even after Stop, an omitted image, or a late drain.
- `input/image-state` returns model eligibility, the request budget, and the effective cap for a fresh submission on the active turn. Each available sample describes the latest validated request with successful response headers for the current worker and model. `workerGeneration`, `requestTurnId` and `requestSequence` identify that sample. `omittedCount` and `reasons` report the older suffix replaced in that request.
- Qualified routes enforce the context window over ordinary prompts, queue drains, tool results and resumed history. The pinned Chat Completions hook rechecks image identity, count, size, dimensions, a conservative text/visual context budget and the whole provider payload before SDK dispatch. The loopback receiver checks HTTP bytes independently. The shipping route list is empty pending qualification on a production provider/model/API/endpoint and the reviewed H-1 contract. With the Host channel present, discovery exposes the wire methods and reports `model-unverified` for unqualified vision routes.
- On a negotiated connection ACP drains unused native terminal history frames in chunks, caps other events at 16 MiB and caps a correlated `get_messages` reply at 64 MiB. Native stderr stays in the bounded internal startup-refusal tail, tool updates omit raw payloads, and provider failure diagnostics use fixed text. The Host SDK's trace controls are part of the paired acceptance.

## Verification

Run with the installed, unchanged wheels from Studio native `83714045439478b17ab0c61248dc5c414435927a`. The tests inject a policy for an isolated loopback model. They exercise the exact serialized 8 MiB rejection with 6 MiB raw, byte and count window boundaries, both image queues, response loss and Stop receipts, a model switch, a reducer and a late payload hook, HTTP rejection and explicit recovery, and loaded file history.

```sh
PYTHONPATH=src "$native/python" scripts/test_running_images.py --aelix "$native/aelix" --stop-trials 100
PYTHONPATH=src "$native/python" scripts/test_running_images_host.py --host "$host" --aelix "$native/aelix"
```

The macOS arm64 loopback run captured 25 provider requests, with an 11,201,847 B maximum HTTP body and a 48,944,069 B native `agent_end` at Stop. The worker settled Stop in 200 ms and served the next explicit request. The paired macOS run with H-1 head `e98b749b` acquired a 250,000 B grant over the actual protected listener and reverse stdio, recovered the original descriptors on re-prepare, and matched the authorized snapshot to the provider image after the local file changed. TRACE diagnostics had zero synthetic prompt, provider error, file, base64, digest and grant-ID markers.

The pipe timing probe writes an 8,387,799 B steer line to an OS subprocess and immediately writes abort while the peer delays reading for 20 ms. It measures until the abort ACK. Native/provider settling is measured separately above.

| OS | Trials | p95 | Max |
| --- | ---: | ---: | ---: |
| macOS arm64 | 100 | 27.04 ms | 56.04 ms |
| Linux arm64 (container) | 100 | 23.28 ms | 91.40 ms |
| Windows x64 | PR CI | Pending | Pending |

Production endpoint qualification, the outcome of H-1 independent review, and Windows results remain open.
