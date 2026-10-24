# Productization evidence — 2026-10-23

## Actual provider and browser follow-up — 2026-10-24

Installed ACP + the public beta.2 wheels passed **16 real Anthropic checks**,
including actual Haiku/Sonnet turns, approved/denied file effects, reasoning,
managed resources/profiles, stream cancellation and memory after ACP restart.
The existing Studio browser renderer also passed model selection, actual
messages, approval/denial, Stop and history restoration after reload.
The [live validation record](live-validation.md) links source/artifact evidence,
the screenshot and the original collector failures with their corrections.
This establishes scoped macOS browser/native acceptance; OAuth, public WebUI
authentication, other providers, packaged OS builds and release CI remain separate.

## Current Preview strengthening — 2026-10-24

The fixed final source passed **126 tests, zero skipped**, including **22**
public beta.2 subprocess tests and **28** control-v1 subprocess tests. Native
model responses are synthetic/local; process, file, permission and native API
effects are real. Ruff, actionlint and diff checks passed. The installed wheel
matches the runtime package source/assets; audited native source hashes match
the original public-wheel investigation.

The unchanged Studio AionCore host accepted that installed wheel, exposed real
model choices and confirmed both fixture/two selection and a runtime setting
as `observed`. This host probe makes no provider turn and does not establish
renderer, packaged-app, team-model or OAuth acceptance. No Studio source was
modified. No remote CI has run for this uncommitted working change.
Exact source/wheel hashes, versions and remaining acceptance are recorded in
[the strengthening evidence](reviews/evidence/2026-10-24-beta2-strengthening.json).

## Historical native-control evidence

The original native-control candidate evidence below is historical. The
subsequent beta.2 preview work adds a separate public-wheel path and repins
the control-v1 candidate to corrected native main `3a841059`. See
[beta.2 preview](beta2-preview.md) for the current contract and
[2026-10-24 strengthening evidence](reviews/evidence/2026-10-24-beta2-strengthening.json)
for post-integration results, public-wheel provenance and limitations; no full native-suite result below
is reattributed to the newer commit.

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
