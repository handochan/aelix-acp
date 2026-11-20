# Studio conversation extension management

For [D04 A6](https://github.com/handochan/aelix-studio/issues/32), Studio sets
`AELIX_ACP_STUDIO_MANAGED=1` in the per-command environment of its managed
Aelix ACP adapter. The launcher must preserve it through to the adapter.
The value is the literal string `1`. Set it for every managed ACP launch,
including launches without an extension generation. The launcher consumes
`AELIX_STUDIO_EXTENSION_STATE` separately when selecting a generation.

With the marker set, the adapter refuses package writes from
`/extension install`, `/extension update` (including update-all), and
`/extension remove` before requesting permission or starting a package manager.
The same policy applies to the shared `_aelix/manage` route
(`extensions/action`) and to an explicitly configured `--extension-manager`.
`list`, `verify`, `discover`, and the session's enable, disable and reload
actions retain their existing behavior. Unmarked ACP processes retain their
existing package-management behavior.

Studio can localize the refusal using the ACP error's `data.kind` and
`data.action`. For example, `session/prompt` with `/extension install demo`
returns:

```json
{
  "code": -32004,
  "message": "Use the Extensions screen in Aelix Studio.",
  "data": {
    "kind": "studio_extension_management_required",
    "action": "install"
  }
}
```

The host forwards the code and data under `USER_AGENT_OPERATION_REFUSED`.
Studio adds a message for this kind to its refusal rendering and uses the
action to show “확장 화면에서 설치하세요.”, “확장 화면에서 업데이트하세요.”,
or “확장 화면에서 제거하세요.” in Korean. The ACP error message supplies
an English fallback.
