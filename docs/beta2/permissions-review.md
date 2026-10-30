# ACP #5 independent review and qualification boundaries

2026-10-25. Another Codex session reviewed the native wheel behavior and the
implementation, then independently reran the final source tests and full
native/installed-ACP/actual-host qualifier. It did not edit product code or Studio.

The review found and verified repairs for:

- A late old-prompt save could undo a permission revocation after `agent_end`;
  prompt ownership now extends through state/save finalization.
- Closing an old socket directory twice masked restart/cancel errors; close is
  idempotent and waits for cancelled consent handlers.
- Repeated Stop/EOF could interrupt cleanup; control tasks retain their reservation
  until shielded cleanup finishes.
- Cancelling a load while the old worker was closing could lose its owner;
  replacement retains both references and finishes old-worker cleanup before
  propagating cancellation, without starting a new child.
- Missing transcript or failed downgrade could preserve Yolo; the worker stops
  and stored policy is Ask, with structured recovery data.
- Restored mode notifications follow open responses; a later notification error
  cannot generate a second response for an already answered RPC request.

Final independent results: source tests **33/33**, actual stock native effects
**20**, installed ACP checks **11**, actual host normal flows **6**, and one
known host display/effective mismatch reproduced. The native original wheel
members (**309**) and installed ACP payload match before/after; harness hashes
match the final source. No unresolved ACP implementation blocker remained.

Author published receipt SHA256:
`bef32a07a7d94c546fcfa0c974d70a6b64f128b4b4c9742f17e0d097ea800ca2`.
Independent full receipt SHA256 (retained locally):
`57bdd38e569211cc81425507f2bab7a40fbf8b1e6667931ecf63344d65f88337`.

The reviewer also independently invoked the native permission hook in 25 cases,
separately from the 20 actual wire/file-effect cases. This does not turn a direct
hook invocation into tool execution or host evidence.

The failure investigation first observed a stale Yolo display. An additional
post-restart write/consent probe showed the actual policy remained Ask and no
unapproved file was created. Any initial hypothesis of actual re-elevation was
discarded. The final receipt and [contract](permissions.md) record the narrower,
reproduced display/desired/effective mismatch. Source attribution uses the pinned
host load response override; a separate cron full-auto override is not evidence
of general restart behavior.

ACP #5 is **not Done**: [Studio #16](https://github.com/handochan/aelix-studio/issues/16)
must resolve the host state mismatch before full product acceptance. Additional
Plan/Auto Edit/Auto modes remain an assessment, not advertised implementations.
Real-provider/OAuth, renderer, Windows/Linux host/installer, field and remote CI
were not accepted by this local review. Existing baseline/management and the
new no-host CI command are checked separately.

## Studio #16 recovery follow-up

The reviewer independently verified the separately identified patched host:
all 1,420 original source files, exactly three declared changes, patch/manifest/
Cargo.lock/LICENSE/binary identities; **35/35** ACP source tests; native **20**,
installed ACP **11**, host **10**, and actual Electron UI **7** checks. No blocking
finding remains for this permission-recovery scope. The review required the
final `ObservedConfigSynced` persistence event and a real visible permission
card assertion; both fixes were reverified.

SQLite Ask state is observed before full host process termination and restored
as Ask afterward, with no unapproved write. Later explicit Yolo still executes.
The source-built candidate is explicitly distinct from the released baseline;
the latter's mismatch remains reproducible. Independent wire receipt SHA256:
`79275b18a46ca2357b4cf256beb988614bbf490b660ffb03c628d0e41d31827f`.
The new [author receipt](permissions-recovery-qualification.json) records the
candidate identity. Source/host/renderer checks use a synthetic loopback model,
not real-provider or installer acceptance. Reviewed host delivery and the rest
of Studio #16 remain outstanding; neither integration Issue is marked Done.
