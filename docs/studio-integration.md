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
| `-33002` | `{kind:"turn_failed_after_dispatch", retryable:false, cause?}` | The turn failed after native accepted work. Model calls or tool effects may have happened; do not resend automatically. |

AionCore maps both to a non-retryable protocol error, so its automatic turn
replay (only for `retryable == true`) does not run. The native lock is
recognized from native's own refusal text until R7 provides a structured
signal; the native transcript path is never copied into the error.

## Management

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

Mutations require idle sessions. Cancel first or disable controls during a turn.
Refresh capabilities/resources after profile/auth/resource changes.
Enable extensions by absolute path or installed `module:factory`. Inspect
`ready`, `loadErrors`, `requiresReload`; a successful pip exit does not imply
a different bundled interpreter can import the package.
For `update`, use the inventory's distribution name or omit the target to
update recorded sources. `remove` accepts the extension entry-point name.
Offline updates need a recorded local source or configured local index.

Create/load can return `_meta["aelix/recoveryWarnings"]`. Display these warnings
alongside the effective selectors; warnings are also emitted as text. Missing
model choices retain the native fallback. A failed saved profile resets its
identity and returns to Ask. A saved profile or tool selection that the runtime
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
lets the adapter attribute children without a parent ID in native's event.

A child event with no open `agent` call (for example `/agents run`) is still
sent as the `_aelix/subagent` notification `{sessionId,event,data}`. AionCore
ignores agent-to-client extension notifications. Progress is not a
crash-recovery queue or an independent child chat; per-child approval, stop
and steering are not part of this contract.

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
