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
   Native can run with no current model (no `defaultProvider`/`defaultModel`
   and no `--model`). `model` is then still offered with the available models,
   with `currentValue: ""` and `models.currentModelId: ""`; `""` is never a
   selectable value. Read the selection from `currentValue`: AionCore's derived
   model catalog shows an empty current value as its first (or desired) model,
   while its reconcile compares the raw `""`. `thinking_level` appears once a
   model is current, in the `config_option_update` that follows the selection.
   Model input (and `/compact`) is refused before it reaches native with
   `-32004` and
   `{kind:"model_selection_required", configId:"model", availableModels, retryable:false}`,
   after a catalog that changed meanwhile is published.
   With `availableModels: 0` there is no `model` option: configure a provider
   first. Slash commands still run, including `/model provider/id`. Native RPC
   `set_model` does not store a default model, so each new session starts
   unselected; load, worker restart and fork restore the session's own choice.
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
without repeating output; reload history if output was lost. A refusal before
native work (`-32602`, `-32004`, `-32601`) is recorded too and replayed for the
same key and content, even after its cause is fixed: after choosing a model for
a `model_selection_required` refusal, send the prompt with a new request ID.
`-32006` means unresolved effects: show history and require a deliberate new
user action.
Never silently retry with a fresh key. `-32008` is changed content under one
key, `-32007` RPC timeout, `-32004` missing capability/client action or an
unmet session precondition (`model_selection_required`),
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

## Transcript synchronization (ACP #22, Host #4)

When the client offers transcript v1 and the native worker advertises
control-v1 `transcript/list`, ACP replays one complete projection of the
selected branch during `session/load`.
It reads and validates all pages before emitting the projection. Successful
load replies carry `_meta["aelix/transcript"]` with the completed snapshot
coordinates. A load error or a response without that marker gives the host no
snapshot to apply. Clients without the offer and older native workers use the
legacy replay path and retain its display-event history. An incomplete or
over-budget scan fails with `native_history_unavailable`. No load invokes
the model, sends a prompt, or executes a tool.

For example, these are the `update` of a replayed `session/update` and the
snapshot marker in the successful load result:

```json
{
  "sessionUpdate": "user_message_chunk",
  "content": {"type": "text", "text": "Appended in the CLI"},
  "_meta": {"aelix/transcript": {
    "source": "replay", "nativeSessionId": "native-123", "entryId": "4ad1b348",
    "parentEntryId": "a8c13400", "sequence": 3, "role": "user", "part": 0
  }}
}
```

```json
{"_meta": {"aelix/transcript": {
  "version": 1, "nativeSessionId": "native-123", "leafId": "c8434a01",
  "totalMessages": 4, "recovered": false
}}}
```

The durable key is `(nativeSessionId, entryId)`. Sequence is the one-based
native message ordinal on the selected path. Siblings can share an ordinal,
and forks use a new native session ID. Parts start at zero for each entry and
are emitted in order. One assistant entry can contain text, thoughts and tool
calls. A result is a separate `toolResult` entry, projected to the related
card's terminal `tool_call_update`. Every native entry has at least one
update, including empty or unencodable rows. Image blocks use display
placeholders. Bounded display projections can omit oversized tool inputs or
results. The same snapshot replays the same keys and order on each load. One
load includes each logical entry once, even where ACP had already recorded
its content or a terminal result. A valid empty snapshot has
`totalMessages: 0`; a damaged but readable native path sets `recovered: true`.

The adapter advertises support in `agentCapabilities._meta["aelix/transcript"]`
as `{"version":1}`. This advertisement describes adapter support. The current
native pin `83714045` uses legacy replay. Host #4 needs a native pin that
includes aelix-ai #655 to enable transcript v1.

Offer `{"version":1}` in `clientCapabilities._meta["aelix/transcript"]`.
Each successful `session/new` or `session/load` with a supporting native worker
reports `_meta["aelix/transcriptCapability"] = {"version":1}`. Its absence
indicates legacy behavior for that session. The session capability is distinct
from the completed snapshot marker, which appears only after canonical replay.
When both the offer and per-session native capability are present, assistant
and tool stream updates carry live metadata when native supplies it. ACP emits
live user chunks with their end receipt so the host can associate the submitted
input with its stored message. A live delta and its later settlement look like this:

```json
{
  "sessionUpdate": "agent_message_chunk",
  "content": {"type": "text", "text": "Done"},
  "_meta": {"aelix/transcript": {
    "source": "live", "nativeSessionId": "native-123",
    "bindingId": "native-binding", "streamId": "native-stream", "role": "assistant"
  }}
}
```

```json
{"jsonrpc":"2.0","method":"_aelix/transcript/settled","params":{
  "sessionId":"acp-session",
  "source":"live","nativeSessionId":"native-123","bindingId":"native-binding",
  "streamId":"native-stream","role":"assistant","status":"stored",
  "entryId":"c8434a01","parentEntryId":"4ad1b348","sequence":4,"replaced":false
}}
```

Group provisional chunks by `(bindingId, streamId)`. Only a `stored` settlement
binds them to durable coordinates. `notStored`, `unknown`, `storageOnly`, and a
missing settlement can leave provisional display content. A later load also
resolves stored replacements, abandoned branches, lost receipts and native
messages without a live event. Tool execution frames use the result stream.
The live card continues to use `toolCallId`, and replay retains that ID
where the call is unambiguous within the selected path. Repeated native
call IDs get distinct replay card IDs. ACP records the result receipt's
durable key against the originating call occurrence. A reducer can rewrite
the stored `tool_call_id`, and replay still completes the original card using
the stored result. Progress on a verified tool stream follows that call
occurrence, including when another turn reuses its raw call ID. Late progress
that cannot be attributed to an occurrence stays in the archived display
history, as does other legacy tool metadata without a verifiable association.
ACP also excludes already-active children from a later occurrence's replay
snapshot when their late progress reached the same live raw-ID card.
Tool progress retains `_meta["aelix/subagents"]` and the negotiated nested
tool metadata. With `aelix/nestedToolCalls`, ACP also replays recorded
standalone synthetic parent cards and their nested tool updates, including
when the native snapshot is empty.

### First load of an existing Studio conversation

Host #4 must stage the replay through the successful load result. It keeps
the old display history as an archive, then rebuilds the active native-backed
portion from the complete projection, keyed by native IDs. Merge or replace
by those keys on subsequent loads. Preserve application-owned messages,
ACP-only updates and tool metadata with a verified card association. Adapter
display events carry `source: "adapter"` when ACP can identify them. They
have their own stable `eventId` and retain their recorded order. Their
`sequence` is local to the adapter `bindingId`, in a separate namespace
from native message sequence. Ambiguous
legacy content remains in the archive. The host can retain its prior clean
view when a load reports recovery. Keep composer echoes provisional and
avoid inserting them again as native user messages. Older assistant chunking,
formatted user echoes, images, tool output formatting, and old branch or
cancelled display content can differ in the active conversation after
reconstruction. The archive preserves the original representation.

The native pin and Host #4 integration are coordinated under Studio D16.

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
with transcript v1 replays the child's selected native branch, including the
inherited prefix. Legacy replay uses the inherited text followed by the
child's ACP events. The native worker projects text from the captured cut
before publication. Image
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
| `children/notices` | `{}` | One native notice turn; `{started, placed, placedInTurn, reason, remaining, stopReason}`. `placedInTurn` counts every notice the turn placed, its boundaries' included, and comes only from a native with aelix-ai #857; `placed` counts those placed before its first model call |
| `children/resume` | `{id, task}` | Resume a stopped child the model delegated (native aelix-ai #836): `id` is its session id, `task` the next instruction (at most 64,512 UTF-8 bytes); `{result}` as `agents/run`'s, with its deadline and cancel. A consent dialog the native asks reaches a client-action client as a `dialog` client action, also on a session just opened with `session/load` |

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
