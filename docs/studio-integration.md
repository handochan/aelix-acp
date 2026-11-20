# Studio integration contract v1

Handoff for the separate `aelix-studio` project. This is an API contract,
not evidence that its UI is connected. Bundle the ACP version and exact
native workspace commit from `runtime-reference.json` together. Older public
Aelix lacks these operations. Advertise client actions only after implementing them.

## Standard ACP

1. Initialize with `protocolVersion: 1` and respect the negotiated result.
2. Create/load with absolute `cwd` and `mcpServers`.
3. Render returned `configOptions` and complete `config_option_update` catalogs.
   Legacy modes/models also exist. Model values are opaque `provider/id` strings;
   the model portion can contain `/`.
4. Use `session/set_config_option` with `sessionId`, `configId`, `value`.
   IDs: `permission_mode`, `model`, `thinking_level`, `steeringMode`,
   `followUpMode`, `autoCompactionEnabled`, `autoRetryEnabled`. Boolean options
   use string `true`/`false`. Replace the catalog after model changes.
   `permission_mode` is `default` (Ask) or `yolo`. A `current_mode_update`
   follows every new/load response with the restored mode.
5. Render available commands, text/thought chunks, tool calls, updates, status,
   raw input/output and file locations. Answer `session/request_permission`
   with a listed option ID. Dismissal denies/cancels.
6. Send slash commands as normal prompt text. Do not execute a second client
   copy. Unknown commands fail; `//` is ordinary text.
7. Support cancel, paginated list (`nextCursor` → `cursor`) and delete.
   Delete removes ACP records, retaining native transcript files.

Input supports text, text embedded resources, resource links and base64
PNG/JPEG/GIF/WebP up to 8 MiB decoded per image, subject to 16 MiB frames and
model support. Audio/video and a general HTML renderer are absent.

Generate a stable turn ID before first send:

```json
{"jsonrpc":"2.0","id":10,"method":"session/prompt","params":{"sessionId":"...","prompt":[{"type":"text","text":"Inspect the project"}],"_meta":{"aelix/requestId":"client-turn-uuid"}}}
```

Reuse only for identical content. Completed duplicates return the stop result
without repeating output; reload history if output was lost. `-32006` means
unresolved effects: show history and require a deliberate new user action.
Never silently retry with a fresh key. `-32008` is changed content under one
key, `-32007` RPC timeout, `-32004` missing capability/client action,
`-32000` busy/limit, `-32003` denied management approval, `-32602`
invalid parameters and `-32603` backend failure before native work started.
Use `error.data`, not parsing human error messages, when details are provided.

Two application codes sit outside the ranges JSON-RPC and ACP reserve, and are
also listed in `agentCapabilities._meta["aelix/errors"]`:

| Code | `data` | Meaning |
|---|---|---|
| `-33001` | `{kind:"session_locked", sessionId, lockedBy:"native"\|"acp", holder:null, retryable:false}` | Another writer holds the session: the CLI or another native process (`native`), or another adapter process (`acp`). Show it; reopen after that writer closes. `holder` stays `null` until aelix-ai reports the lock holder (Studio request R7). |
| `-33002` | `{kind:"turn_failed_after_dispatch", retryable:false, cause?}` | The turn failed after native accepted work, where the failure would otherwise be `-32603`. Model calls or tool effects may have happened; do not resend automatically. |

AionCore maps both to a non-retryable protocol error, so its automatic turn
replay (only for `retryable == true`) does not run. After native work started,
a failure with a more specific code keeps that code instead of `-33002`:
`-32007` (native may have started work), `-32000`, `-32602`, `-32004`,
`-32003`, `-32006`, `-32008` or `-33001`. AionCore treats all of them as
non-retryable too. An adapter crash or closed transport has no adapter error at
all; AionCore v0.2.2 replays that turn once until handochan/aelix-studio-host#6
lands. The native lock is
recognized from native's own refusal text until R7 provides a structured
signal; the native transcript path is never copied into the error.

## HEAD fork

The ACP `session/fork` capability takes the registered parent ID, the same
workspace, the client's MCP servers and `_meta["aelix/expectedHead"]`. The host
sends a turn reference in `session/prompt` as `_meta["aelix/forkHead"]`. ACP
binds it to the saved native snapshot when the turn settles. The fork compares
the expected reference and the captured snapshot before writing a child. A
missing or stale reference returns `-33003` with `kind: "fork_head_changed"`.
See the [Host #43 wire examples](https://github.com/handochan/aelix-studio-host/issues/43).

A matching request opens a child with a new ACP ID and
returns its selectors. The native repository copies the active path through the
current leaf and records the parent. The parent can stay open in a compatible
ACP worker while idle. An active parent returns `-32000`. A writer running an
older adapter or an independently owned native session returns `-33001`.

The fork response omits historical replay. The host copies its message
projection first and uses the same reference on child startup. Later `session/load`
replays the inherited native text followed by the child's own ACP events. The
native worker projects text from the captured cut before publication. Image
payloads stay in the native transcript and remain available to the model.

The adapter reserves one native destination before calling the worker and
clears the reservation in the SQLite transaction that binds the child. A
failed open closes the worker and removes its reserved transcript and staging
file. Cleanup verifies the generated transcript's digest before deleting it,
so a draft changed by a writer is preserved. An interrupted adapter leaves a
reservation for recovery on the next initialize or fork. If cleanup cannot
finish, another fork of that parent returns `-32006` with `kind:
"fork_cleanup_pending"`. The reservation covers ACP binding. The fixed host
does not send an operation ID to recover an already successful ACP fork whose
response the host lost before recording the new child ID.

## Closing the native writer

When `agentCapabilities.sessionCapabilities.close` is advertised, send
`session/close` with the live ACP `sessionId`. ACP returns `{}` after the
native worker exits and the session lease is released. The saved ACP
identity, history, and native transcript are retained for `session/load`.
An active session operation returns `-32000` without cancelling the turn.
A `children/` control in flight is not such an operation: the close goes on
and ends it as any worker change does (`tests/test_children.py`, a steer in
flight at `session/close`), and on POSIX a native with aelix-ai #612
gets up to 45 s to end before it is killed, so `{}` can take that long (see
[Delegated children](#delegated-children) and the close latency measured in
[product validation](product-validation.md)).

## Management

For app defaults and conversation-specific extension selection, Studio uses the
[managed extension override contract](studio-extension-overrides.md). It
defines both the launcher snapshot and the versioned ACP management actions.

`_aelix/capabilities` takes `{sessionId}` and returns `version:1`, native
`runtime` capabilities and `configOptions`. Discover actual methods.

```json
{"jsonrpc":"2.0","id":11,"method":"_aelix/manage","params":{"sessionId":"...","action":"settings/get","arguments":{}}}
```

| Action | Arguments | Behavior |
|---|---|---|
| `sessions/close` | `{}` | Close process, retain identity/history |
| `sessions/branch` | `{kind:"clone"}` or `{kind:"fork",entryId:"..."}` | New ACP ID, parent preserved |
| `settings/get` | `{}` | Settings + typed schema, choices, scope, reload flag |
| `settings/set` | `{key,value}` | Only schema keys; reload as indicated |
| `resources/list` | `{}` | Tools, skills, prompts, hooks, extension errors, trust |
| `resources/reload` | `{}` | Native reload; refresh views/selectors/commands |
| `tools/set_active` | `{names:[...]}` | Select loaded names |
| `auth/status` | `{provider?}` | Configured/source, no credential values |
| `auth/set_api_key` | `{provider,key}` | Native store; never log params |
| `auth/login` / `auth/logout` | `{provider}` | OAuth dialogs / removal |
| `mcp/configure` | `{servers:[...]}` | Replace client servers, ACP entry format |
| `mcp/status` | `{}` | Names/connections, no credentials; tools in resources/list |
| `extensions/list` | `{}` | Installed distribution/version/entry reference |
| `extensions/action` | `{action,target?}` | list/verify/discover/install/remove/update/enable/disable/reload |
| `agents/list` / `agents/show` | `{}` / `{name}` | Native profiles and diagnostics |
| `agents/use` | `{name}` | Persistent selection; `--none` clears |
| `agents/run` | `{name,task,allowProject?}` | Native delegation, enabled explicitly |
| `agents/enable` | `{enabled:bool}` | Consent + restart |
| `workspace/trust` | `{trusted:bool}` | Consent + restart |
| `children/list` | `{}` | `{epoch, children:[row]}`: native's live and recently ended delegated children |
| `children/events` | `{id, afterSeq?}` | Native's kept frames of one child after the cursor |
| `children/steer` | `{id, text, mode?:"steer"\|"follow_up"}` | A message to a running child; `{outcome, phase, verb, ended, reason}` |
| `children/wrap_up` | `{id}` | Ask a running child to finish; same answer shape |
| `children/stop` | `{id}` | Stop a child; `{state}` |
| `children/notices` | `{}` | One native notice turn; `{started, placed, reason, remaining, stopReason}` |

Mutations require idle sessions. Cancel first or disable controls during a turn.
The five `children/` actions other than `children/notices` are the exception:
they are answered during a turn and during `agents/run` (see
[Delegated children](#delegated-children)).
Refresh capabilities/resources after profile/auth/resource changes.
Enable extensions by absolute path or installed `module:factory`. Inspect
`ready`, `loadErrors`, `requiresReload`; a successful pip exit does not imply
a different bundled interpreter can import the package.
For `update`, use the inventory's distribution name or omit the target to
update recorded sources. `remove` accepts the extension entry-point name.
Offline updates need a recorded local source or configured local index.

Create/load can return `_meta["aelix/recoveryWarnings"]`. Display these warnings
alongside the effective selectors; warnings are also emitted as text. Missing
model choices retain the native fallback. A saved agent profile that can no
longer be loaded refuses the load with `-32004` and
`{kind:"profile_unavailable", profile}`; the user restores or recreates that
profile, or starts a new session. A saved tool selection that the runtime
cannot enforce refuses the load (`-32004`, `kind:"restrictions_unenforceable"`).
`_meta["aelix/permission"]` reports the native posture read back with
`permissions/get` next to the client approval policy.

Skills/prompts are native files. Studio can author them through its file
editor, set `skill_paths` / `prompt_template_paths` and reload. This API
does not provide a terminal-widget-to-web editor converter.

Client MCP servers use the native namespace `acp_NAME`, with tools named
`acp_NAME__TOOL`. Use returned tool names unchanged for tool selection.

## Agent-to-client actions

Opt in at initialization:

```json
{"protocolVersion":1,"clientCapabilities":{"_meta":{"aelix/clientActions":true}}}
```

Handle requests to `_aelix/client_action` with
`{sessionId,action,arguments}` and reply with an object. Unsupported clients
receive `client_action_required`; the LLM is never asked to emulate UI.
Avoid deadlocks: acknowledge before calling a mutation on the same session
from an active slash action.

| Action | Arguments | Client behavior |
|---|---|---|
| `dialog` | Native event, `id`, `method`, `title`, options, etc. | Render and return result below |
| `new_session` | `{cwd}` | Queue creation/navigation after acknowledging |
| `open_session` | `{sessionId,parentSessionId?}` | Queue load/navigation after acknowledging |
| `login` / `logout` | Raw argument string | Open provider management |
| `trust` | Raw argument string | Open trust controls |
| `statusline` / `hotkeys` / `expand` | Raw argument string | Studio presentation |
| `import` | Raw argument string | Open transcript import workflow |

These are extension points. Native transcript import and Studio preferences
screens are not implemented by this adapter. `/new` and `/resume` never
overwrite the current native file. `/tree` returns forkable entries, not a
complete graphical lineage tree.

Dialog methods: `select`, `confirm`, `input`, `editor`, `oauth`, `oauthSelect`.
Dismissal replies `{cancelled:true}`. Select/input/editor/oauthSelect use
`{value:...}`; select values must match options. Confirm uses
`{confirmed:true|false}`. OAuth shows/opens the supplied URL/instructions;
reply `{}` to continue or cancel. Never persist OAuth codes/inputs/URLs as
transcript text. Native deadline is 300 seconds, client request slightly less.

`_aelix/notification` carries transient auth progress and needs no response.

## Delegated children

A child started by the model's `agent` tool is reported on that tool call, in
standard ACP. Every `tool_call_update` for the parent after the first child
event carries:

```json
{"sessionUpdate":"tool_call_update","toolCallId":"call_1","status":"in_progress",
 "_meta":{"aelix/subagents":{"version":1,"children":[{"id":"sub-…","profile":"reviewer",
 "state":"running","currentTool":"read","elapsedMs":2300,"tokens":0,"cost":0.0,
 "model":"one","provider":"fixture","event":"subagent_tool"}]}}}
```

`children` is the complete latest list for that call (at most 32), so a client
that keeps only the newest frame, or merges frames with JSON merge-patch as
AionCore stores them, still has every child. `state` is native's
`starting|running|done|error|stopped`; text fields are child-authored, bounded
to 256 characters and stripped of control characters. Native's own progress
card text keeps arriving as the call's `content`. A late child event after the
call ended carries `_meta` without `status`. `agent action=resume` moves the
child to the new call. Native runs `agent` calls one at a time, which is what
lets the adapter attribute `aelix_subagent` children without a parent ID.

A child event with no open `agent` call (for example `/agents run`) is still
sent as the `_aelix/subagent` notification `{sessionId,event,data}`. AionCore
ignores agent-to-client extension notifications. Progress is not a
crash-recovery queue or an independent child chat.

### With a native that has aelix-ai #612

These hold only when `_aelix/capabilities` lists the `children/` methods in
`runtime.methods` (aelix-ai `ff755b58` or later, with delegation enabled for
the session). The native then also emits `aelix_child`, one frame per change of
a child, each naming the call that started it (aelix-ai ADR-0307).

**On the parent card, for every client.** A child's entry gains `lastText`,
`stuck` (native's open pattern name, such as `"repeat"` or `"error loop"`, or
`null`), `wrappingUp`, `currentTool` (the tool of its latest call still open, or
`null`) and, on its last frame, `end`: `completed`, `wrapped_up`, `stopped`,
`cancelled` or `failed`. The update also carries `_meta["aelix/child"]` =
`{id, epoch, seq, event, end?}`; deduplicate by `(epoch, id, seq)`, since ACP
does not promise the order of a notification and a response. A child spawned in
the background (`agent action=spawn`) never sends `aelix_subagent`: its entry is
created from `aelix_child` with `event:null` and no model or usage, and keeps
reaching the spawning call's card after that call completed and after later
prompts, with the `profile`, `state`, `wrappingUp` and other fields it last
showed (`tests/test_native_children.py`, a spawned child's entry after the next
prompt; a real model, `docs/product-validation.md`). A parent call id that native
hashes (over 128 bytes as the wire writes it, see below) still reaches its card.
ACP's `stopReason` stays `end_turn` or `cancelled`; a child's own end is only
in `end`.

**When the worker is replaced or ended** (a restart, `session/close`,
`sessions/close`, `/quit`, `session/delete`, a worker that died), the
`tool_end` and terminal `state` frames native writes while it stops a child
still arrive. A child still live when the worker is gone gets its last update
from the adapter: its open calls
`failed`, its entry `state` and `end` `stopped`, with
`_meta["aelix/child"].closedBy: "adapter"`, and its top-level card (below)
`failed`. These updates are recorded; they are sent while the client is
connected and was not told the session was replaced, so a `session/load` of the
same live session gets them in its replay instead.

**Nested tool calls, opt in.** Declare
`clientCapabilities._meta["aelix/nestedToolCalls"]: true`. Each of a child's own
calls then arrives as a `tool_call` / `tool_call_update` with
`toolCallId: "<child id>:<native call id>"` and
`_meta.parentToolCallId` naming the parent card: `in_progress` at its start,
`pending` while it waits for approval, `completed`, or `failed` (also for a call
native closed without a result). A child with no parent call (`/agents run`)
gets a top-level card `aelix-child:<child id>` (`kind:"other"`, title
`agent <profile>`) that its calls nest under, closed `completed` (end
`completed` or `wrapped_up`) or `failed` (any other end). These updates are
recorded, and `session/load` replays them only to a client that declared
`aelix/nestedToolCalls`; a client that did not gets the parent card's updates
alone, as it did live.

**Controls.** `children/list`, `children/events`, `children/steer`,
`children/wrap_up` and `children/stop` (Management table) are sent to native
outside the session's turn lock and its activity lease, so they are answered
while `session/prompt` or `agents/run` runs. While the native worker is being
replaced or ended (trust, delegation or extension changes, `/trust`,
`session/close`, `sessions/close`, `/quit`, `session/delete`, `session/load` of
the same live session, compaction recovery, adapter shutdown) a new one is
refused before anything is written: `-32000`, `"the session is changing"`,
`data:{kind:"session_changing"}`. One already written is ended only when the
worker's close begins (for a restart, after the restart's own checks): if
native has not answered it by then, it is answered `-33002`, `"the session
changed while this request ran; its delivery is unknown"`,
`data.cause:{kind:"session_changed",command}`, since a steer or wrap-up may have
reached the child. One native answered before the close began returns that
answer, on every supported Python, also when the answer came after the change
began (`tests/test_children.py`: a steer written before a restart and answered
before that restart's close returns native's answer). One whose worker died
first is answered `-32603` (`"Aelix process closed its RPC stream"`). A native
reload (`resources/reload`, `/reload`) is fenced by native itself, whose answer
passes through unchanged. A `children/list` or `children/events` reply names
native's current `epoch`; the adapter retires an older one when it sees it.

**Notices, opt in.** Declare `clientCapabilities._meta["aelix/notices"]: true`
to get `{"method":"_aelix/notice","params":{sessionId,event:"queued",data:{}}}`
when a background job's result or attention notice waits (native
`aelix_notice`; not recorded, not replayed, and while the worker is being ended
sent only as the card updates above are). Wake the session with
`_aelix/manage children/notices` while it is idle (it takes the turn lock, so it
is `-32000` busy during a turn), and call again while `remaining > 0`; the
next prompt also places waiting notices first. The call runs one native notice
turn the way a prompt runs: the turn timeout applies, `session/cancel` cancels
it (the answer is then `{stopReason:"cancelled"}` alone), and a cancel of an
earlier turn does not deny its tools. A failure after the request was sent is
`-33002`.

### Child approvals

When the client declared `aelix/clientActions` and native lists
`children/list`, ACP enables native child approvals
(`ui/enable {enabled:true, childApprovals:true}`). A child of an Ask
(`default`) session then asks before each change, and each question arrives as
a standard `session/request_permission` (owner, 2026-11-18: "default로 돌며
물어봄 (권장)"; a background child's question while the session is idle is sent
at once, "즉시 대화창 (권장)"):

```json
{"sessionId":"…","toolCall":{"toolCallId":"sub-3f2a:call_9","title":"write",
 "kind":"edit","status":"pending","rawInput":{"path":"out.txt","content":"…"},"locations":[{"path":"/abs/child/cwd/out.txt"}],
 "_meta":{"aelix/child":{"id":"sub-3f2a","epoch":"…","profile":"worker","jobId":null,"background":false},
          "parentToolCallId":"call_1"}},
 "options":[{"optionId":"allow-once","name":"Allow once","kind":"allow_once"},
            {"optionId":"allow-always","name":"Allow for this agent","kind":"allow_always"},
            {"optionId":"reject-once","name":"Reject","kind":"reject_once"}]}
```

For a nesting client (as above), `toolCallId` is the same id as the nested call
and `title` is that card's own title, the tool's name, so the request does not
rename the card. `parentToolCallId` is sent only to a nesting client
(`aelix-child:<id>` for a child with no parent call). A client without nesting
gets native's descriptive title instead (`"worker agent sub-3f2a asks:
Create/overwrite out.txt?"`), and its `toolCallId` names no card that client
received.
`allow-always` is native's "Yes, for this agent": the child's own session
grant, so its next such call is not asked; it never answers the parent or
another child (aelix-ai ADR-0308; not run live through ACP). Paths in `locations` resolve against the child's own directory.
Anything other than a selected offered option denies: a cancelled outcome
(also one that names an option), `session/cancel` (it cancels every open
question, during a manual `/compact` too, and one native sent as the cancel
arrived, which the client is then never asked), a client error response, and a
question asked while a cancelled turn is still ending. A question from a native
epoch the adapter has seen replaced is denied without asking; one from an epoch
it never saw is asked. The parent's own approvals keep their two options; a
parent's and a child's question can be open at the same time, and neither waits
for the other. Native asks one child question at a time (its permission
gate's lock, aelix-ai `builtin/permission.py` `decide_for_child`): a second
child's question arrives after the first one ends.

**A question native drops.** When native drops an open question (the child
ends or is stopped or wrapped up, the question's approval step is `withdrawn`
or `denied`, or a newer native epoch appears, for example after
`resources/reload` or a worker restart), the adapter answers native for it and
sends ACP's `$/cancel_request` notification, `{"method":"$/cancel_request",
"params":{"requestId":"<the request's id>"}}`, so the client can close its
dialog. A client may ignore that notification: its dialog then stays open, and
an answer the human gives in it has no effect (`tests/test_native_children.py`:
`children/stop`, `children/wrap_up` and `resources/reload` with a question open;
a late Allow wrote nothing). Native gives a question 300 s: then it answers the
child itself ("no answer from the host within 300 s"), and its `denied` step
takes the request back the same way; the adapter waits up to 330 s for the
client only as a backstop past that (read from native's source, aelix-ai
`rpc/control_ui.py` and `aelix_agents/child_approvals.py`, and held by a unit
test with the bounds shortened; a 300 s wait was not run).

Without `aelix/clientActions` the adapter does not enable child approvals and a
child of an Ask session runs under native `plan` (read-only), as before. This
is narrower than the owner's Q-A (2026-11-18, "default로 돌며 물어봄 (권장)"),
which has every Ask session's child ask: keeping the questions to clients that
declared client actions is the lead's call.

## Packaging and acceptance

Keep app resources immutable and user state/auth outside the bundle. Provide
a separately managed writable runtime for packages. Closed-network installers
need exact wheels, extension/MCP dependencies and CA configuration;
`--offline` is not a complete air-gap mechanism.

Studio still needs UI acceptance for changing selectors, permission rejection,
reconnect, OAuth cancellation, extension errors, MCP replacement, subagent
display, import and every client action. Test Windows installation and
process-tree cleanup with the packaged runtime. Adapter tests do not prove
these screens or packaging work.
