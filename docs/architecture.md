# ACP runtime-control boundary

Status: development candidate, 2026-10-23; realigned on main for the Beta, 2026-10-31 (ACP #20).

The adapter owns ACP v1 framing, presentation, process lifetime, session indexing
and client approval. Aelix owns models, authentication, native sessions,
settings, extension dispatch, resources, permission posture and delegation.
Shared behavior is exposed through an additive `aelix_control` envelope;
the original 29-command RPC roster is unchanged. The adapter does not copy
native command handlers or the credential store.

## Protocol

Negotiate ACP v1, including when a client requests an unsupported higher
version. Initialize precedes sessions. Use `configOptions` for selectors,
retaining legacy modes/models. Return complete catalogs after changes.
Configuration comes from actual runtime state, stripping provider HTTP headers.

Follow ACP [initialization](https://agentclientprotocol.com/protocol/v1/initialization),
[configuration](https://agentclientprotocol.com/protocol/v1/session-config-options),
[slash commands](https://agentclientprotocol.com/protocol/v1/slash-commands)
and [extensibility](https://agentclientprotocol.com/protocol/v1/extensibility).
Custom methods start `_`, capabilities live in `_meta`, error details in `data`.

Control commands never silently become model prompts. Unknown commands error.
Extension handlers use the native dispatcher; explicitly selected templates
and skills use native preparation before invoking the model. Client-only
actions require an advertised capability. Arbitrary terminal widgets still
need a client-specific adaptation.

## Lifetime and cancellation

Each session owns one child, approval listener, readers, pending requests and
an OS session lease. Startup reservations enforce the process limit during
concurrent creation. Failure before readiness closes the candidate. EOF and
adapter shutdown close children. POSIX native workers run in their own process
group; cleanup terminates it and kills members left after the leader exits.
SIGKILL follows SIGTERM after 5 s, or after 45 s for a native that lists
`children/list` or `children/notices`: its session-end stages (the turn's
settle, the jobs' owner end, the job service's shutdown) are bounded at about
40 s in sequence (aelix-ai contract r5 section 6.4), and a close shorter than
that would cut them. A native that lists only `children/notices` gets it too,
since its background jobs need the same stages (the lead's call). It is a
deadline, not a promise that native finished.
On Windows a trusted gate holds the native bootstrap until the worker joins a
kill-on-close Job Object, so the worker and every descendant, including MCP
servers started before the first RPC, end when the adapter's job handle closes,
even after a hard adapter kill. The extension manager subprocess keeps the
process-group/taskkill fallback. Pipe readers drain during shutdown so child
exit is not held open by a full output buffer.

Stdin uses a bounded raw-descriptor daemon on every OS, avoiding unbounded
queues, Windows Proactor pipes and blocked executor shutdown. Frames are bounded to 16 MiB; invalid
Unicode, nonfinite numbers and excess requests are rejected. Stderr drains in
chunks. Cancellation reaches native control tasks without waiting for their
mutation lock and cancels pending dialogs.

Stdout and forwarded native stderr use bounded daemon writers on raw descriptors. A client that stops
reading cannot block the event loop or Python's buffered-stdout shutdown lock.
EOF can cancel queued replies; disconnected clients must recover through history.

Changing extensions, trust or delegation restarts the child, preserving the
transcript and ACP identity. Client MCP configuration and UI support rebind
in memory. Supported session controls restore from saved preferences.
Branching creates a new file and ACP identity, restores the parent and leaves
navigation to Studio. Close preserves the index. A candidate worker keeps its
lease through the final save and registration. ACP checks the worker and lease
before commit and before publishing the live session. If the worker exits
before commit admission, the save rolls back. An exit observed after commit
rejects the live open and retains the committed session for `session/load`.

## Approval and trusted extensions

Automatic extension discovery is disabled. The bridge loads after selected
extensions. A launch nonce proves loading; a random token authenticates the
ephemeral loopback endpoint. Requests must match a known native tool-call ID
and name. Actual arguments after prior hooks are shown for consent. Invalid
authentication, failure, cancellation and deadlines deny execution.

Permission modes are Ask and Yolo. Ask requests client approval for every
tool; Yolo removes client prompts while native guardrails, tool restrictions
and other extensions' denials remain. Native posture follows through
`--permission-mode` at launch and `permissions/set`, read back with
`permissions/get`. A downgrade revokes client Yolo and its saved value before
native is asked; an upgrade applies only after native confirms. Native's
Plan, Auto Edit and Auto are not advertised: their headless RPC behavior has
not been accepted for Studio. On POSIX the channel is a private Unix socket in
a 0700 directory; on Windows a per-worker token authenticates loopback TCP.

Installed extensions and the process environment are trusted. Extension Python
can run during loading or commands with the host's authority. The bridge is
not a malicious-extension sandbox. Delegated children retain native restrictions.
With a children-capable native and a client-action client, a child of an Ask
session asks through `session/request_permission` (Delegated children below);
otherwise it runs under native `plan`.

## Persistence and ambiguous outcomes

SQLite uses WAL and FULL synchronization; the main file is private and a
symbolic-link DB is rejected. The old index migrates additively. OS leases
prevent concurrent writers to one stored ACP session and release on process
death. Lock files remain to avoid replacing locked inodes. Do not manually
point independent ACP identities at the same native transcript.

A stable `aelix/requestId` plus content fingerprint identifies a turn.
Completed retries return the stored result without model/tool execution.
Reusing a key for different content is rejected. Running/interrupted outcomes
are unresolved and never replayed blindly. Definitively invalid requests retain
their structured error. Without a client key each prompt is new.

Display history includes control replies, thoughts and tool updates. Each
live session queues updates in order and commits short SQLite batches off the
RPC reader. Approval replies and Stop ACKs continue while SQLite waits for a
writer. The queue includes the in-flight batch and has per-session limits
of 1,024 updates and 32 MiB of UTF-8 JSON. Overflow emits
`history_backlog_overflow`, flushes the accepted prefix, and stops recording
later updates on that open session. A failed batch rolls back and emits
`history_write_failed`. Live notifications and controls continue in either
case. Reopening replays the durable prefix, and the new worker can record
new updates.

Prompt input history and the running receipt commit before dispatch. Turn
completion waits for its queued history before writing the outcome receipt.
If that write fails, `turn_receipt_write_failed` is reported and the original
outcome is returned. The running receipt prevents automatic replay. A graceful
close drains accepted updates before releasing the session lease. A hard kill
can lose uncommitted history. Native transcript and SQLite use independent
transactions. A timeout leaves external tool effects uncertain. Protect
history as sensitive user data.

The native `agent_end` closes a provider attempt. ACP prompt completion instead
waits for native `get_state.isStreaming` to become false, including retries and
closing compaction. A missing saved model or other setting recovers to the
native choice with a warning. A saved agent profile that cannot be applied
refuses the session (`-32004`, `profile_unavailable`): the profile's tool and
posture limits would be lost, and AionCore v0.2.2 does not show warnings sent
during `session/load`. A saved tool selection the runtime cannot enforce
refuses the session too (`restrictions_unenforceable`).

Once a turn has sent native anything but a read-only request (the prompt, a
mutating control or command, a worker restart or a package change), a failure
that would otherwise be an internal error (`-32603`, or a session-not-found
`-32001`) becomes `-33002` with `retryable:false`. A failure that already has a
specific code keeps it: `-32007` (a native RPC timed out; native may have
started work), `-32000` (busy), `-32602`, `-32004`, `-32003`, `-32006`, `-32008`
and `-33001`. AionCore classifies every one of these as non-retryable, so its
turn recovery, which replays only retryable errors, does not resend the turn.
A client must not resend it automatically either. Recording the failure in the
adapter's own store cannot replace that code. When the adapter process itself
dies or the transport closes, there is no adapter error; that replay is the
host's to prevent (planned in handochan/aelix-studio-host#6).

A native writer-lock refusal ("already open in another terminal", whether at
startup or on a session switch) and a second adapter's ACP lease become
`-33001` (`session_locked`). Until aelix-ai reports the lock holder (Studio
request R7), native's refusal text is the only signal; one function in
`wire/errors.py` matches it so R7 can replace it.

With transcript v1 negotiated, session load stages the selected branch from
native `transcript/list` and reconciles it with ACP tool metadata and adapter
display events before replying. The host applies that projection only after
the successful load marker. See [Transcript synchronization](studio-integration.md#transcript-synchronization-acp-22-host-4).

Legacy session load prefers recorded ACP history. When it is missing, the adapter
scans the pinned native `get_messages` response in chunks, replaces native
images with text placeholders, and recovers text and tool calls in order.
It discards unused tool arguments while scanning. Oversized arguments and
tool results get fixed placeholders on their existing call IDs. The adapter
bounds retained history JSON to 64 MiB and normal tool-result content to
1 MiB. It stages short SQLite batches off the event loop and publishes the
complete cache before replay. Generic ACP clients receive native stderr
through the existing passthrough. The Studio Host path suppresses native
stderr. ACP reports incomplete or over-budget fallback history with
`native_history_unavailable` and the fixed message
`Native session history is unavailable`.
Native still serializes the response as a single line, so replies on that
pipe arrive after the line ends. The stdio measurement script is
[`scripts/measure_native_history.py`](../scripts/measure_native_history.py).

## Delegated children (aelix-ai #612)

Native's `children/` family, `aelix_child` and `aelix_notice` events and
`childApproval` dialog are mapped here; the client contract is in
[Studio integration](studio-integration.md#delegated-children).

Admission. `children/list`, `children/events`, `children/steer`,
`children/wrap_up` and `children/stop` are native's lock-free reads and
`CONCURRENT_METHODS`. The adapter sends them from `Session.intervene`, outside
`mutation_lock` and the per-session activity lease that a running
`session/prompt` or `agents/run` holds, so a host reaches a child during a turn.
They never mark that turn dispatched: they act on a child, and resending the
turn would not resend them. `children/notices` runs a turn and stays under the
lock (`Session.notice_turn`, sharing `drive_turn` with a prompt: the turn
timeout, `wait_until_idle`, `session/cancel`, and a reset of `cancelled` so the
bridge does not deny the turn's tools).

The fence. A counter on `Session` is raised inside `restart()` and `_close()`
themselves, so every path that replaces or ends the worker passes it: trust and
extension enable/disable (`_aelix/manage` and slash `/trust`, `/extension`),
delegation enable/disable (`_aelix/manage agents/enable`; `/agents` has no such
subcommand), `session/close` and `sessions/close` (both `Server.close_session`
since aelix-acp #35), `/quit`, `session/delete`, `session/load` of a live
session, compaction recovery, the close that follows a worker's ended RPC
stream, and adapter shutdown. Only the close's raise ends anything:
`raise_fence()`, the first step of `_close()`, ends each intervention native has
not answered yet: its line is already on native's stdin, so it answers `-33002`
with cause `session_changed` ("its delivery is unknown"). `restart()` raises the
counter first, a loop turn or more before its close, and that raise only refuses
new ones; an intervention whose answer is read in between returns it (fix round
2, F1: the verifier's run of Codex's `fence` case read the answer with the
counter at 1, and the reply was native's `queued`; `FenceOrderTests`, a steer
answered between a restart's start and its close). One native answered before
the close's fence rose returns that answer, on Python 3.11 to 3.14 alike (fix
round 1, Codex C-M1: 3.11's `wait_for` returned an answer that arrived after the
cancel and 3.12's did not; `tests/test_children.py` `FenceOrderTests`, both
interleavings, run on 3.11 and 3.12). A worker that dies first fails its
waiting requests itself (`-32603`, "Aelix process closed its RPC stream"),
before that close begins (the lead's call: that answer stays). While the counter
is up, the session is starting or closed, or its stream ended or its process is
gone, a new intervention is refused before it is written (`-32000`,
`session_changing`). At adapter shutdown the request tasks are cancelled before
sessions close, so an intervention then gets no reply, as every request does. A
native reload within one worker is fenced by native (its `transitioning`) and
passes through.

Events. `runtime/children.py` keeps `(epoch, id) -> parentToolCallId` from
each child's `state` frame, outside the per-prompt `SubagentTracker` reset: at
most 64 live children (the adapter's own bound on what it routes; an ended
child's route is dropped), an entry dropped on its terminal frame and every
entry of an epoch dropped when native names another epoch, in a frame or in a
`children/list` or `children/events` reply (an epoch seen replaced is
remembered, eight deep, and its late frames are dropped). Frames repeat nothing
already seen for `(epoch, id, seq)`. Beside each route it keeps the card fields
the child has shown, so a frame after the next prompt's per-turn reset folds onto
them and the entry keeps its `profile`, `state` and `wrappingUp` (fix round 1,
V-F1). The card's `_meta["aelix/subagents"]` gains `lastText`, `stuck`,
`wrappingUp`, `end`, and `currentTool` from the child's own `tool_start` and
`tool_end` (the tool of its latest call still open; fix round 1, Codex C-L7). A
client that declared `aelix/nestedToolCalls` also gets the child's own calls and
a synthetic card for a child with no parent call. `aelix_notice` becomes
`_aelix/notice` for a client that declared `aelix/notices`. Native names a parent
call whose id is longer than 128 bytes as the wire writes it (JSON-escaped
UTF-8, a control character six bytes) `h:` and the first 32 hex digits of its
sha256 (aelix-ai `aelix_agents/child_events.py` `wire_call_id`); the adapter
applies the same projection to the ids of the cards it opened, so such a child
still finds its card (fix round 1, Codex C-L6; `WireCallIdTests` compares the two
functions when the native is installed).

Replay. Every update is recorded. `session/load` replays a child's own calls
(`_meta.parentToolCallId`) and the synthetic `aelix-child:` card only to a
client that declared `aelix/nestedToolCalls`, as the live path sends them; the
parent card's updates are the same for both and replay to every client (fix
round 1, Codex C-M4; `ReplayTests`).

When the worker ends. `_close()` keeps reading native's stdout through its
shutdown (SIGTERM up to the deadline, then the exit), so the `tool_end` and
terminal `state` frames native writes while it stops a child are routed and
recorded like any other (fix round 1, Codex C-M3; against native `83714045` a
`workspace/trust` restart with a child's question open gave native's own
`tool_end` and `end: "stopped"`). A dialog native opens meanwhile is answered
`cancelled` without asking. Then, for each child still live when the worker is
gone, the adapter closes what it left open: its own calls `failed`, its entry
state and end `stopped` (native's end for a child a stop ended), marked
`_meta["aelix/child"].closedBy = "adapter"`, and its synthetic card `failed`;
the epoch is then retired (P-LOW-2). These updates are recorded; they are sent
only while the client is still connected and was not told the session was
replaced (`session/close`, `sessions/close`, `/quit`, a restart,
`session/delete` and a dead worker send them; adapter shutdown and
`session/load` of the same live session record them, and that load replays
them). `_aelix/notice` and `_aelix/subagent` read then follow the same rule and
are not recorded (fix round 2, N1; `EndingNotificationTests`).

Approvals. `childApproval` becomes `session/request_permission` in the
`extension_ui_request` task, outside `mutation_lock`; the reply is native's
`extension_ui_response` from the same task. The parent's own approvals stay on
the bridge, so the two can be open together (aelix-ai ADR-0308 K13). Every
ending that is not an offered choice is `{cancelled:true}`. A question native
drops while it is open is void: the child's terminal frame, the question's
`withdrawn` or `denied` approval step, or a newer epoch (frame or
`children/list` / `children/events` reply). The adapter then ends its own wait,
takes the request back from the client with ACP's `$/cancel_request`
(`{requestId}`), which a client is free to ignore, and answers native
`{cancelled:true}`, which native ignores (fix round 1, P-LOW-1). `session/cancel`
voids every open question, also during a manual `/compact` (P-LOW-3), and also
one whose task it cancelled before the task first ran (the cancel and native's
request handled in one loop turn): that task's done-callback answers native
`{cancelled:true}` once, for an extension's dialog too, which a cancel one loop
turn later already answered so (fix round 2, F2;
`CancelBeforeTheDialogRanTests`). An
extension's dialog opened during `/compact` keeps its earlier behaviour (it is
not cancelled, since the compaction goes on). The adapter waits up to 330 s for
the client, past native's own 300 s (aelix-ai `rpc/control_ui.py`
`DIALOG_TIMEOUT_SECONDS`): at 300 s native answers the child itself ("no answer
from the host within 300 s") and its `denied` step takes the request back; the
330 s only backs that up (P-LOW-4; other client requests keep 290 s; read from
native's source and held by a unit test with shortened bounds, a 300 s wait not
run). Native asks one child question at a time (its permission gate's lock,
`decide_for_child`), so a second child's question waits until the first ends. A nesting client's request keeps
the nested card's own title (the tool's name); a client without nesting gets
native's descriptive title (NIT-5).

Cancel cadence (aelix-ai #800 K5). A cancelled turn that still streams is
aborted again only when 2 s (native's own re-abort grace) have passed since the
previous abort sent by any path, not at every 50 ms poll; `drive_turn`'s
re-cancel after native accepted a prompt obeys the same cadence. Each client
`session/cancel` sends one abort at once: its own after `commands/cancel`, or
none if the turn's poll aborted while `commands/cancel` waited (fix round 1,
Codex C-M2: those two were 0.3 ms apart).

How it is held, against native aelix-ai `83714045` on macOS (the Windows paths
are unchanged and not run here): `tests/test_children.py` (no native: routing,
dedupe and epochs, the card fields across the per-prompt reset, `currentTool`,
the projected parent id, replay by capability, approvals and their option
mapping, void questions and `$/cancel_request`, which requests are taken back,
a dialog whose task a cancel ended before it ran, the timeout bounds, the
fence's two answers, its two orders, a restart's counter that ends nothing and
`session/close`, the cadence, the notice turn, the deadline choice, the routes'
bounds, the `_aelix/` notifications while the worker is ended, and closing with
a POSIX worker that writes or does not write its last frames) and `tests/test_native_children.py` (a real native and a scripted model:
a steer and a wrap-up during `session/prompt`, a steer and a stop during
`agents/run`, refusals during three restarts, a steer in flight when a restart
begins (POSIX), nested calls of a spawned child after its call ended, a spawned
child's entry after the next prompt, the card-only view without the
capability, `children/notices` after a `session/cancel`, allow-once and
reject-once, `session/cancel` denying an open child question, `children/stop`,
`children/wrap_up` and `resources/reload` taking an open question back, a
restart with a child asking closing its cards, a parent's and a child's question
open together, the cancelled call's report replayed by `session/load` (a guard
that passes on `a78db59` too), and one abort in the re-abort window for one
cancel, read from native's receipt times so a runner that settles after 2 s may
count the re-abort that is then due). Closing measured with a scripted model at
`20f1d27` (with the shutdown read): an idle native, one with a spawned child
held in its model call and one with a background `bash` job were gone
0.08-0.55 s after the request through `sessions/close`, `session/delete` and
adapter shutdown, and 0.61-1.01 s through a `session/load` of the same session
(which starts the new native), so neither the 45 s deadline nor the read added
latency there; a native whose session end took 8 s (a `session_shutdown` hook)
finished in 8.08-8.12 s (8.60 s through `session/load`) and was killed at
5.01 s on `a78db59` ([product validation](product-validation.md)).

## Credentials, MCP and packages

Use the held AuthStorage, return only provider status and omit management keys
and OAuth replies from history. Clients must not log auth payloads. OAuth
URLs/progress are transient events.

MCP configuration is validated and replaced transactionally. Status and saved
preferences omit environment, headers and URLs. A task owns each AnyIO
transport lease and closes it in the same task; native reload reattaches client
tools. Only explicit environment values join the SDK's safe base.

Extension operations use argv, bounded output, deadlines and consent for
package changes, enablement and trust/delegation changes. Signed app bundles
are immutable: provide a managed writable runtime. Installing a package in
another environment does not make it importable by bundled Python.
Offline disables catalog/package network paths, not provider or extension
network calls. Closed-network installers need local wheels, models, MCP
dependencies and CA configuration.
