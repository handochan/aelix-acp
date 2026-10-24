# Published beta.2 Preview contract

Updated 2026-10-24. This is the current ACP working-tree contract, not a released
wheel or a claim that Studio management screens are implemented.
The [feature/ownership matrix](beta2-feature-matrix.md) records the full TUI
comparison, remaining native boundaries and release sequence in Korean.

## Runtime selection

Default `--runtime-profile beta2` uses public PyPI wheels for `aelix`,
`aelix-ai`, `aelix-agent-core` and `aelix-coding-agent`, all `0.1.0b2`.
It never sends an `aelix_control` probe. The separate `control-v1` profile
requires actual version-1 capabilities from the candidate pinned by
`runtime-reference.json`; it keeps its native control path unchanged.
The main checkout's package version also says beta.2, so version text is not
proof of public-wheel compatibility. Repeat acceptance against released beta.3.

## Model and tool controls

Public beta.2 fails classic RPC model selection because its CLI does not pass
ModelRegistry to run_rpc_mode. Its public extension API does provide the live
registry, authenticated model filtering, set_model and tool selection.
The packaged approval bridge now loads `bridge_controls.py` by trusted file
path inside the native interpreter; ACP need not be importable there.
This is production-path integration of the earlier extension-only probes.

After session_start, a private nonce-bound endpoint advertises an authenticated
loopback listener. Requests have random correlation IDs, a 1 MiB frame bound,
8-connection admission bound, operation serialization, native idle checks,
30-second native / 35-second parent deadlines and explicit cleanup. Model
records use a field allowlist; HTTP headers and auth payloads are omitted.
Plugin/provider exception details are not echoed to clients.
Cancel closes pending control connections, interrupts waiting native hooks and
returns an error to the original ACP request. A cancelled/failed model change
refreshes actual native state: cancellation does not prove rollback.

Models are returned through standard configOptions and legacy models. A switch
preserves process identity, ACP identity and history. Reasoning uses the native
supported-level helper and classic awaited RPC setter. Tool selection is bounded
by the session's initial launch grant, including profile restrictions; `[]`
means no tools. Saved tools fail closed if the bridge/grant cannot enforce them.
Without an available bridge and no saved restrictions the adapter retains the
older read-only fallback; it never fabricates native control-v1 capabilities.

## Management API

Discover `_aelix/capabilities {sessionId}`. `runtime` remains the actual native
control envelope (empty for beta.2); `adapterMethods` lists supported Preview
operations and `modelSwitching` describes the effective model route.
Send `_aelix/manage {sessionId,action,arguments}`. Mutations require idle sessions.

| Action | Arguments | Preview behavior |
| --- | --- | --- |
| sessions/close | {} | Close native process; retain index/history |
| settings/get | {} | Session configOptions, selected resources/profile |
| settings/set | {key,value} | Standard session config ID; boolean/string choices |
| resources/list | {} | Launch-granted tools, active selection, managed resource commands/diagnostics and selected extension/profile metadata |
| tools/set_active | {names:[...]} | Live native public tool API; cannot exceed launch grant |
| resources/configure | {inheritSkills?:bool,skills?:[path],promptTemplates?:[path]} | Replace adapter resource selection, approval + restart; not global native settings |
| resources/reload | {} | Approval + native child replacement, same ACP ID/transcript |
| extensions/list | {} | Operator and adapter-selected references, not installed distribution inventory |
| extensions/action | {action,target?} or {command} | Native CLI list/verify/discover/install/remove/update; adapter enable/disable/reload |
| agents/list / agents/show | {} / {name} | Native discovery/resolve; name or absolute profile path |
| agents/save | {name,content} | Approval + native validated Markdown creation in native user agents dir; existing files refused |
| agents/use | {name} | Approval + native compiled profile startup; clears prior config/tool overrides. name="--none" deliberately clears the profile |

Example skill allowlist and prompt addition (paths already authored in Studio):

```json
{"sessionId":"...","action":"resources/configure","arguments":{"inheritSkills":false,"skills":["/workspace/skills/review"],"promptTemplates":["/workspace/prompts"]}}
```

The session exposes slash commands only for explicitly managed skill/template
snapshots, expanded with public native loaders and formatters. Ambient loaded
resources and arbitrary extension command contexts are not exposed by beta.2's
public extension API. Resource listing does not claim authoritative native
loader inventory. `loadState: not_observed` after extension replacement reports
this limit; a completed restart does not prove each selected extension loaded.
Operator/profile-owned extensions cannot be disabled through adapter selection.

Resource/profile changes require client permission before effects. Native file
creation does not overwrite existing profiles. Extension package writes use a
writable manager outside signed app resources, bounded output and owned-process
cleanup. Once replacement begins, cancellation waits for replacement/cleanup;
inspect actual state before retrying. No rollback or exactly-once claim is made.

Profile files use native AgentProfile format and trust validation. Source hashes
are checked on resume; a changed/deleted profile refuses startup rather than
silently removing grants. Automatic conversion to control-v1 currently refuses
sessions with Preview resource/profile preferences; migration is a beta.3 gate.

## Remaining boundaries

Preview exposes only Ask. Advanced permission modes are neither offered nor
accepted. All tool executions retain the authenticated approval hook.
Client MCP servers (including stdio) are rejected before replacing an existing
healthy session; initialize advertises HTTP/SSE=false. Arbitrary extension
commands/dialogs, live auth, native settings inventory and delegation require
control-v1. The adapter disables native agents for Preview.
Unknown/unsupported slash commands never become LLM input.

The native aggregate JSONL/history limit is still 16 MiB. This work does not
provide native pagination, an extension sandbox or automatic replay of ambiguous
external effects. Completed-turn deduplication still requires stable client keys.

## Evidence and reproduction

The 2026-10-23 registry and feature probes are historical feasibility evidence;
the older 112-test record describes the pre-integration implementation.
Current verification is recorded separately in
[2026-10-24 strengthening evidence](reviews/evidence/2026-10-24-beta2-strengthening.json).
Actual ACP/native subprocesses and synthetic local model endpoints are used;
that regression record does not establish real-provider acceptance. The later
[actual live validation](live-validation.md) covers 16 direct Anthropic checks
and the existing Studio browser's core conversation/selector/approval flow.
OAuth, public WebUI auth, dedicated management screens and packaged OS builds
remain separate gates.

```sh
uv venv --python 3.12 .local/beta2
uv pip install --python .local/beta2/bin/python --only-binary=:all: aelix==0.1.0b2
.local/beta2/bin/python scripts/test_beta2.py --python .local/beta2/bin/python
AELIX_ACP_TEST_BETA2="$PWD/.local/beta2/bin/aelix" \
  python scripts/test_native.py --core /path/to/pinned-native-candidate --check-ref
```

The beta.2 runner rejects source/editable distributions. On Windows use the
virtualenv Scripts/python.exe and Scripts/aelix.exe paths. Native source is not
patched and no duplicate credential store or private harness mutation is used.
