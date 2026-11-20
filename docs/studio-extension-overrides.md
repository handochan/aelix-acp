# Studio managed extension overrides (v1)

Studio owns the installed generation and the app defaults. ACP owns
conversation overrides, keyed by ACP session ID. The host forwards ACP
requests and responses for the target conversation.

## Launcher input

After choosing and leasing a verified generation, the Studio launcher passes
`["--managed-extensions", absoluteSnapshotPath]` as two adapter arguments.
The snapshot is a UTF-8 JSON file, up to 1 MiB, written by the launcher in
Studio-owned storage. It is read once during argument parsing. Studio chooses
the generation and reads the app defaults used to build this snapshot. It
includes all verified entries, including those disabled by default.

```json
{
  "version": 1,
  "generation": "g00000012-feed",
  "sitePackages": "/studio/runtime/generations/g00000012-feed/venv/lib/python3.12/site-packages",
  "extensions": [
    {
      "id": "aelix-notes:notes",
      "defaultEnabled": true,
      "reference": "aelix_notes:setup"
    },
    {
      "id": "aelix-calendar:calendar",
      "defaultEnabled": false,
      "reference": "/studio/runtime/generations/g00000012-feed/pack-inputs/calendar",
      "entryPoints": ["aelix_calendar:setup"]
    }
  ]
}
```

- `version` is the integer `1`. Generation and entry IDs use an ASCII letter
  or digit followed by up to 255 ASCII letters, digits, dots, underscores,
  colons or hyphens. Entry IDs are case-sensitive and unique. Studio should
  use the canonical distribution name and the stable entry-point name.
  If multiple entry points load the same pack, publish one entry with a
  stable ID for the group and list all of its entry-point aliases.
- `defaultEnabled` is a boolean. Use `true` for a verified installation with
  no user preference. `extensions` may be empty.
- `sitePackages` is the absolute import directory of the leased generation.
  It is required whenever the snapshot contains a module entry.
- A module entry has exactly `id`, `defaultEnabled`, and `reference`. Its
  reference is `module.path:factory`. Use `:setup` explicitly for bare module
  entry points. Module components and the factory are ASCII Python
  identifiers. A single-letter module before the colon is drive-relative
  on Windows. ACP passes a private absolute loader file to native `-e`.
  The loader resolves the named module from `sitePackages`, including
  when the workspace is trusted and when native reloads resources.
  It rejects a cached module from another location.
- A pack entry also has `entryPoints`, a nonempty array of explicit
  `module.path:factory` references. Its reference names an absolute wrapper
  directory with exactly one child directory containing
  `aelix-plugin.toml`. A symlink or junction to the installed pack is valid.
  ACP reads the manifest plugin ID and includes its explicit Python factory,
  if present, in alias matching. The wrapper is passed to native `-e`, which
  binds the child's manifest. Use the manifest's existing D04 load checks.
- ACP rejects duplicate JSON keys, unexpected fields, repeated IDs or pack
  plugin IDs, and overlapping reference or entry-point aliases. An invalid
  supplied snapshot stops adapter startup. A launcher with a legacy
  generation may omit the argument and use its legacy launch path until
  the individual pack inputs have been verified.
- The launcher attaches the generation's site-packages to the **native**
  worker's import path and holds the generation lease for the lifetime of
  the adapter. Use the same directory for `sitePackages` and the worker's
  generation import path. Pass each managed selection through the snapshot only.
  Operator `--extension` arguments remain available for separate trusted
  extensions. ACP rejects overlapping operator references before launch.
  A known legacy session reference migrates to an `on` override unless
  the session already has an override for that ID. An indirect overlapping
  reference, such as a directory with several packs, is rejected. Direct
  native extension arguments inside `--aelix-arg` are refused in managed mode.

The snapshot is trusted launch configuration. Only the launcher supplies
references or paths. Session requests supply IDs. The launch argument works
independently of the Studio package-write policy.

## ACP requests

An adapter that received a snapshot returns
`_meta["aelix/extensionOverrides"] = {"version":1}` from `initialize`.
An empty snapshot supports the same API and can report dormant overrides.
Without a snapshot the feature is unavailable and the capability is absent.

```json
{"jsonrpc":"2.0","id":1,"method":"_aelix/manage","params":{"sessionId":"s","action":"extensions/overrides/list","arguments":{}}}
{"jsonrpc":"2.0","id":2,"method":"_aelix/manage","params":{"sessionId":"s","action":"extensions/overrides/set","arguments":{"extensionId":"aelix-notes:notes","value":"off"}}}
```

`set` accepts exactly `extensionId` and `value` in its arguments.
`on` and `off` override the app default. `inherit` removes the override and
also clears a saved override for an entry that is currently unavailable.
An unchanged override returns the current snapshot without prompting or
restarting. A change requires an idle conversation and the existing ACP
`session/request_permission` approval. The device's extension package is
already installed. The action selects it for the conversation. Managed
`/extension enable TARGET` and `/extension disable TARGET` share the setter.
They prefer the exact stable ID and accept an unambiguous reference,
entry-point alias or pack ID. Use the structured `set` request to restore inheritance.
With a snapshot, `enable` through either the slash command or
`extensions/action` refuses a target outside the snapshot before requesting
permission. This keeps new session loads within the launcher's verified
catalog. Existing legacy session entries can still be disabled.

Both actions return the same shape.

```json
{
  "generation": "g00000012-feed",
  "extensions": [
    {
      "extensionId": "aelix-notes:notes",
      "defaultEnabled": true,
      "override": "off",
      "desiredEnabled": false,
      "state": "disabled",
      "reason": null
    }
  ]
}
```

`state` is `active`, `deferred` (registered for activation on its first
command), `disabled`, `unavailable`, or `error`. `reason` is a machine-readable
code or null. A dormant row has `defaultEnabled: null`, `desiredEnabled: false`
and `reason: "not_installed"`. A selected entry that cannot be confirmed has
`state: "error"`. Native's extension inventory and selection diagnostics
report actual registration and on-demand activation. An active row describes
the extension registration. Its individual capabilities and MCP connections
remain governed by native.

Invalid arguments return `-32602`. An ambiguous managed alias returns
`-32602` with `data.kind: "managed_extension_ambiguous"`. Busy or rejected
requests use the existing ACP errors (`-32000` or `-32003`). `-32004` carries a `data.kind` of
`managed_extensions_unavailable`, `managed_extension_unavailable`,
`managed_extension_conflict`, `managed_extension_not_applied`, or
`managed_extension_change_failed`. When a change fails,
`data` includes the extension ID, reason and `recovered` status. Cancellation
during worker replacement uses the same recovery path and returns `-32003`.

## Worker startup and recovery

ACP composes the requested managed references before every worker start.
The order is operator and legacy session references, then the selected
managed entries in snapshot order, then the approval bridge. Native still
runs with `--no-extensions`. An unexpected registration of a disabled entry
refuses the worker.

ACP holds the session activity lock through a managed change and its recovery.
It reopens the same native transcript, verifies the requested registration,
and persists an accepted override in the session preferences in
`sessions.sqlite3`. If the candidate fails, ACP reopens with the previous
preferences and verifies registration of every managed extension selected
by that configuration. If startup or registration fails during recovery,
ACP reports `recovered: false`, closes the worker, and the client can retry
using `session/load`. The saved history and previous override are
retained. A request that loses its response can read back the current
override after reconnecting. Session changes do not write native
`selections/set` global or project preferences.

A worker restart uses the same adapter snapshot. On a new adapter, the
launcher chooses the current generation and prepares a new snapshot.
`session/load` combines that snapshot with the persisted overrides. An entry
missing from the current generation is unavailable until the same ID
returns, or until the user clears the override.
