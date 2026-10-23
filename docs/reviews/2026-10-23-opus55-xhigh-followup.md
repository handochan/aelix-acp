# ACP 수정(f86a745) 후속 리뷰

**결론: 아직 열려 있는 P0/P1은 없고, 이번 수정으로 새로 생긴 P0/P1도 없습니다.** P1-1~P1-4는 소스와 core 쪽 전제까지 확인했고 맞게 고쳐졌습니다. 다만 수정이 덜 된 P2가 2건 있습니다(N1, N2). 기존 P2-6은 그대로 남아 있습니다. 실행 없이 소스만 읽었습니다.

## 확인한 수정
- **P1-1**: `bridge.py:37-48`이 실제 `event.args`와 `ctx.cwd`로 native `_is_auto_allowable_write`를 호출합니다. 이 함수는 원래 경로와 symlink를 푼 실제 경로를 둘 다 검사합니다(`core/.../permission.py:511-567`). 호환되지 않으면 False가 되어 사용자에게 묻습니다. `policy.py:8`은 `write_allowed is True`일 때만 자동 허용합니다. 테스트는 두 모드에서 cwd 밖 경로와 `.aelix` 경로(`test_native_runtime.py:254-267`), 프로젝트 안 허용(`306-314`)을 다룹니다.
- **P1-2**: core는 prompt를 받아들이기 전에 phase를 `turn`으로 바꾸고(`harness/core.py:1403`, `1451-1456`), prompt의 `finally`에서만 idle로 되돌립니다(`1758`). `get_state`의 `isStreaming`은 `phase != "idle"`입니다(`rpc_mode.py:546`). 이벤트는 stdout에 동기로 기록되므로(`rpc_mode.py:2205-2230`) idle 응답보다 앞선 이벤트가 먼저 도착합니다. InputHandled도 같은 `finally`를 지납니다. 어댑터 쪽 `session.py:310-311`, `579-583`, `595-602`도 맞습니다.
- **P1-3**: `store.py:52-54`가 이벤트마다 커밋합니다. 다른 DML도 모두 await 없이 바로 커밋되므로, 공유 연결에서 다른 트랜잭션이 중간에 커밋되는 일은 없습니다.
- **P1-4**: `session.py:153-186`에서 client MCP 구성, profile 실패 시 `--none` 적용, 쓰기 자동 허용 모드의 default 강등, 없는 설정값에 대한 경고가 모두 확인됩니다.
- **나머지**:
  - fork/clone 후 도구 선택 복원(`server.py:97`)
  - compact·extension reload의 timeout(`commands.py:109`, `extensions.py:52`, `session.py:253-258`)
  - 도구 등록을 UI 출력보다 먼저 처리, bridge 요청은 해당 도구 이벤트를 기다림(`session.py:324-327`, `383-390`)
  - 삭제 시 lease·mutex 유지(`server.py:193-213`), load 시 index 재확인(`session.py:103-107`)
  - restart 시 lease 유지(`466-473`, `490-492`), 설정을 lease 획득 뒤 다시 읽음(`108`)
  - 클라이언트 RPC 오류를 -32603으로 분류(`server.py:358-361`)

## 새로 발견했거나 덜 고쳐진 P2

### N1. MCP 재구성이 `agents/use`가 고정한 native 도구 필터를 처리하지 않음 (P2-1 부수 효과가 일부 남음)
- **위치**
  - `acp-final/src/aelix_acp/session.py:200-205`: `self.active_tools is not None`일 때만 필터를 비움
  - `session.py:161-169`: 저장된 agent 복원
  - `server.py:299-300`, `310-311`: `agents/use` 뒤에 `active_tools`를 갱신하지 않음
- **근거**
  - native `agents/use`는 프로필에 `tools:`가 없을 때와 `--none`일 때도 `set_active_tools(현재 전체 registry)`로 명시 필터를 설치합니다 (`core/.../agents/service.py:466-472`).
  - `set_tools`는 기존 필터에 있던 이름이 새 목록에서 사라지면 예외를 내고, 그렇지 않으면 기존 필터를 유지합니다 (`harness/core.py:3502-3510`).
- **트리거**: 저장된 agent가 있는 세션을 load했거나(복구 경로의 `--none` 포함) manage로 `agents/use`를 한 뒤, `tools/set_active` 없이 `_aelix/manage mcp/configure`로 서버를 제거하는 경우입니다.
- **영향**
  - 서버를 제거하면 native stale-name 오류로 -32603이 나고, 제거할 수 없습니다.
  - 서버를 추가하면 도구는 로드되지만 필터 밖이라 조용히 비활성으로 남습니다. 프로필에 `tools:`가 없거나 `--none`인 경우입니다.
- **테스트 공백**: `test_native_runtime.py:117-121`은 사용자가 도구를 명시적으로 선택한 경우만 다룹니다.
- **제안**: `configure_mcp` 전에 `resources/list`의 `activeTools`를 저장합니다. native 필터가 있으면 항상 비운 뒤 재구성하고, 이전 필터 ∩ 새 registry로 복원합니다. 이전 필터가 전체 registry였다면 새 MCP 도구도 포함합니다.

### N2. 취소 시 보낸 abort가 native의 "acceptance 후 turn task 생성 전" 구간에서 유실됨 (P2-5 미완)
- **위치**
  - `session.py:579-583`: acceptance 직후 abort를 한 번만 다시 보냄
  - `session.py:604-616`: `commands/cancel` 왕복이 끝난 뒤에야 abort를 보냄
- **근거**
  - core는 `on_accept`(`1456`) 뒤에 `before_agent_start` hook(`1522`)을 await합니다.
  - `_run`은 진입할 때 `_abort_requested=False`로 초기화하고(`5158`), `build_context`를 await한 뒤(`5166-5168`) 플래그 확인 없이 turn task를 만듭니다(`5426-5435`).
  - `abort()`는 그 시점에 이미 존재하는 turn task만 cancel합니다(`1843-1845`).
- **트리거**: acceptance 직후나 `wait_until_idle` 초기에 `session/cancel`이 오는 경우입니다. hook이 있거나 컨텍스트가 큰 세션에서는 이 구간이 넓어집니다.
- **영향**: 모델 요청이 그대로 진행됩니다. 도구는 모두 거부되지만, 응답은 run이 끝나거나 `turn_timeout`이 될 때까지 늦어지고 토큰이 소비됩니다. 최종 결과는 `cancelled`입니다.
- **제안**: `wait_until_idle`에서 `self.cancelled`이고 아직 `isStreaming`이면 abort를 주기적으로 다시 보냅니다.

## 잔여 위험 (낮음, native 계약에 의존, 회귀 아님)
- **P2-3**: timeout 시 보내는 abort는 진행 중인 summary 요청이 아니라 대기와 재시도만 끊는 것으로 보입니다 (`harness/core.py:5994-5998`, 확정은 아님). 600초를 넘기면 어댑터는 -32007을 반환하지만 compaction은 계속 진행되어 저장되고, 다음 prompt는 busy로 거부됩니다. P1-2 수정으로 threshold compaction 시간도 `turn_timeout`에 포함되어 같은 경로를 탑니다.
- **P1-3 성능 (측정 안 함)**: 이제 스트리밍 delta마다 `synchronous=FULL` 커밋이 이벤트 루프 안에서 실행됩니다(`store.py:19`, `52-54`). 느린 디스크에서는 처리량이 떨어질 수 있습니다.
- **기존 가설 1 (Windows lease)**: restart 경로는 lease 유지로 해소됐습니다. load가 기존 세션을 대체하는 경로(`server.py:153-159`)는 여전히 close 직후 lease를 다시 잡습니다.

## 알려진 미해결 P2-6 (변경 없음)
- `session.py:143`의 16 MiB 한도는 그대로이고, `read_rpc`에서 예외가 나면 세션이 종료됩니다(`292-306`).
- 기록된 이벤트가 없으면 load 때 `get_messages`로 전체 이력을 한 프레임으로 받습니다(`server.py:166`).
- 어댑터가 더 이상 `agent_end`로 turn 종료를 판단하지는 않지만, 그 프레임 자체는 여전히 읽어야 하므로 한도 문제는 같습니다.
- compact-event나 paged-history 같은 native 계약이 필요합니다. 이번 수정으로 생긴 회귀는 아닙니다.