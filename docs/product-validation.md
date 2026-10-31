# Productization evidence

## Beta realignment — 2026-10-31 (ACP #20)

Candidate: this adapter on ACP main plus native aelix-ai
`e19ab55e17e871a40356fc9802f3f6669ca727de` (main before the beta.3 cut,
including delegated child results #615 and `agent action=resume` #610/#626).
Re-pin to the beta.3 release tag when it is cut; control-v1 is not frozen yet
(aelix-ai #623), so the native suite must be rerun on that tag.

Host: macOS arm64, Python 3.12.13, synthetic loopback model, isolated HOME and
native directories. Levels are kept separate:

| Level | What ran | Result |
|---|---|---|
| Source | Unit suite (`python -m unittest discover -s tests`) | Pass; native-process tests skipped without a runtime |
| Native process | `scripts/test_native.py --core <aelix-ai at the pin>`: every unit test plus 34 tests through an actual native child | Pass (Windows Job test skipped on macOS) |
| Native process | `scripts/test_packages.py`: offline starter wheel install, enable, `/hello`, update, disable, remove | Pass |
| Host | `scripts/smoke_host.py` with the AionCore `v0.2.2` release binary (`aioncore-v0.2.2-aarch64-apple-darwin.tar.gz`, binary sha256 `04404864ca622159ffcc79ef9cb468e82fc820b91f16d565c5a5efdb0b0c4cb1`; the same source as aelix-studio-host main `47e66d0d`) | Pass, see below |
| UI | Studio renderer / Electron | Not run |
| Live provider | Real model provider, OAuth | Not run |

Host smoke checks, through actual host HTTP → host ACP client → this adapter →
native, with a structure-only wire tap and the host's own SQLite rows:

- A provider failure after native dispatch reaches the host once as `-33002`;
  the host sends no second prompt and no reload, and the model sees one request.
  The same flow with the PR #1 adapter (`-32603`) made the host start its
  automatic replay: it re-initialized the adapter and sent `session/load`. That
  attempt then stopped on the previous adapter's still-held session lease, so
  this run shows the replay starting, not a second model call.
- A model-driven `agent` delegation streams parent `tool_call_update` frames
  with `_meta["aelix/subagents"]` while the call is `in_progress`; the host
  persists the parent `acp_tool_call` row with the final child (`reviewer`,
  `done`) and returns it from the conversation messages API.
- With a separate native process holding the session file, the host's resume
  receives `-33001` (`session_locked`, `lockedBy: native`), records the error,
  does not fall back to `session/new` and sends nothing to the model. After the
  holder exits the same conversation resumes.

Native-process coverage added for this change: Ask/Yolo effects and native
posture read-back; refusal of Plan/Auto Edit/Auto; launch-owned extension
disable refused while its command keeps running (#9); a disabled extension
leaves native's inventory; seeded `agent=restricted-fixture`, `activeTools=[]`,
`permission_mode=yolo` resumes in Ask with a warning and an actual model request
that carries no tools (#10); a real native lock holder refuses `session/load`
with `-33001`; a provider failure with native retry off returns `-33002` after
one model request; a parented child never uses `_aelix/subagent`.

Not covered: Windows and Linux are left to exact-head CI; packaged
installers, the Studio renderer showing the `_meta`, and real providers.

Host-side replay after an adapter crash: when the adapter process itself
dies, or the transport closes, after `session/prompt` was sent, there is no
adapter error to mark. AionCore v0.2.2 classifies the closed transport as
retryable and replays the turn once (reproduced: SIGKILL of the adapter
mid-request, the model received the turn twice). By the user's decision of
2026-10-31 the host fixes this in handochan/aelix-studio-host#6, and the
adapter no longer carries the managed-operation contract that covered it for
Preview ([ACP #20 decision](https://github.com/handochan/aelix-acp/issues/20#issuecomment-6536397248)).
A manual resend after a lost HTTP response is a new turn by the same decision.

## Development candidate — 2026-10-23 (historical)

The rest of this file records PR #1 at native `10854a2d4cfc089cdf021c3f2e251bce9f230d94`.
It includes the Plan/edit modes that the Beta realignment no longer advertises.

Candidate: aelix-acp 0.2.0a1 + native runtime-control v1, exact source recorded
in `runtime-reference.json`. This is a development candidate for Studio
integration, not a release/packaged-app acceptance certificate.

## Locally observed

Host: macOS arm64, Python 3.12.13. Tests isolate HOME, native agent/settings
directories and sessions; synthetic keys never use the user's credential store.

```sh
python3 scripts/test_native.py --core .local/aelix-core
```

92 tests passed, including 28 black-box tests through the actual Aelix child
and adapter JSONL transport. A local HTTP endpoint emits synthetic model SSE;
the MCP fixture is a real local stdio process. These are real process, file,
protocol and native service checks with synthetic model responses.

Coverage includes actual approved/denied writes, Plan/edit modes, model and
reasoning changes, native extension commands/dialogs/cancellation, unknown
slash rejection, request deduplication and changed-content rejection,
restart/history/model/profile restoration, settings/reload, credential status
redaction, extension enable/disable, non-destructive clone, MCP tool forwarding
and restart rebind, one-session OS leases, native delegation/progress and
owned extension-manager cancellation. A disconnected client with unread stdout
also exits without blocking the event loop; a chatty child is drained and
terminated on disconnect. Additional unit tests cover hostile
framing, approval token/identity validation, policy provenance, pagination,
startup bounds, package consent and immutable app resources.

An isolated wheel installation also passed initialize, actual native session
creation, capability discovery, Plan selection, close and retained-index checks.
Its entry point imported from site-packages, with no checkout PYTHONPATH.

Native focused regression: `pytest tests/rpc tests/mcp_client -q` →
336 passed, 1 skipped. CLI selection/auth/session regressions in the four
changed fixture modules → 350 passed.

Full native suite on commit `10854a2d4cfc089cdf021c3f2e251bce9f230d94`:

```sh
env -u NO_COLOR TERM=xterm-256color PATH="$PWD/.venv/bin:$PATH" \
  .venv/bin/python -m pytest -p no:cacheprovider -q --tb=short
```

18,370 passed, 28 skipped, 114 warnings in 829.72 seconds. Ruff, type gate
(298 files, zero errors), citations and generated contract schema also pass.
Use the supported virtualenv and a color-capable terminal environment for
the full suite; the inherited system Python 3.9 / NO_COLOR=1 / TERM=dumb do
not satisfy its packaging and renderer test premises.

Actual package lifecycle through ACP also passed in a disposable native
environment with isolated HOME and `--offline`: local starter wheel install,
metadata inventory, enable, `/hello`, recorded-source update, disable and
remove. This used native CLI installation, not a fake package manager:

```sh
python scripts/test_packages.py --aelix /disposable/environment/bin/aelix \
  --wheel /path/to/aelix_starter_ext-0.1.0-py3-none-any.whl
```

Run this with Python 3.11+. It modifies the explicitly supplied disposable
environment and refuses a preinstalled starter. The script is also included
in each native CI platform job. Update uses the installed distribution name;
remove uses the extension entry-point name.

## Separate acceptance still required

| Level | State |
|---|---|
| ACP unit + native-process integration on this Mac | Measured above |
| Native full-suite on this Mac | 18,370 passed, 28 skipped |
| Linux/Windows/macOS CI | Workflow added; require completed exact-head jobs |
| Installed Studio selectors, management screens and client actions | Not exercised by this backend suite |
| Packaged Windows lifecycle and clean-machine/offline installation | Not exercised locally |
| Real provider/model streaming and OAuth callback flow | Not exercised by synthetic endpoint |
| Actual local-wheel package lifecycle on this Mac | Measured above; indexes/catalogs and packaged-app distribution still need acceptance |
| Real Analytics data + Studio chart/HTML/browser rendering | Existing exporter regression only; separate UI acceptance |
| Crash recovery of external side effects / durable subagents | Not promised; ambiguous turns are refused on replay |

The source adapter, core API and CI are reviewable work. Studio should consume
the pinned source only after its own integration and installation gates pass.
Do not upgrade its shipped bundle to an unpinned branch tip.

## Independent review follow-up

Claude Code 2.1.309 was run with `--model claude-opus-5-5 --effort xhigh`
and read-only tools against an immutable snapshot. Its original report and
the accepted/reproduced fix dispositions live under `docs/reviews/`.
Review output, parent-run reproductions, synthetic integration and exact-head
CI are separate evidence. The native core working branch was not modified
during this follow-up; the runtime pin remains unchanged while core review proceeds.

Added cases cover native path containment/sensitivity, provider-attempt retry
versus prompt completion, handled input with no agent_end, missing model/profile
fallback, MCP profile/selection restoration and removal, independent SQLite
writers, early bridge requests, slash cancellation, command deadlines, deletion
serialization and launch preferences after lease admission. Known native frame
size/aggregate-history limits remain explicit in the review disposition.
