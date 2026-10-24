# Minimum Studio changes for the beta.2 preview

Read-only Studio assessment, updated 2026-10-24. Studio baseline `3a8763cfd`, unchanged
AionCore v0.2.2 on macOS arm64. This task modifies ACP and its tests/docs;
it does not modify Studio source, its saved profiles, or its user database.
The earlier uncommitted `codex/studio-runtime-controls` main-runtime work is
still present and must remain separate from preview packaging.

The later [actual live validation](live-validation.md) also exercises direct
Anthropic messages and the existing browser UI's model selection, permission
cards, Stop and history reload. It is separate from the no-model-turn host
contract probe described below and does not modify Studio source.

## Required packaging changes

| Location in Studio | Preview change |
| --- | --- |
| `scripts/aelix-studio/runtime.json` | Retain the released native `aelix==0.1.0b2` wheels. After this ACP change is committed/reviewed, pin that exact ACP commit/version and archive checksum. Do not reuse the earlier `8deb482` control-only candidate. |
| `scripts/aelix-studio/prepareRuntime.py` / `compatibility/apply.py` | Stop applying the PoC's exact-match patch to the product adapter. The new ACP already implements Windows stdio, authenticated loopback, process cleanup, channel-error diagnostics and repeatable `--aelix-arg` prefixes. Applying either the original PoC patch or the earlier main-integration diagnostic patch again will fail its exact-match checks. Remove that invocation for the newly pinned adapter; no native behavior replacement is required. |

Those are the mandatory compatibility changes relative to Studio HEAD.
The existing launch bridge, native Python entrypoint, writable state locations,
standard config bridge, approval UI and message stream can be retained. The
default ACP profile is already beta2, so a new runtime-profile launcher flag is
not necessary for preview.

The earlier uncommitted Studio main-integration branch changes more than these
minimums. Its `native` source pin, native wheel build helper, OpenAI SDK 2.x and
filelock additions belong to the control-v1 candidate, not the public beta.2
preview. Build preview from its released-wheel baseline. The new generic
runtime-setting UI can be reused later: these four setters exist in beta.2,
but an additional settings UI is not required for a working ACP connection.

## Measured host contract

`scripts/probe_studio.py` launches the existing AionCore binary with a private
temporary database and isolated Aelix/ACP state, and registers two disposable
custom agents using an installed ACP wheel and the published beta.2 CLI.
It does not use the previous main-built Studio runtime or touch the running
user application. The loopback host's `--local` option is for this isolated
test only; it is not a public WebUI deployment recommendation.

Observed:

- Standard session creation succeeds with unchanged AionCore.
- Its config catalog contains permissions, models, reasoning and the four
  delivery/compaction/retry controls. The beta.2 public-extension bridge supplies
  actual model choices. The unchanged host selects fixture/two and confirms the
  native effective model as `observed`; no host model turn is used in this probe.
- Changing `autoRetryEnabled` to string `false` returns `confirmation=observed`
  and a matching effective value.
- Existing team session creation and teammate attachment succeed for the two
  Coding probe agents. Actual team model messages and team tool/delegation
  effects are not established by this setup check.
- Setting the existing `supports_team` metadata to false removes team
  selectability. Hiding teams for an initial Preview would be an optional
  product-scope decision, not a prerequisite demonstrated by this probe.

The earlier pre-integration probe showed that AionCore synthesized a model
picker from singleton legacy choices. The fallback still returns an empty
choice list if no registry route exists. The current bridge supplies a real
registry and standard model config; it requires no Studio source workaround.

Dedicated tool, extension, skill, template and profile screens remain Studio
work. Use `_aelix/capabilities.adapterMethods` and the
[Preview management contract](beta2-preview.md), with approval/restart feedback.
These screens are additions beyond the minimum connection/selector changes.

Reproduce using an ACP wheel installed into a disposable beta.2 environment:

```sh
python scripts/probe_studio.py \
  --backend /path/to/studio/resources/bundled-aioncore/darwin-arm64/aioncore \
  --acp /path/to/beta2-environment/bin/aelix-acp \
  --aelix /path/to/beta2-environment/bin/aelix
```

## Existing behavior to retain and release checks

- Display only the returned modes and commands. Preview has explicit approval
  only; do not synthesize Plan/Auto/full-access options or enable native agents.
- Do not send client MCP servers in preview. Unsupported `mcpServers` is a
  structured error, not a request to ignore or emulate MCP. Global MCP choices
  should be omitted/disabled for these preview agents if that UI is offered.
- Keep reconnect/manual retry distinct from automatic replay. ACP deduplication
  needs a stable `aelix/requestId` from the host. This assessment does not claim
  AionCore's send path forwards that key; unsafe automatic retry must stay off
  until its mapping is checked. A per-turn idempotency host extension is not
  required for the measured baseline session/config contract.
- Retain public WebUI authentication and protection of credentials. Native auth
  is configured before launch in preview; runtime OAuth/management screens
  remain control-v1 work. An unchanged HTTP host probe is not browser auth QA.

The installed beta.2/ACP host probe did not cover the Analytics extension,
actual Studio renderer interactions, model-backed teams, real providers/OAuth,
Windows/Linux packages or clean-machine installation. Repeat those checks on
the final repinned preview bundle before distributing it. After native beta.3
is published, pair its real wheels, select `control-v1` and repeat the upgrade
and UI acceptance gates before the official desktop/web beta release.
