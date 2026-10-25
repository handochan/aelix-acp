# ACP #7 management and session-host wire qualification

2026-10-25, public native beta.2 **without patches**, ACP `c573b2cacc26b1b76e1377ceeaa1bd1bf6b3431e`,
Studio #4 `aa6b07190d1eea8f8177ed4e6e8911389837fdde`, AionCore public `v0.2.2`.
This records measured W01/W02 compatibility and implementation handoffs. The
[canonical Studio contract](https://github.com/handochan/aelix-studio/blob/aa6b07190d1eea8f8177ed4e6e8911389837fdde/docs/studio-release-train/session-and-management-contracts.md)
remains a proposal; this document does not install a production helper or add
`_aelix/manage` to ACP main. PR #1 and both original dirty checkouts are excluded.

## Responsibility and actual support

| Owner/path | Observed support | Limit / next implementation |
| --- | --- | --- |
| Studio-owned sessionless stdio helper → native libraries | A test-only helper registers a key, reads status, logs out, resolves a user profile and lists installed extensions with no LLM/session worker | Production endpoint, authenticated facade, package/profile mutation policy and UI remain Studio work |
| Studio facade → existing AionCore → installed ACP → native | Basic session, launch selector, text, actual tool permission denial, Stop, busy rejection, restart/load and stored history | Keep this one session supervisor. A helper must not send private commands to its live worker |
| ACP current `authenticate` / `_aelix/manage` | No auth methods advertised; `authenticate` returns `-32602`; custom management returns `-32601` | Provider bootstrap cannot require a working LLM session; use a sessionless helper |
| Native model/reasoning/context | Earlier #6/#4 qualification remains valid | This change does not add model switching, reasoning/context forwarding, Ask/Yolo or control-v1 |
| Custom context/child/auth-progress notifications | Not exercised in this qualifier; current selected ACP does not emit these custom streams | Host delivery and reconnect terminal snapshots remain unknown; do not infer support from ordinary text/tool updates |
| Global native mutation versus legacy CLI | Two processes preserve unrelated providers in a sequential update/logout test; held auth status stays stale until reload | Concurrent shared mutation is unsupported; helper serialization alone cannot fix noncooperating writers |

The executable evidence is [management-qualification.json](management-qualification.json).
`passed` means the assertions reproduced these behaviors **including expected
gaps**, not that every management feature is implemented. The original four
[wheel hashes](manifest.json) and 309 original installed members were checked
before/after. ACP package bytes matched the selected source before/after. The
full receipt includes platform, resolved dependency versions and harness hashes.
Native tag `1093cc2578e70500c91fe0a8d340b7db2528c8de` is a source reference,
not independent wheel build provenance. The tested AionCore binary SHA256 is
`04404864ca622159ffcc79ef9cb468e82fc820b91f16d565c5a5efdb0b0c4cb1`.

## Authentication, roots and secrets

The public CLI explicitly constructs `AuthStorage(Path(get_agent_dir()) / "auth.json")`.
`get_agent_dir()` uses `AELIX_CODING_AGENT_DIR` or `~/.aelix/agent`. In contrast,
the library's implicit `AuthStorage()` consults `AELIX_AUTH_PATH`, then XDG, then
`~/.config/aelix/agent/auth.json`. The probe deliberately sets disagreeing roots:
only the explicit CLI root changes. A production helper must resolve and report
the selected CLI/native root and pass it explicitly, alongside the same settings,
endpoint and session scope. Do not create a second credential store implicitly.

The helper calls native `set_api_key`, `get_auth_status`, `logout`, `load` and
`login`. Status reports `configured/source`, never the key. Native auth file mode
is `0600` on the tested POSIX host. Configured means stored credentials, not a
successful remote authorization: a bad key/endpoint/model still needs a separate
explicit provider test. AionCore Provider DB and WebUI login are separate stores
and purposes; neither proves Aelix provider authentication.

The native OAuth registry is given an explicitly **synthetic provider**. Real
`AuthStorage.login` invokes URL/input callbacks; cancel returns a terminal error
without saving tokens. Success commits native credentials and logout removes
them. Closing helper stdin during a pending input returns `-32800`, exits zero
within five seconds and leaves no credentials. This tests callbacks/lifecycle,
not any real provider, browser redirect, token refresh or remote WebUI OAuth.

Fixture keys, tokens and input values are absent from captured helper responses,
status and the published structural receipt. OAuth URL/input are transient and
excluded from the receipt. Requests/exceptions are not echoed. The tap discards
native stderr and records method/field names and stop/error codes only. This is
not an attestation that existing production AionCore/ACP diagnostics redact every
provider error. A production helper must suppress secret-bearing request logging,
transcripts, telemetry and operation ledgers at every boundary.

The actual nonlocal AionCore rejects an unauthenticated facade request with 401.
Session tests use a separate disposable `--local` embedded host. This does not
constitute authenticated remote helper integration: there is no production helper
route yet. It must be placed behind Studio's existing authentication/Origin and
scope boundary; do not add a second public listener.

## Requests, denial, cancel and observed state

| Trigger | Actual wire / state | Client implication |
| --- | --- | --- |
| HTTP send | 202 admission, then ACP `session/prompt` and text updates, then `stopReason:end_turn` | Admission is not completed execution |
| Same `_meta["aelix/requestId"]` twice directly to ACP | Two provider requests | ACP current pin has no deduplication ledger |
| Same metadata twice via host HTTP | Two provider requests; no `_meta` in either ACP prompt | Stable IDs are lost before ACP; do not automatically resend after ambiguous timeout/disconnect |
| A second prompt during a held turn | Host HTTP 409; direct ACP `-32602` | Busy is rejection, not steering or a queued follow-up; `supports_midturn_delivery:false` |
| Unavailable model | Host HTTP 400 | Launch-only selector cannot imply live model switching |
| Native synthetic write tool / reject offered option | Actual `session/request_permission`, host confirmation, native deny; target file absent | Echo an actually offered option; never synthesize approval IDs |
| Stop while native tool consent pending | Pending confirmation clears; cancelled prompt terminal; target file absent | Cancellation must also settle pending consent |
| Stop during held provider request | Turn-scoped HTTP cancel → ACP notification without ID → native abort → prompt `stopReason:cancelled` → idle | HTTP acknowledgement alone is not the terminal result or proof of no external effects |
| Cancel with stale turn ID | HTTP 200 no-op, current runtime still running, no ACP cancel emitted | A successful HTTP response does not mean that turn was cancelled |
| Host runtime restart | `session/load`, native history replay, idle; host persisted messages identical | Reconnect restores stored history; no live attach, pending-command replay or exactly-once recovery claim |
| Candidate HTTP `.../_aelix/manage` route | 404 | No generic custom forwarding route is exposed here; do not invent one in clients |
| Synthetic reverse `_aelix/client_action` after active prompt | No reply during the recorded bounded observation; ordinary prompt still completes | Custom dialog is unsupported/unknown beyond the observation window; do not wait indefinitely or route it through a helper |

The reverse request is injected by a transparent **test-only tap**, not emitted by
native and not a capability advertisement. The receipt records the observation
duration, not a proof that a reply can never arrive. A separate AionCore source
path (`aionui-session/backend/acp_conn.rs`) returns `-32601` for unknown reverse
RPCs; that source behavior must not be attributed to this observed custom-agent
route. The pinned SDK wrapper has `ext_request` internally, while its facade
still needs explicit bounded exposure and typed reverse-action handlers.

The production port should retain transport request ID, stable operation/turn ID,
scope and runtime generation, with `accepted`, apply time and independently
observed effective state. Cancel must name the active operation/turn and produce
a terminal result, including on EOF/deadline. Unsupported methods need bounded,
structured failure. These are handoff requirements, not fields added to current
ACP v1. Secrets belong in transient input, not replay/dedup ledgers; a future
ledger must not fingerprint or persist raw credentials. An ambiguous cancelled
mutation must be resolved by native status, not blindly repeated.

## Shared mutation and compatibility boundary

The public native auth store already has POSIX `flock`; describing it as having
no locking would be incorrect. It locks `auth.json` itself, then replaces that
pathname. In a **two-process native IO-primitive probe**, process A keeps the
original descriptor locked after native `_write_raw`; its inode differs from the
current pathname. Process B's ordinary `set_api_key` completes while that old
descriptor is still locked. No native file or method is modified/monkeypatched.
This is a lock/rename counterexample, not a full public-setter lost-update or
crash stress test. Windows was not run. Existing [native #46](https://github.com/handochan/aelix-ai/issues/46)
tracks cross-process auth/settings locking and temp-file problems.

Preview must report legacy concurrent shared mutation as unsupported. A
production helper can serialize its own operations and explicitly reload caches
or restart idle workers, but cannot promise safety against the legacy CLI.
Auth/settings/profile stores need shared canonical-scope locks, reread/revision
checks and safe publication before claiming concurrency support. Do not backport
these into the public Preview wheels. Profile resolution/package inventory do
not prove profile CRUD, registry-wide package mutation, module hot unload or a
writable runtime generation; those require their own acceptance.

## Reproduce and evidence levels

Create separate disposable public native and installed ACP wheel environments as
in [#6](README.md#reproduce). Run with the native Python, matching ACP package
payload, and the exact Studio #4 AionCore darwin-arm64 binary:

```sh
.local/native/bin/python -B scripts/qualify_management.py \
  --adapter /absolute/path/to/installed/acp/bin/aelix-acp \
  --host /absolute/path/to/pinned/aioncore \
  --report .local/management-qualification.json

# Ubuntu CI: helper/native/ACP only; host checks are explicitly not_run.
.local/native/bin/python -B scripts/qualify_management.py \
  --adapter /absolute/path/to/installed/acp/bin/aelix-acp \
  --native-only --report .local/management-native.json
```

Python `-O` is refused. The helper refuses a HOME/cwd outside the supplied test
sandbox; it is not packaged or registered as a product entrypoint. The tap owns
only its test child process group and performs bounded EOF/terminate/kill cleanup.
Registry identity audits require network during verification; this is not offline
installation acceptance. The CI workflow adds the native-only regression but its
remote runs remain unexecuted until an authorized push.

This run covers native wheel bytes/APIs, a synthetic-model installed ACP process,
and actual macOS arm64 AionCore HTTP/stdio. No Studio renderer changes were made;
Studio #4's earlier renderer smoke is cited separately. Real provider/OAuth,
Windows/Linux host/installer, signed bundle, field/customer data, Team, package
generation and closed-network installation remain untested. The POSIX-only ACP,
16 MiB frame and aggregate-history limitations remain.

## Implementation handoff

- [Studio #5](https://github.com/handochan/aelix-studio/issues/5): production
  authenticated sessionless helper and existing Provider/settings UI, explicit
  native roots, secret handling, effective-state refresh and real provider flows.
- [Studio #7](https://github.com/handochan/aelix-studio/issues/7): profile/package
  management and writable runtime generations; inventory is not an install proof.
- [ACP #5](https://github.com/handochan/aelix-acp/issues/5): Ask/Yolo launch policy,
  idle restart and actual host selector/permission acceptance, with existing bridge
  intact. This qualification does not implement or advertise the second mode.
- [Studio #16](https://github.com/handochan/aelix-studio/issues/16): bounded host
  forwarding, stable request identity and reverse client-action handling through
  the actual custom-agent route. Preserve the existing session supervisor.
- [ACP #12](https://github.com/handochan/aelix-acp/issues/12): compatible
  deduplication/outcome ledger and explicit ambiguous recovery. Metadata
  forwarding alone cannot make retries safe.
- Native #46 remains a dependency for shared mutation safety claims. Preview
  uses the published wheels unchanged and retains the unsupported boundary.

Local review and commits are not push/merge/release delivery. #7 remains open
until its reviewed evidence/handoff is accepted; dependent cards remain blocked
until that acceptance. This qualification does not close the Studio release.

The [independent review record](management-review.md) separates its rerun from
the final author rerun and lists the repaired harness assertions.
