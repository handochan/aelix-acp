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
`-32000` busy/limit/lease, `-32003` denied management approval, `-32602`
invalid parameters and `-32603` backend failure. Use `error.data`, not parsing
human error messages, when details are provided.

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
identity and uses explicit approval unless Plan was already selected.

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
| `clear` | Raw argument string | Clear visible view per Studio policy |
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

Notifications `_aelix/notification` and `_aelix/subagent` need no response.
The former carries transient auth progress. The latter carries
`{sessionId,event,data}` with event `subagent_start`, `subagent_tool` or
`subagent_end`. Data can contain native ID, profile, state, current_tool,
elapsed_ms, tokens, cost, model, provider. Key cards by native ID. Progress
events are not a crash-recovery queue or independent child chats.

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
