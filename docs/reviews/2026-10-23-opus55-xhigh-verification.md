**결론: P0/P1은 없습니다.** N1/N2가 지적한 주 시나리오는 이번 수정으로 닫혔습니다. 다만 이번 변경분에서 새로 생긴 P2 회귀가 2건 있습니다. 소스만 읽었고 테스트는 실행하지 않았습니다.

## 새 회귀 (P2)

**R1. 반복 abort의 오류를 처리하지 않아 취소가 오류 응답으로 바뀜**
- **위치**: `acp-verified/src/aelix_acp/session.py:624-627`
- **근거**
  - `cancel()`은 abort 실패를 `suppress(ProtocolError, TimeoutError)`로 무시합니다(`:640-642`). 새 루프의 abort는 그렇지 않습니다.
  - native `abort()`는 turn task를 cancel한 뒤 `AbortHookEvent`를 emit합니다. extension handler의 기본 error_mode는 `"throw"`입니다(`core/.../harness/core.py:950`). handler가 예외를 내면 `AgentHarnessError`가 됩니다(`:1874-1878`).
  - 이 예외는 RPC 오류 응답이 되고(`rpc_mode.py:2039`), 어댑터에서 `BackendError`가 됩니다(`session.py:311`). `_prompt`는 `TimeoutError`만 잡습니다(`:610`).
- **트리거**: `abort`나 `queue_update` handler가 예외를 내는 extension이 켜져 있고, 스트리밍 중에 `session/cancel`이 오는 경우입니다. abort가 5초를 넘겨도 `-32007`로 같은 결과가 납니다.
- **영향**: `session/prompt`가 `stopReason: cancelled` 대신 -32603으로 끝나고, `save()`도 건너뜁니다. f86a745에서는 `cancelled`를 반환했습니다.
- **수정**: 반복 abort를 `contextlib.suppress(ProtocolError)`로 감싸고, 예외를 내는 abort hook 테스트를 추가합니다.

**R2. profile이 정한 필터를 GUI 선택처럼 저장해서, 나중에 수정한 profile이 무시됨**
- **위치**: `session.py:231-235`(`sync_profile_tools`), `:245`, `:163`, `:187`
- **근거**
  - `tools:` 목록이 명시된 profile이면 `active_tools`에 그때의 목록이 저장됩니다.
  - load나 restart 때 `start()`는 `agents/use`로 *현재* profile 필터를 설치합니다(`:163`). 그 직후 `restore_tools()`(`:187`)가 저장해 둔 예전 목록으로 덮어씁니다.
- **트리거**: `tools: [read, bash]` profile을 use합니다. 그다음 profile에서 `bash`를 뺍니다. 이후 `session/load`나 `workspace/trust`로 재시작하면 `bash`가 다시 활성화됩니다. `activeAgent`는 같은 profile로 표시됩니다.
- **영향**: f86a745에서는 GUI 선택이 없으면 `active_tools`가 None이라 현재 profile이 그대로 적용됐습니다. 이번 변경으로 도구가 넓어지는 쪽의 새 회귀가 생겼습니다. 권한 정책은 여전히 적용되므로 P2로 봅니다.
- **수정**: 저장할 때 출처(profile인지 user인지)를 함께 기록합니다. profile 출처면 `start()`에서 `agents/use` 뒤에 `restore_tools` 대신 `sync_profile_tools`를 다시 실행합니다.

## 확인한 것

**N1**
- `configure_mcp`(`:200-219`)는 매번 실제 native 필터를 읽어 저장하고, `[]`로 비운 뒤 재구성하고, 새 도구 목록과 겹치는 이름만 복원합니다.
- 실패하면 저장해 둔 `active`로 되돌립니다. native가 `original_tools`로 롤백하므로 이 이름들은 유효합니다(`control_mcp.py:106-108`).
- `asdict(profile)`에는 `tools` 키가 항상 있고, None은 상속을 뜻합니다(`profile.py:167`).
  - `--none`이거나 `tools:`가 없는 profile이면 `follow_all`이 참이 되어 새 MCP 도구가 활성화됩니다.
  - 전체 native 도구를 명시한 profile이면 거짓이 되어 새 MCP 도구가 들어가지 않습니다.
- GUI 선택은 `configure_mcp`가 건드리지 않으므로 제거 후 재추가해도 남습니다(`test_native_runtime.py:122-126`).
- manage 경로(`server.py:314-315`)와 slash 경로(`commands.py:178`) 모두 refresh 뒤에 sync합니다. 두 경로는 `mutation_lock`으로 직렬화됩니다.
- 새 테스트의 1단계(`--none` 뒤 MCP 추가 시 활성화)는 f86a745에서 실패하는 게 맞습니다.

**N2**
- 취소된 상태에서 아직 스트리밍 중이면 abort를 다시 보내므로, 나중에 생성된 turn task도 cancel됩니다.
- 테스트의 `before_agent_start` 대기 지점과 `model_gate`로 막힌 provider 구성이 이 구간을 정확히 재현합니다.

## Native handoff (참고)
- `_run` 진입 시 `_abort_requested`를 초기화하므로, acceptance 이후에 받은 abort가 버려집니다. native가 같은 prompt 안에서 이 플래그를 유지하면 어댑터의 반복 abort가 필요 없어집니다.

## 별도로 남은 known limitation (변경 없음)
- P2-6(16 MiB 프레임 한도)
- native preflight hook과 summary 요청은 강제로 취소할 수 없음
- P2-3(compaction timeout)
- 느린 디스크에서의 커밋 비용