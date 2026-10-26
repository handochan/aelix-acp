# Managed prompt operations

This is the opt-in adapter implementation for [ACP #12](https://github.com/handochan/aelix-acp/issues/12).
The matching [Studio #16](https://github.com/handochan/aelix-studio/issues/16) HTTP/UI
forwarding is a separate integration gate. Public beta.2 native wheels remain
unchanged. There is no generic management proxy or native auth/config mutation.

## Wire

Start with `--managed-operations`. `initialize.agentCapabilities._meta["aelix/operations"]`
advertises `schema=aelix.operation/1`, its immutable `storeId`, the concrete
`statusMethod=_aelix/operation_status`, and the `--operations-status-only` argument.
The latter transport opens only the existing store, advertises no load capability,
and refuses every method except initialize and operation status. It never starts
a native worker. Missing, copied, or physically replaced stores fail closed.

Session new/load may include `_meta["aelix/operationStore"]` with exactly `schema`
and the expected `storeId`. The response's same key adds `workerGeneration` and
`nativeSessionId`, observed from the actual worker handshake/state. Before prompt,
the host must persist that binding alongside the original ACP session ID.
Successful managed session/set_mode acknowledgements carry the same metadata:
a real worker restart changes workerGeneration, and a no-op preserves it. The
host updates its next execution fence from that acknowledgement; completed
operation replays still return their original worker/turn fences.

`session/prompt._meta["aelix/operation"]` has exactly these fields:

```json
{
  "schema": "aelix.operation/1",
  "id": "client-operation-1",
  "storeId": "negotiated-store-id",
  "conversationId": "owned-conversation-1",
  "turnId": "server-turn-1",
  "generation": "worker-generation-from-session-open"
}
```

Tokens are ASCII `[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}`. The private namespace scopes
each ID by ACP session; profile and conversation are checked. The fingerprint
uses the normalized accepted native message plus stable scope with HMAC-SHA256.
Transport generation/turn changes cannot create another semantic operation:
duplicates return the original execution fences. The current native JSON prompt
frame must fit 16 MiB minus a 1 KiB envelope reserve before claim.

The ordinary prompt result adds a bounded receipt under the same `_meta` key.
It stores schema/id/store/conversation/original turn/generation, state, allowlisted
code, optional stopReason, effectsUnknown, automaticRetry=false, and replayed.
It never stores text, model/tool output, arbitrary metadata, or exception strings.
`completed` means an observed native end plus nonstreaming/noncompacting state;
it is not exactly-once external effects. A cancel terminal keeps effectsUnknown.

| Condition | Code / result | Native execution |
| --- | --- | --- |
| Known completed/cancelled ID, same accepted body | Original bounded terminal, replayed=true | None |
| Known ID, different body or owner/profile scope | -32041 conflict | None |
| Known pending ID | -32045 + original receipt | None |
| Known interrupted/failed ID | -32046 + original receipt | None |
| Different/missing store identity | -32042 / -32043 | None |
| Stale generation or unverifiable ownership | -32044 | None |

Status params have exactly `sessionId` and `operation` (the six-field original
context). A not_found status still has effectsUnknown=true and automaticRetry=false;
it never authorizes synthesizing a new key. Pending status tries a fresh existing
OS lease only when it is not the current live turn. A still-held child reference
returns pending/recovery_waiting; only actual writer release permits interrupted.
No startup sweep, lease recreation, automatic pruning, or namespace reset exists.

`session/cancel` may carry the same original context. A mismatched operation,
turn, or generation returns cancelled=false/stale_operation without cancelling
the active turn. A matching cancellation is still only an intent until the
native terminal arrives; HTTP/stdio acknowledgement alone is not that terminal.

Unkeyed legacy clients retain their lack of deduplication. A managed launch
rejects the historical diagnostic-only `aelix/requestId` as an unsupported key.
The flag-off compatibility path still accepts it as a diagnostic, without dedup.
Removing the flag cannot reopen a persisted managed mapping/history unfenced.
Adapter launch prefixes cannot contain native history-selection flags (including
the public short resume/continue aliases) in either mode; use session/load.
Explicit Python/script launch prefixes remain supported. Arbitrary malicious
operator wrappers/extensions and older binaries are outside this protocol.

## Ownership and storage

One SQLite transaction creates the namespace and random 32-byte HMAC key. Claims
commit with FULL/WAL before native dispatch; allowlisted terminal commits precede
stdout publication. Publication/cleanup failures cannot overwrite a terminal.
The store records physical root/database identity and refuses substitution; local
records and reads are bounded. POSIX state/DB/lease/marker permissions are private.
Windows uses nonshare handles and complete 128-bit file identity; this is a
cooperative local protocol, not an OS sandbox against the operator/extensions.

The lease is acquired before native bootstrap and explicitly inherited through
the Windows pre-native Job gate into the actual native interpreter. The Job
handle remains noninherited. POSIX references close without LOCK_UN; native gets
its own process group for owned cleanup. The existing bridge proves nonce,
generation, held lease identity, and actual PID/process ownership before session
publication, and checks its reference before tools.

Native header UUID, physical file/parent identity, immutable adjacent marker, ACP
session/profile, namespace, and original lease converge before registration. A
fresh history stays unpublished until this binding completes. Only registered
legacy mappings can enroll; there is no arbitrary path/import/discovery API.
The public beta.2 session filesystem writes/appends using O_TRUNC/O_APPEND on the
same file (qualified installed `aelix_agent_core/session/fs.py`), so a copied file
with an identical header is rejected without altering native history semantics.
Incomplete/missing markers or managed stores are never silently recreated.

The unmodified public CLI and older adapters do not participate in this lease.
There is no common CLI writer lock, safe concurrent legacy mutation, arbitrary
backup/rollback or power-loss proof. Stop/EOF/kill may leave external effects
unknown, and uncertain operations are never automatically replayed.

## Verification

`tests/test_{leases,ledger,native_bindings,managed_server}.py` cover private
boundaries, real inherited child lifetime, concurrent duplicate/conflict,
publication failure, root/database/native replacement and partial state loss.
These are source/unit/OS-reference checks, not installed host acceptance.

Run the installed qualification with its own disposable wheel environment:

```sh
.local/native/bin/python scripts/qualify_operations.py \
  --adapter .local/native/bin/aelix-acp --report .local/operation-qualification.json
```

It verifies installed package identity and all 309 original public-native members
before/after actual model/tool execution, duplicate/conflict/pending/busy, scoped
cancel, consent denial, status-only restart, flag-off refusal, actual parent kill,
native lifetime barrier, interruption/no replay, EOF and credential/body exclusion
from SQLite/WAL. Provider responses and credentials are synthetic loopback only.
CI runs this on five native targets and Linux Python 3.11/3.12, retaining reports.
Exact-head CI, Studio HTTP/stdio/UI and actual five-target installers are distinct
gates; enabling this flag alone does not complete Studio #16 or Preview release.
