# ACP #7 independent qualification review

2026-10-25. An independent Codex review session read the Issue worktree and ran
the native/helper/installed ACP/pinned AionCore qualifier independently. It did
not edit the implementation, original dirty checkout or Studio sources.

The reviewer found and verified fixes for these qualification defects:

- Replace `assert __debug__` with an explicit optimized-mode refusal.
- Propagate tap reader failures and bound child/group cleanup.
- Require helper EOF to return a cancelled terminal and exit zero before any
  forced cleanup; check that no OAuth credential was committed.
- Check actual selector values and persisted history across restart.
- Require a cancelled terminal separately for consent cancellation and held
  provider cancellation.
- Remove a debug-output dependency on a preexisting local directory.
- Bind all five new executable harness files to receipt hashes.
- Link actual follow-up Issues instead of leaving an unassigned proposal.

The reviewer independently ran the full qualifier and the existing 21 source
tests, confirmed optimized-mode refusal, and checked that no probe child remained.
The independent full rerun preceded the final extra assertions that distinguish
the two cancellation paths; the reviewer then inspected those assertions and
the author's final passing receipt. No unresolved P1/P2 findings remained.

Final published receipt SHA256:
`5500565ac6e6c075f0007e318962bec10af63f4156a98b48174d6f92765eb3ce`.
Its five new harness hashes match the final source; 309 original native wheel
members and the installed ACP payload match before/after. It records helper 12,
shared-auth 2, adapter 6, actual-host 11 and unauthenticated-host rejection 1.
The two cancelled terminals and bounded reverse-RPC no-response observation
(4.732 seconds) are actual wire results, including expected gaps.

The separate review rerun receipt is retained locally; SHA256
`30b34070ae9db4e542a1ff395e680e1542968fde6d8ed961b4f2d58899d4e6eb`.
Source tests and qualification success do not establish remote CI, renderer,
real-provider/OAuth, Windows/Linux installer or field acceptance. The newly
added CI job has not run remotely in this unpushed work.
