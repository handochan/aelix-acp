# Aelix ACP PR #1 적대적 리뷰 (head `7f49cab`, base `71eabb4`, core `10854a2`)

범위는 `acp/` 어댑터와 이 어댑터가 core에 대해 전제하는 동작입니다. 코드는 실행하지 않고 소스만 읽었습니다. 아래 라인 번호는 모두 스냅샷에서 직접 확인했습니다. 해당하는 경우 core 쪽 근거도 함께 적었습니다.

**결론: P0는 없습니다. P1 4건, P2 6건입니다.** 기존 테스트는 합성 모델 endpoint, 단일 adapter 프로세스, 성공 경로 위주라서 아래 실패 경로를 다루지 않습니다.

---

## P1

### P1-1. `auto-accept-edits`/`auto` 모드가 프로젝트 밖이나 민감 경로 쓰기를 승인 요청 없이 허용 — 확정
- **위치**: `acp/src/aelix_acp/runtime/policy.py:8-9`, `acp/src/aelix_acp/session.py:331-340`
- **근거**
  - core의 같은 이름 모드는 내장 write/edit를 "cwd 안 + 민감하지 않은 경로"일 때만 자동 허용하고, 나머지는 사용자에게 묻습니다 (`core/packages/aelix-coding-agent/src/aelix_coding_agent/builtin/permission.py:687-705`, `714-720`). `.aelix`, `.ssh` 등은 민감 경로입니다 (`474-476`).
  - RPC 모드에는 UI가 없어서, 묻는 대신 headless ALLOW로 처리됩니다 (`permission.py:742-745`).
  - 그러면 최종 판단은 어댑터의 `permission_decision()`에 남는데, 여기서는 provenance가 `write`이면 경로와 무관하게 `allow`입니다.
  - write 도구는 절대경로를 그대로 씁니다 (`core/packages/aelix-coding-agent/src/aelix_coding_agent/tools/_path_utils.py:37-48`).
- **트리거**: Accept edits나 Auto 모드에서 모델이 `write`/`edit`로 `~/.zshrc`, `~/.ssh/authorized_keys`, cwd 밖 경로, 또는 프로젝트 안 `.aelix/agents/x.md`에 씁니다. 저장소 내용을 통한 prompt injection으로도 유도할 수 있습니다.
- **영향**
  - 네이티브 TUI에서는 승인 프롬프트가 뜨는 쓰기가 ACP에서는 `session/request_permission` 없이 실행됩니다.
  - architecture.md의 "Native policy precedes client approval" 전제가 headless에서는 성립하지 않습니다.
  - `.env`/`.git`은 Guardrail이 따로 막습니다.
  - README(68-69)는 "확인된 내장 쓰기·편집만 허용"이라고만 쓰고, 프로젝트 밖 허용을 수용 한계로 적지 않았습니다.
- **재현**: native 테스트에서 `permission_mode=auto-accept-edits`로 두고, 합성 모델이 `<cwd>/.aelix/agents/evil.md`나 cwd 밖 임시 디렉터리에 write하게 합니다. 기대는 `request_permission` 1회, 실제로는 0회이고 파일이 생성됩니다.

### P1-2. 첫 `agent_end`를 turn 종료로 판정 — auto-retry·overflow 복구·threshold compaction과 계약 불일치 — 확정
- **위치**: `acp/src/aelix_acp/session.py:291-295`, `305-306`, `505-516`
- **근거**
  - core는 provider 오류가 나면 `message_end`(errorMessage), `turn_end`, `agent_end`를 차례로 보냅니다 (`core/packages/aelix-agent-core/src/aelix_agent_core/loop.py:224-227`).
  - 그 뒤 같은 `prompt()` 안에서 backoff 후 `_run`을 다시 실행합니다 (`core/packages/aelix-agent-core/src/aelix_agent_core/harness/core.py:1549-1605`).
  - auto-retry는 기본으로 켜져 있습니다 (`core/packages/aelix-ai/src/aelix_ai/settings/settings_manager.py:1327-1333`).
  - overflow 복구와 threshold compaction도 `agent_end` 이후 같은 turn의 일부로 처리되며, 그동안 phase는 idle이 아닙니다 (`core/packages/aelix-coding-agent/src/aelix_coding_agent/rpc/rpc_mode.py:540-546`).
- **트리거 A — 재시도 가능한 오류 1회** (429/503/overloaded 등)
  - `self.failure`가 설정되고, 첫 `agent_end`에서 클라이언트는 -32603 오류를 받습니다. turn은 `interrupted`로 기록됩니다.
  - 그런데 native는 재시도해서 성공합니다. 그 출력, 도구 호출, 권한 요청은 이미 끝난 turn 밖에서 스트리밍됩니다.
- **트리거 B — threshold compaction이 걸리는 긴 세션**
  - 어댑터는 `end_turn`을 반환하지만 native는 아직 compaction 중입니다.
  - 사용자가 바로 다음 prompt를 보내면 core가 busy로 거부합니다 (`rpc_mode.py:369-387`).
  - 이 거부는 `interrupted`로 기록되어, 같은 requestId로 재시도하면 -32006으로 영구히 막힙니다. 실행되지 않은 요청이 "unresolved"로 저널링되는 것입니다.
- **재현**: 합성 서버가 첫 요청에 HTTP 429, 두 번째에 정상 응답을 주게 합니다. 그러면 prompt는 오류로 끝나고 `NATIVE_OK`가 응답 뒤에 도착합니다. 직후 보낸 prompt는 busy 오류를 받습니다.

### P1-3. 스트리밍 turn 내내 SQLite 쓰기 트랜잭션이 열려 있음 — 다른 ACP 프로세스가 10초 막힌 뒤 실패 — 확정
- **위치**
  - `acp/src/aelix_acp/runtime/store.py:16`: 기본 isolation, `timeout=10`
  - `acp/src/aelix_acp/runtime/store.py:48-51`: 이벤트 INSERT 후 commit 없음
  - `acp/src/aelix_acp/session.py:308-311`, `491-493`, `509-515`: turn 중 기록 흐름
  - `acp/src/aelix_acp/session.py:237-238`: 실패 전파
- **근거**
  - Python sqlite3의 legacy 모드는 첫 INSERT에서 암묵 트랜잭션을 열고 commit까지 유지합니다.
  - `_prompt`는 사용자 블록을 493에서 커밋한 뒤, 스트리밍 `update()`가 만드는 INSERT를 turn이 끝나는 `save()`(515)까지 커밋하지 않습니다.
  - WAL은 writer를 1명만 허용하므로 turn 동안 write lock이 유지됩니다.
- **트리거**: 같은 `--state-dir`(기본 `~/.aelix/acp`)를 쓰는 adapter 프로세스가 둘 있는 경우입니다. lease 설계와 README:59, architecture.md:81-83이 이 구성을 지원 시나리오로 다룹니다. A가 10초 넘는 turn을 스트리밍하는 동안 B가 `session/new`, `session/prompt`, chunk 기록 중 하나를 시도합니다.
- **영향**
  - B의 이벤트 루프 전체가 최대 10초 동기적으로 막힌 뒤 `database is locked`가 납니다.
  - `read_rpc` 안에서 나면 B의 세션이 종료되고 child가 kill됩니다.
  - 문서는 hard kill 시 미커밋 chunk 손실만 다루고, 프로세스 간 잠금 문제는 다루지 않습니다.
- **재현**: 같은 디렉터리에 `open_store`를 두 번 엽니다. A에서 `append_event`만 하고 commit하지 않은 상태로 B에서 `begin_turn`을 호출하면 약 10초 뒤 `OperationalError`가 납니다.

### P1-4. 저장된 설정 복원이 실패하면 치명적 — 세션을 영구히 load할 수 없음 — 확정
- **위치**
  - `acp/src/aelix_acp/session.py:140-150`: 저장된 agent·config 재적용
  - `acp/src/aelix_acp/runtime/controls.py:114-117`: 카탈로그에 없는 값이면 예외
  - `acp/src/aelix_acp/server.py:153-154`, `173-177`: load 실패 처리
  - `acp/src/aelix_acp/session.py:415-427`: restart 경로
- **근거 (a) 모델**
  - `get_available_models`는 인증된 모델만 반환합니다 (`core/packages/aelix-coding-agent/src/aelix_coding_agent/rpc/rpc_mode.py:934-957`).
  - core는 세션 모델을 복원하지 못하면 다른 모델로 fallback합니다 (`core/packages/aelix-coding-agent/src/aelix_coding_agent/cli/entry.py:3453-3478`, `3513-3522`).
  - 어댑터는 그 뒤 저장된 모델을 다시 적용하려 하고, 카탈로그에 없으면 "Unsupported value for model"(-32602)이 나서 `session/load`가 실패합니다.
- **근거 (b) agent profile**
  - 저장된 agent profile이 삭제되면 `ProfileError`가 납니다 (`core/packages/aelix-coding-agent/src/aelix_coding_agent/agents/service.py:245-251`).
  - 또 이 시점에는 client MCP가 아직 구성되지 않았습니다 (`server.py:157-161`이 start 이후에 실행). 그래서 `tools:`에 MCP 도구를 적은 profile은 항상 실패합니다.
- **트리거**: provider 로그아웃이나 키 제거, GUI 실행으로 env 키가 상속되지 않음, models.json에서 모델 제거, agent profile 파일 삭제.
- **영향**
  - 세션을 열 수 없으니 ACP로 설정을 바꿔 복구할 방법도 없습니다. 매 load가 같은 오류로 실패하고, 오류 코드(-32602)도 잘못된 파라미터처럼 보입니다.
  - `restart()`도 같은 구조입니다. 확장/trust/delegation 변경을 `start()` 전에 저장하고(421), 실패하면 세션을 닫은 채로 둡니다(425-427).
- **재현**: model을 `fixture/two`로 바꾸고 prompt를 보낸 뒤 종료합니다. models.json에서 `two`를 지우고 다시 `session/load`하면 오류가 납니다. 기대 동작은 fallback 모델로 열리면서 경고를 주는 것입니다.

---

## P2

### P2-1. load/new에서 client MCP를 도구 선택 복원 뒤에 구성 → 선택한 MCP 도구가 비활성 — 확정
- **위치**: `acp/src/aelix_acp/server.py:153-161`, `acp/src/aelix_acp/session.py:72`, `156-158`, `164-169`
- **근거**
  - 새 `Session`의 `client_mcp`는 `[]`입니다. 그래서 `start()` 안의 `restore_tools()`는 MCP 도구가 없는 상태에서 저장된 선택을 걸러 적용합니다.
  - 이후 `mcp/configure`는 기존 active filter를 그대로 유지합니다 (`core/packages/aelix-agent-core/src/aelix_agent_core/harness/core.py:3494-3513`, `core/packages/aelix-coding-agent/src/aelix_coding_agent/rpc/control_mcp.py:101-105`). 결과적으로 `acp_NAME__TOOL`은 필터 밖에 남습니다.
  - manage 경로(`server.py:291-293`)와 restart 경로는 `restore_tools()`를 다시 호출하지만 load/new 경로만 빠져 있습니다.
- **부수 효과**: 선택된 MCP 도구가 있는 상태에서 그 서버를 제거하는 `mcp/configure`는 core의 stale-name 검증에 걸려 실패합니다.
- **재현**: 기존 MCP 테스트 끝에 재시작 + 같은 `mcpServers`로 `session/load`를 추가하면, `activeTools`에 `acp_fixture__echo`가 없습니다.

### P2-2. fork/clone 뒤 부모 세션의 도구 선택이 런타임에서 초기화 — 확정 (소스 추적)
- **위치**: `acp/src/aelix_acp/server.py:77-98` (특히 96-97)
- **근거**
  - core는 branch를 만든 뒤 `switch_session(original)`로 되돌아갑니다 (`core/packages/aelix-coding-agent/src/aelix_coding_agent/rpc/control.py:358-366`).
  - 이때 harness를 reload seed 없이 새로 만들고 (`core/packages/aelix-agent-core/src/aelix_agent_core/runtime/agent_session_runtime.py:679-691`), active tools는 기본값으로 다시 정해집니다 (`core/packages/aelix-coding-agent/src/aelix_coding_agent/cli/entry.py:1873`).
  - 어댑터는 그 뒤 `restore_tools()`를 호출하지 않습니다.
- **영향**: 런타임은 전체 도구가 활성인데, 저장값과 `session.active_tools`는 여전히 제한된 선택입니다.
- **재현**: `tools/set_active [read]` → `/clone` → 부모 세션의 `activeTools`가 `[read]`가 아닙니다.

### P2-3. `/compact`·`/extension reload`가 기본 45초 RPC deadline을 사용 — 확정
- **위치**: `acp/src/aelix_acp/runtime/commands.py:108-109`, `acp/src/aelix_acp/runtime/extensions.py:51-52`, `acp/src/aelix_acp/session.py:187`, `198-204`
- **근거**: core의 `compact`는 요약 LLM 호출이 끝날 때까지 응답하지 않습니다 (`core/packages/aelix-coding-agent/src/aelix_coding_agent/rpc/rpc_mode.py:588-606`). 어댑터는 timeout 시 `commands/run`·`auth/login`에만 취소를 보냅니다.
- **영향**
  - 긴 대화에서 `/compact`가 -32007로 실패 표시되고 turn은 `interrupted`가 되지만, native는 compaction을 계속하고 다음 prompt는 busy로 거부됩니다.
  - `/reload`는 `command_timeout`을 쓰는데 `/extension reload`만 45초라 경로마다 다릅니다.

### P2-4. 권한 요청과 `tool_execution_start` 처리 사이의 race → 프롬프트 없이 거부 — 코드상 확정, 발생은 타이밍 의존
- **위치**
  - `acp/src/aelix_acp/session.py:263-270`: 클라이언트 전송을 await한 뒤에야 `self.tools`에 등록
  - `acp/src/aelix_acp/session.py:328-330`: 미등록 ID는 조용히 거부
- **근거**: core는 시작 이벤트를 쓴 직후 hook을 실행합니다 (`core/packages/aelix-agent-core/src/aelix_agent_core/loop.py:746-756`, `841-851`). `read_rpc`가 전역 `output_lock`이나 stdout 역압에 막혀 있으면 bridge 연결이 등록보다 먼저 도착합니다.
- **영향**: fail-closed이긴 하지만, 승인 UI 없이 모델이 "User declined"를 받습니다.
- **테스트**: `server.send`를 지연시키는 fake를 두고, `event()` 처리와 permission 연결을 동시에 보내면 현재 `{"allow": false}`가 나옵니다.

### P2-5. slash 전개 이후 `cancelled`를 다시 False로 설정해 취소가 유실 — 코드상 확정, native abort 부분은 가설
- **위치**: `acp/src/aelix_acp/session.py:454`, `494-506`
- **근거**
  - prompt template/skill이 출력 메시지를 `await output()`하는 동안 `session/cancel`이 오면, 그 시점의 `commands/cancel`과 `abort`는 아무 효과가 없습니다.
  - 그 뒤 506에서 `cancelled`가 False로 덮어써지고, 전개된 prompt가 모델로 전송됩니다.
  - 일반 prompt도 acceptance 전에 abort가 도착하면 무효일 가능성이 있습니다. core 주석상 abort 플래그는 `_run` 진입 시 초기화됩니다 (`core/packages/aelix-agent-core/src/aelix_agent_core/harness/core.py:1569-1586`).
- **영향**: turn이 끝까지 실행되면서 도구만 전부 거부되고, 마지막에 `cancelled`로 응답합니다. 토큰은 계속 소비됩니다.

### P2-6. child가 보낸 16 MiB 초과 JSONL 한 줄이 세션 전체를 종료 — 한도는 확정, 도달 크기는 시나리오
- **위치**: `acp/src/aelix_acp/session.py:130`, `218-238`, `acp/src/aelix_acp/server.py:166`
- **근거**
  - `readline()`의 한도 초과 오류가 루프 바깥 `except Exception`에 잡혀 RPC 스트림을 닫습니다.
  - core의 `agent_end`는 그 실행의 모든 메시지를 한 줄에 담고 (`core/packages/aelix-agent-core/src/aelix_agent_core/loop.py:276`), 예외 종료 경로는 전체 상태를 담습니다 (`core/packages/aelix-agent-core/src/aelix_agent_core/harness/core.py:5478`).
  - `get_messages`도 전체 이력을 한 줄로 반환합니다.
- **영향**: 이미지 읽기가 누적된 turn은 중간에 사라지고, 이미지가 많은 branch 세션은 load할 수 없습니다.
- **테스트**: fake child가 17 MiB짜리 `agent_end`를 출력하게 하면 세션이 종료됩니다.

---

## 검증하지 못한 위험 (가설)
1. **Windows lease 재획득**: `acp/src/aelix_acp/runtime/leases.py:20-26`은 unlock 없이 close만 한 직후 비차단(`LK_NBLCK`)으로 다시 잠급니다. Windows는 close 시 잠금 해제가 지연될 수 있다고 문서화하고 있어, restart나 load-replace에서 간헐적으로 "another ACP process"가 날 수 있습니다. Windows CI 로그와 대조해 보시길 권합니다.
2. **stderr 역압**: 클라이언트가 adapter의 stderr 파이프를 읽지 않으면 세션별 writer가 막히고(`acp/src/aelix_acp/session.py:208-216`), child의 stderr 파이프가 찹니다. core는 stdout의 print를 stderr로 보내므로 해당 세션이 hang합니다. 이벤트 루프 자체는 막히지 않습니다.
3. **branch 후 토글 덮어쓰기**: rebuild 뒤 auto-compaction/auto-retry/steering이 settings 기본값으로 돌아가면, `server.py:96-97`의 refresh→save가 사용자 선택을 영구히 덮어쓸 수 있습니다. `set_auto_*`가 settings에 기록되는지는 확인하지 못했습니다.
4. **delete/load 동시 호출**: `session/delete`(`server.py:181-204`)는 `starting_ids`를 예약하지 않습니다. 같은 ID로 load가 동시에 오면, 삭제된 행이 다음 `save()` 때 되살아날 수 있습니다.
5. **저널 분류**: 클라이언트의 JSON-RPC 오류가 기본 -32602로 바뀌고(`server.py:348-349`), `rejected`로 영구 저장됩니다(`acp/src/aelix_acp/runtime/store.py:80-82`). `/fork`로 branch를 만든 뒤 `open_session`이 실패하는 경우처럼 부작용이 이미 일어난 뒤에도 "확정적으로 무효"로 기록됩니다.
6. **delegated child와 브리지**: delegated child가 `AELIX_ACP_ENDPOINT`를 상속해 브리지를 로드한다면, 그 도구 ID는 부모의 `self.tools`에 없어 모두 거부됩니다.
7. **`--turn-timeout` 의미**: help 문구는 "per model turn"이지만, 실제로는 전체 agent run과 사용자 승인 대기 시간(건당 최대 290초)을 모두 포함합니다 (`server.py:405`, `session.py:510-511`).
8. **MCP 전부 아니면 전무**: 클라이언트가 넘긴 MCP 서버 중 하나만 실패해도 `session/new` 전체가 실패합니다 (`core/packages/aelix-coding-agent/src/aelix_coding_agent/rpc/control_mcp.py:84-85`).
9. **blob resource 거부**: `embeddedContext`를 광고하면서 blob resource는 거부합니다 (`acp/src/aelix_acp/session.py:478-482`).

## 결함으로 계산하지 않은 문서화된 한계
- unknown slash 오류와 `//` 처리
- turn 중 설정 변경 거부
- Windows Job Object 부재
- `--no-extensions` 때문에 설치 후 명시적 enable이 필요함
- `--offline`의 범위
- hard kill 시 미커밋 chunk 손실
- delete 시 native transcript 파일 보존