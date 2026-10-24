# Aelix ACP — Agent Guide

## Shared Studio delivery operations

For Studio Preview/Beta work, read the README of
[Project 9](https://github.com/users/handochan/projects/9) before choosing work.
Exact project ID: `PVT_kwHOA-ATMs4BojoA`. This is the shared board for
`aelix-ai`, `aelix-acp`, and `aelix-studio`; do not substitute another project.

```sh
gh project view 9 --owner handochan --format json --jq .readme
gh project item-list 9 --owner handochan --format json --limit 100
```

- Follow the user's explicit task scope. For queued product work, take only an assigned Ready Issue with satisfied dependencies; convert draft cards before implementation.
- One Issue, one owner, one branch/worktree. Check existing work and claims; preserve unrelated dirty files. Initial independent implementation concurrency is at most two.
- Obtain one independent review by another session; Claude/Codex may alternate implementation and review. Recheck affected validation after fixes.
- Update Issue/Project at start, review submission, blockage, and stop/completion. Handoff records current SHA/PR/worktree, verification, next action, and blockers.
- Use `needs-decision` only for product/compatibility/release choices; use `blocked` for unmet dependencies. Do not turn routine implementation choices into owner approval requests.
- Done requires the Issue's acceptance and relevant merge/verification evidence. A single ACP PR does not complete a cross-repository Studio integration or release.
- Preserve existing authorization boundaries. These files do not grant new push, merge, release, or external-publication permission.

## Product and adapter boundaries

- Preview uses released beta.2 native wheels without source patches or forced private-core upgrades. Verify source/wheel identity and actual capabilities; a `0.1.0b2` version string does not prove released-wheel behavior.
- Through the first Beta, reuse Aion UI surfaces and connect actual native state. A separate implementation of every slash command is not required; new `/clear` implementation is excluded.
- Ask/Yolo is a design target until implemented and accepted; do not advertise unsupported modes or emulate native capabilities from names alone.
- Keep native configuration/auth/session semantics in Aelix. ACP translates protocol and capabilities; verify actual native history for fork/import/clone.
- Read [README](README.md) and the current CI workflow for code/validation contracts. If the checkout includes `docs/architecture.md` or `docs/studio-integration.md`, read those candidate contracts too; do not assume a candidate branch has merged. Distinguish source, synthetic, live-provider, host UI, and installer evidence.
