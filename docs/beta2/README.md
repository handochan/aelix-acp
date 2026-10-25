# Public beta.2 ACP qualification

Issue [#6](https://github.com/handochan/aelix-acp/issues/6), 2026-10-25. This is a
repeatable native/adapter baseline, not full Studio Preview acceptance. Shared
product requirements remain in the [Studio design](https://github.com/handochan/aelix-studio/blob/main/docs/studio-release-train/README.md).

The subsequent [ACP #7 management/host wire qualification](management-contract.md)
records sessionless native API probes and the actual pinned AionCore path,
including request identity, cancellation, unsupported methods and shared-store
limits. It does not add a production helper to this adapter.

## Source and wheel identity

[manifest.json](manifest.json) pins all four public `0.1.0b2` wheels by filename,
download URL, byte size and full SHA256. They were downloaded from PyPI and
compared with both registry metadata and the actual installed files, before and
after the process probes. The local run verified 309 original wheel members,
including package data and metadata. Installers rewrite RECORD, so that member
is excluded from byte comparison. This is not a transitive-dependency or Python
bytecode security attestation; the receipt records resolved dependency versions.

| Distribution | Wheel SHA256 prefix |
| --- | --- |
| aelix | `98a9201be2c84c96` |
| aelix-ai | `5a8b03bdc87161ea` |
| aelix-agent-core | `f3fdabe25ed8e49a` |
| aelix-coding-agent | `60b19815c9355fa1` |

The public source reference is tag `v0.1.0-beta.2`, commit
`1093cc2578e70500c91fe0a8d340b7db2528c8de`. Its GitHub ref was resolved separately.
Matching a release tag and registry artifact does not independently attest that
the wheel was built from that source. No native source or installed payload is patched.

The qualified ACP payload is the initial main at
`cf2911f9842d6ae6a7a2fedb0b68791fe0b07f59`. This change adds qualification tools,
tests, CI and documentation; it does not merge PR #1 or the existing dirty
beta2 candidate. The installed ACP package-file hashes are in the receipt and
must match the qualification checkout. Studio should pin the subsequently
reviewed/merged qualification commit and verify its own AionCore pairing.

## What actually ran

The [local receipt](qualification.json) records macOS arm64/Python 3.12, separate
disposable native and ACP wheel environments, nine native import symbols,
eleven CLI flags, exact harness source hashes, and these process checks:

| Path | Observed result | Boundary |
| --- | --- | --- |
| Native CLI + explicit test extension | Started with public wheels | The test extension is qualification code, not a production management bridge |
| Classic reasoning setter | `high` returned by native get_state | Studio selector forwarding remains to implement |
| Native stream and Abort | Synthetic response streamed; held turn aborted | No real provider/model performance claim |
| Public extension registry/context | Fixture models and native context usage observed | Token usage comes from the synthetic provider fixture |
| Public extension model setter | Switched one→two; following request retained the earlier history marker | This proves an API route, not current production ACP forwarding |
| Installed main ACP → native | Session, launch-model selector, text stream and Stop passed | Current main model selection remains launch-only |
| Installed PR #1 → public native | Default session creation refused control-v1 absence | A passing expected-blocker probe is not Preview compatibility |

The classic CLI does not supply ModelRegistry to its RPC runner:
`get_available_models` and `set_model` return the specific missing-registry
error. `aelix_control` is not a recognized public beta.2 command. The qualifier
checks these exact gaps; an unrelated parse/startup failure does not qualify.
The explicit test extension proves registry/model/context access without
modifying native wheels. A production route still needs its own lifecycle,
authorization, bounded framing, cancellation and host integration work.

All model requests use an in-process loopback HTTP fixture and temporary
config/auth/session roots. The qualifier strips inherited provider credentials
and PYTHONPATH from child environments. The receipt contains no raw prompts,
provider diagnostics, user conversations or real data. No real-provider,
Studio/Aion UI, Windows installer, mobile or offline-install acceptance was run.

## Reproduce

From a clean checkout, with Python 3.11 or 3.12 and uv:

```sh
uv --no-config venv --python 3.12 --no-project .local/native
uv --no-config pip install --python .local/native/bin/python --index-url https://pypi.org/simple --only-binary :all: aelix==0.1.0b2
uv --no-config build --wheel --out-dir .local/acp-wheel
uv --no-config venv --python 3.12 --no-project .local/adapter
uv --no-config pip install --python .local/adapter/bin/python --no-index .local/acp-wheel/*.whl
.local/native/bin/python scripts/qualify_beta2.py --adapter .local/adapter/bin/aelix-acp --report .local/qualification.json
```

The adapter must be installed from its wheel, not source/editable. Native
source/editable distributions, wrong versions, registry/digest mismatches and
altered installed members fail qualification. Python `-O` is refused because
runtime assertions are acceptance checks. The command fetches public registry
metadata/wheels; it does not establish network-isolated installation support.

CI runs the same qualifier on Ubuntu/Python 3.11 and 3.12 and uploads a receipt
with the CI source SHA. Ordinary existing ACP tests remain on Ubuntu/macOS.
These jobs are separate from Studio package/installer testing. Current main
ACP is POSIX-only; the PR #1 Windows implementation is a distinct candidate.

`--candidate /path/to/immutable-pr1-env/bin/aelix-acp` additionally reproduces
PR #1's default control-v1 refusal. It does not use the legacy override. Only
use a wheel built from the reviewed immutable candidate; the candidate payload
hashes in the receipt identify the tested package.

## Reuse, gaps and next owner

- Reuse main's installed entrypoint/stream/approval-bridge/Stop behavior after
  reviewing its POC limits. Reasoning/context/model switching need real ACP
  capability/configOptions bindings; the extension probe alone is not delivery.
- Review PR #1's protocol validation, owned-process cleanup and ACP index/store
  separately. Its lease covers ACP identity, not a cooperative legacy CLI writer lock.
- Preserve the dirty beta2 SDK bridge/controls candidate as a separate patch
  receipt. Do not apply it wholesale or replace native bytes. Its prior provider
  and UI evidence must be rechecked on the selected exact integration source.
- Ask/Yolo policy, sessionless auth helper, native fork/history, startup MCP/Team,
  shared config concurrency and long aggregate history remain explicit follow-up
  work. Public symbols or CLI flags do not prove those user flows. The existing
  16 MiB ACP frame boundary and native aggregate-history cost are unresolved.
- [PR #1 review](pr1-review.md) records two separate correctness findings and
  candidate-source decisions. `--allow-legacy-runtime` is not an accepted Preview solution.

Next: [Studio #4](https://github.com/handochan/aelix-studio/issues/4) consumes
this immutable baseline and known limits, selects candidate patches and probes
the actual Aion host. Then [ACP #7](https://github.com/handochan/aelix-acp/issues/7)
defines the shared management/session contract. [ACP #5](https://github.com/handochan/aelix-acp/issues/5)
implements Ask/Yolo only after those prerequisites. See [the execution map](../studio-preview.md).
