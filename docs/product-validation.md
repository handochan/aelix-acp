# Productization evidence — 2026-10-23

Candidate: aelix-acp 0.2.0a1 + native runtime-control v1, exact source recorded
in `runtime-reference.json`. This is a development candidate for Studio
integration, not a release/packaged-app acceptance certificate.

## Locally observed

Host: macOS arm64, Python 3.12.13. Tests isolate HOME, native agent/settings
directories and sessions; synthetic keys never use the user's credential store.

```sh
python3 scripts/test_native.py --core .local/aelix-core
```

72 tests passed, including 18 black-box tests through the actual Aelix child
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
also exits without blocking the event loop. Additional unit tests cover hostile
framing, approval token/identity validation, policy provenance, pagination,
startup bounds, package consent and immutable app resources.

An isolated wheel installation also passed initialize, actual native session
creation, capability discovery, Plan selection, close and retained-index checks.
Its entry point imported from site-packages, with no checkout PYTHONPATH.

Native focused regression: `pytest tests/rpc tests/mcp_client -q` →
336 passed, 1 skipped. CLI selection/auth/session regressions in the four
changed fixture modules → 350 passed. Native lint/type/citation gates and the
full suite must also pass on the final source; see the companion PR for exact
results and CI. The full-suite run is recorded separately before delivery.

## Separate acceptance still required

| Level | State |
|---|---|
| ACP unit + native-process integration on this Mac | Measured above |
| Native full-suite / remote CI | Recorded with companion source change |
| Linux/Windows/macOS CI | Workflow added; require completed exact-head jobs |
| Installed Studio selectors, management screens and client actions | Not exercised by this backend suite |
| Packaged Windows lifecycle and clean-machine/offline installation | Not exercised locally |
| Real provider/model streaming and OAuth callback flow | Not exercised by synthetic endpoint |
| Actual package install/update/remove in managed distribution | Consent/argv/cancel tested; installation acceptance still needed |
| Real Analytics data + Studio chart/HTML/browser rendering | Existing exporter regression only; separate UI acceptance |
| Crash recovery of external side effects / durable subagents | Not promised; ambiguous turns are refused on replay |

The source adapter, core API and CI are reviewable work. Studio should consume
the pinned source only after its own integration and installation gates pass.
Do not upgrade its shipped bundle to an unpinned branch tip.
