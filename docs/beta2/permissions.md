# ACP #5 permission candidate and additional-mode assessment

2026-10-25. Base ACP main `2128cc8d36afd1ae6537988f0117c5393d09b927`, four
unmodified public native `0.1.0b2` wheels from [the fixed manifest](manifest.json),
AionCore `v0.2.2` / `47e66d0d151123e973b3fd1e77afcb5671b3f8c5`.
This is local candidate implementation and qualification. **ACP #5 product
acceptance remains blocked by a host display/effective-state mismatch.**
Studio #5 is owned by its separate execution session; this change edits no Studio
code, native wheel or private control-v1 candidate.

## Implemented adapter behavior

| Mode ID | Selector | Native launch | ACP approval policy |
| --- | --- | --- | --- |
| `default` | Ask | `--permission-mode default` | Every tool requires a listed Allow once/Reject choice |
| `yolo` | Yolo | `--permission-mode yolo` | Every tool still crosses the private bridge; client approval questions are skipped |

Yolo retains native guardrails, native active-tool selection, other extension
denials and cancellation. The bridge remains loaded and its ready nonce is
checked; channel failure blocks execution in both modes. Explicit extensions
load before the bridge so consent sees their prior argument changes. Trusted
extension Python still runs with the user's authority; this is not a sandbox.

The standard legacy `modes` selector is translated by the existing AionCore
`config-options/mode` endpoint. No custom host forwarding or second supervisor
is introduced. `session/set_mode` accepts only these two verified IDs; other
native mode names receive `-32602`. The optional adapter `--permission-mode`
sets new-session policy; loading an existing ACP identity uses its saved policy.

Changes require an idle native worker. The adapter reserves the ACP identity
through restart and rejects prompt/load/another setting while it is reserved.
Active prompt ownership extends through final `get_state`/save, and native
`isStreaming` is checked as well as pending consent. A provider `agent_end`
alone cannot admit a mode restart while retry/compaction or finalization remains.

Successful switching retains the ACP ID and actual native transcript path,
replaces the child, checks bridge readiness/state/commands, persists the mode and
publishes `current_mode_update`. A mode notification also follows new/load
responses so clients can observe restored policy after applying the response.
`_meta["aelix/permission"]` identifies native launch mode, approval policy and
apply time. Public beta.2 has no RPC posture getter: `nativePostureObservation`
is explicitly `launch_argument`, with actual behavior proven by tool effects.
Do not turn this into a claim of a native live-posture setter/readback.

The additive `permission_modes` table preserves the existing sessions schema.
Old records without a mode remain Ask; unknown stored modes refuse resume.
Yolo is saved only after startup/commands succeed. Downgrades save Ask before
replacing the old worker. Failed/cancelled replacement stops the worker, retains
Ask, and requires `session/load`. Structured error data is:

```json
{"state":"stopped","savedMode":"default","recoveryMethod":"session/load"}
```

Restart failure returns `-32603`, cancelled configuration returns `-32800`, and
busy configuration returns `-32000`. Repeated Stop/EOF cannot interrupt cleanup;
closing the same child/socket directory twice is safe. Busy/precondition errors
never silently widen authority. A missing transcript during an idle downgrade
stops Yolo rather than reviving it. These are ACP-owned settings, not a new native
auth/profile store or a solution to legacy concurrent writers.

## Other permission modes: possible with additional adapter work

Public beta.2 exposes all five names, but native RPC is headless. Its permission
gate has an ALLOW fallback where the TUI would ask. The [qualification receipt](permissions-qualification.json)
contains **five stock-native modes × four actual file effects**, with synthetic
loopback responses and no ACP bridge in that audit:

| Native mode | Project write | Outside-project write | Shell redirect write | Custom extension mutation |
| --- | --- | --- | --- | --- |
| default | Executed | Executed | Executed | Executed |
| yolo | Executed | Executed | Executed | Executed |
| plan | Blocked | Blocked | Blocked | Executed |
| auto-accept-edits | Executed | Executed | Executed | Executed |
| auto | Executed | Executed | Executed | Executed |

All targets were disposable synthetic files. This describes these exact RPC
cases; it is not a claim that every shell command passes native Auto/guardrails.
It also does not describe the interactive TUI's approval behavior.

| Additional mode | Feasibility | Required before advertising it in ACP |
| --- | --- | --- |
| Plan | Feasible as a separately validated adapter policy | Native Plan blocks known write/bash names but allows unknown extension mutations. Verify actual built-in read provenance and deny unknown/mutating tools; do not grant read-only status by name alone |
| Auto Edit (`auto-accept-edits`) | Feasible with native path checks plus an approval fallback | Reuse the pinned native containment/sensitive-path/symlink checks. Route outside-project, sensitive and shell cases to ACP consent instead of native headless ALLOW |
| Auto | Feasible with native classifier integration and bounded fallback | Preserve native DENY, allow only verified safe cases, route ASK/unavailable classifier to ACP consent and verify the actual execution shell/dialect |

They are **not impossible**, and they are **not implemented or advertised by this
candidate**. Simply adding names to the menu would either allow too much through
headless fallback or reduce every mode to Ask. Additional policy, provenance,
compatibility pinning, restart persistence and actual effects tests are needed.
Windows shell/classifier and installer acceptance remain separate.

## Actual host mismatch that blocks final acceptance

Happy-path actual AionCore tests pass: standard mode selector, Ask denial, Yolo
write without a prompt, preserved history, saved Yolo after host restart, Ask
downgrade, and pending-consent Stop. A forced failure exposes a separate state
problem:

1. Select Yolo, then request Ask while the test launcher forces new-worker failure.
2. The host returns HTTP `502/BAD_GATEWAY`. ACP stops the worker and saves Ask.
3. Remove the injected failure and use the host's runtime restart.
4. The host API displays **Yolo**, while a subsequent native write raises **Ask
   consent** and creates no file. Stop clears that consent without a write.

This is a **display/desired/effective mismatch, not demonstrated unapproved
execution or privilege escalation**. Both author and independent reviewer ran
the additional effect/consent probe. An early diagnosis based only on the Yolo
label was corrected before final documentation.

The pinned host's [session-load path](https://github.com/iOfficeAI/AionCore/blob/47e66d0d151123e973b3fd1e77afcb5671b3f8c5/crates/aionui-ai-agent/src/manager/acp/agent_session_flow.rs#L205)
overrides the adapter's returned current mode with the preloaded preference.
Its configuration service persists assistant preferences only on successful
configuration changes. Standard notifications before and after the load
response did not resolve the observed mismatch in this pairing. ACP cannot
truthfully claim worker readiness after failure or infer a new human choice
from a stale host preference. No timer, fabricated success response or host-name
policy emulation is used to hide it.

[Studio #16](https://github.com/handochan/aelix-studio/issues/16) must preserve
failure recovery state and make restored native/adapter policy authoritative
over a stale preference. Re-run this actual host test after that fix. Until
then, the full receipt reports `qualified_with_host_blocker`, with
`host.acceptance=blocked_on_host_display_effective_mismatch`, and ACP #5 remains
open. CI without the host reports only its native/ACP checks as passed and marks
host checks `not_run`.

## Validation and reproduction

Use a disposable public native wheel Python and a separately installed candidate
ACP wheel, as in [the baseline reproduction](README.md#reproduce):

```sh
.local/native/bin/python -B scripts/qualify_permissions.py \
  --adapter /absolute/path/to/installed/acp/bin/aelix-acp \
  --report .local/permissions.json

# Actual pinned macOS arm64 AionCore HTTP/stdio; renderer not exercised.
.local/native/bin/python -B scripts/qualify_permissions.py \
  --adapter /absolute/path/to/installed/acp/bin/aelix-acp \
  --host /absolute/path/to/pinned/aioncore \
  --report .local/permissions-host.json
```

Native and installed ACP bytes are checked before/after. The test-only launcher
adds a synthetic extension and injects startup failures without patching native
code. Receipt fields contain counts, hashes and observed state, not credentials,
raw conversations or provider diagnostics. Synthetic API/OAuth fixtures are not
real-provider acceptance. Python `-O` is refused.

Checks cover Ask allow/deny, pending-consent cancel, Yolo real shell Stop and PID
reaping/no late write, native guardrail and another extension's denial, native
active-tool selection, broken bridge, same-identity history/mode resume, forced
downgrade failure and repeated Stop during native startup. Source regressions
also exercise late prompt save, missing transcript, cancelled old-worker cleanup
and notification ordering. Existing public-wheel and management qualifiers are
rerun separately. Remote CI has not run for this unpushed candidate.

Studio source/UI, native wheels, auth roots and shared manifests are unchanged.
No new renderer, real-provider/OAuth, Windows/Linux host/installer, field or
closed-network installation acceptance is claimed. Current POSIX socket and
16 MiB/aggregate-history limitations remain.
