# PR #1 review against the public beta.2 Preview baseline

Reviewed exact head: `8deb482bcd53261762979cf0e6ce1302dcc821c1`, Draft
[PR #1](https://github.com/handochan/aelix-acp/pull/1). Review-time main:
`cf2911f9842d6ae6a7a2fedb0b68791fe0b07f59`; merge base: `71eabb42b3c97e089e7d8601bc32b310ee40dbea`.
An independent session inspected the source. Parent-run checks below are separate
from that read-only review. No change to the existing PR or dirty work was made.

## Integration decision

Do not merge this entire PR as public beta.2 Preview support. Its
runtime-reference pins private native source `10854a2d4cfc089cdf021c3f2e251bce9f230d94`
and requires runtime-control v1. The installed PR wheel against four unmodified
public beta.2 wheels actually refused default session creation with code
`-32004`, `Aelix runtime control v1 is required`. This is an expected boundary,
not a regression against the PR's original private control-v1 purpose.

Exact-head run [39897711016](https://github.com/handochan/aelix-acp/actions/runs/39897711016)
is completed success, 15/15 jobs. It covers that PR/native candidate pairing,
not public beta.2/Studio/main acceptance. The parent also reran its fixture suite:
92 tests discovered, 64 passed, 28 native integration tests skipped. We did not
rerun those 28 private-runtime integrations. A source or CI change requires a fresh review.

## P1: operator extension disable reports success without changing its owner

- [runtime/extensions.py:70](https://github.com/handochan/aelix-acp/blob/8deb482bcd53261762979cf0e6ce1302dcc821c1/src/aelix_acp/runtime/extensions.py#L70)
  removes only adapter `extra_extensions`, restarts and returns selected refs/ready.
- [session.py:125](https://github.com/handochan/aelix-acp/blob/8deb482bcd53261762979cf0e6ce1302dcc821c1/src/aelix_acp/session.py#L125)
  adds the operator `server.args.extension` on every launch.
- Parent unit execution of the actual PR action, with approved synthetic control
  replies, returned `enabledExtensions=[]`, `ready:true`; the operator source
  remained configured. Continued native hook loading follows the startup source
  path; actual hook/tool persistence was not executed in this reproduction.

Reject disabling operator/profile-owned references or explicitly update their
owning configuration. An off result must match actual hook/tool behavior.
Tracked in [ACP #9](https://github.com/handochan/aelix-acp/issues/9).

## P1: legacy resume silently skips saved grants

- [session.py:161](https://github.com/handochan/aelix-acp/blob/8deb482bcd53261762979cf0e6ce1302dcc821c1/src/aelix_acp/session.py#L161)
  skips a saved agent if `agents/use` is absent without setting recovery failure.
- [session.py:193](https://github.com/handochan/aelix-acp/blob/8deb482bcd53261762979cf0e6ce1302dcc821c1/src/aelix_acp/session.py#L193)
  skips saved `activeTools` if the native setter is absent; stored Yolo can still
  be restored by the preceding permission path.
- Parent actual-process reproduction used the installed PR wheel, public native
  wheels and a synthetic loopback model. A created ACP session received seeded
  persisted preferences `agent=restricted-fixture`, `activeTools=[]`, `permission_mode=yolo`.
  Loading the same native session with `--allow-legacy-runtime` succeeded and
  returned Yolo. The actual model request contained eight tools despite the saved
  empty selection. This is a seeded restore regression, not a complete migration
  of a real control-v1 session or proof of a bypass in current main.

Fail startup or require explicit migration when saved restrictions cannot be
enforced. Never silently recover restricted identity into wider grants.
Tracked in [ACP #10](https://github.com/handochan/aelix-acp/issues/10).
The bounded [reproduction receipt](pr1-reproductions.json) contains no raw transcript.

## Candidate reuse map

| Candidate | Treatment before adoption |
| --- | --- |
| wire/transport.py, wire/errors.py | Reuse validation/bounds selectively; repeat installed-entrypoint tests |
| wire/process.py | Review POSIX/Windows owned-process cleanup independently |
| runtime/store.py, runtime/leases.py | ACP index/history only; do not claim a legacy native CLI writer lock |
| bridge.py | Keep consent/nonce/token fail-closed behavior; verify public native symbol fallbacks |
| session.py | Select classic RPC/lifetime pieces; repair restore restrictions and remove the Preview control-v1 dependency |
| runtime/controls.py | Reuse selector shape, bind actual public native state, advertise only enforced capabilities |
| runtime/commands.py | Do not copy all commands; reuse Aion UI surfaces and exclude new `/clear` work |
| control-v1 auth/MCP/profile/branch calls | Preserve as a future runtime path; not default public beta.2 support |

The current PR advertises MCP/permission/command surfaces before native checks;
legacy startup cannot honor all of them. Capability advertising must follow the
actual selected runtime. Existing dirty beta2 work may already address parts of
this map, but it remains uncommitted candidate code and requires an exact diff review.
The known 16 MiB ACP/native aggregate history boundary is retained as an existing
P2 limit, not counted as a new finding or described as fixed.
