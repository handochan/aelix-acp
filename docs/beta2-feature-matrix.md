# beta.2 Preview 기능 범위와 구현 책임

기준일: 2026-10-24. 대상은 **PyPI 공개 `aelix==0.1.0b2`와 세 하위 패키지의 wheel**이다.
본체 main도 같은 버전 문자열을 쓰므로 `0.1.0b2`라는 문자열만으로 기능을 판정하면 안 된다.
이 문서는 현재 ACP 작업 트리의 구현과 조사 결과이며 배포 완료를 뜻하지 않는다.

출시 구분은 beta.2 기반 Studio Preview → 공개 beta.3 wheel 기반 desktop/web beta다.
beta.3 후보 main 검증을 출시 wheel 검증으로 대체하지 않는다.

## 기능 및 담당 경계

표의 ‘본체 변경 불필요’는 공개 확장 API와 CLI로 구현 가능하다는 뜻이다.
전용 Studio 관리 화면까지 완성됐다는 뜻은 아니다.

| 기능 | 공개 beta.2의 기반 | 이번 ACP 구현 | Studio 책임 | aelix-ai 책임 |
| --- | --- | --- | --- | --- |
| 모델 목록·전환 | TUI와 공개 확장 API에 registry/set_model 존재. 배포 RPC 호출부는 registry 누락 | 인증된 native registry 조회, 표준 모델 선택기, idle 중 같은 프로세스에서 전환, 복구 | 기존 표준 config UI 재사용·실제 모델 옵션 표시 | 이번 연결에는 변경 불필요. beta.3에서는 native RPC 사용 |
| 추론 단계 | 기존 RPC setter 존재 | 모델별 지원 단계, 변경·저장·복원. 비추론 모델 전환 시 유효 단계로 조정 | 기존 Reasoning UI 재사용 | 변경 불필요 |
| steering/follow-up·자동 압축·재시도 | 기존 RPC 지원 | 네 설정의 변경·저장·복원 | 반환된 configOptions 렌더링 | 변경 불필요 |
| 권한 모드 | TUI의 실제 PermissionPosture. beta.2 일반 확장 API에는 동일 live setter 없음 | Ask만 노출. 모든 도구 명시적 승인, 거절/취소/채널 오류 시 차단 | 반환된 모드만 표시·승인 UI | Plan/자동 편집/Auto/전체 접근의 동일 의미 보장은 native beta.3 제어 사용 |
| 도구 on/off | 공개 get_all_tools/get_active_tools/set_active_tools | idle 중 선택·빈 배열·복원. 최초 launch grant 밖으로 확대 거부 | 도구 목록·체크박스 관리 UI | 변경 불필요 |
| 확장 설치/제거/업데이트 | native extension CLI | 승인 후 CLI 실행, 출력·시간·프로세스 수명 제한 | 쓰기 가능한 관리 runtime, 설치/오류/재시작 UI | 변경 불필요 |
| 확장 on/off | 명시적 `-e`, `--no-extensions`로 자동 탐색 차단 | ACP가 선택한 확장 목록 변경 후 재시작, 같은 ACP ID와 transcript 유지 | 선택/승인/재시작 표시 | live unload는 Python import 부작용 때문에 동일 보장 불가. 재시작 사용 |
| 스킬 on/off | `--no-skills`와 명시적 `--skill` 경로 | allowlist를 저장해 재시작. 관리된 스킬의 `/skill:name`은 native loader/formatter로 확장 | 스킬 경로·상속 선택·파일 작성 UI | 변경 불필요. 모든 ambient native 로드 객체의 완전한 live inventory는 beta.3 사용 |
| 프롬프트 추가 | native Markdown template와 `--prompt-template` | 기존 파일/폴더 선택·저장·재시작, 관리된 `/<template>`을 native formatter로 확장 | 파일 작성/선택 UI. ACP가 일반 편집기를 제공하지 않음 | 변경 불필요 |
| 조합으로 프로필 생성 | 기존 AgentProfile Markdown: 모델·추론·도구·스킬·확장·system prompt | native 파서 검증 후 사용자 agents 폴더에 신규 저장, list/show/use. use는 native profile_to_flags로 재시작 | 조합 편집·저장·선택 UI | 새 프로필 형식 불필요 |
| 임의 확장 슬래시·대화상자 | TUI dispatcher/command context와 headless UI는 별도 | beta.2에서는 실행 거부. 관리된 resource 명령만 지원 | 표준 native dialog를 위한 UI bridge, 임의 터미널 위젯은 개별 구현 | 범용 실행·UI·취소는 native beta.3 commands/run 사용 |
| MCP | beta.2 native 사전 구성과 신규 control-v1 전달은 다름 | client mcpServers를 조용히 무시하지 않고 거부 | Preview에서 client MCP 입력 비활성화 | 연결 교체/정리/네임스페이스는 beta.3 native mcp/configure 사용 |
| 인증·OAuth | 기존 native AuthStorage/CLI | 기존 native 인증을 registry가 재사용. Preview live auth 화면은 미구현 | 시작 전 인증 설정, 이후 beta.3 auth UI | held AuthStorage와 동기화되는 beta.3 auth API 사용. 복제 credential store 생성 금지 |
| 프로필 위임/팀 | 프로필 identity와 위임은 별도 | Preview `--no-agents`, 명시적 enable도 거부 | 팀 생성 성공을 실제 위임 실행 성공으로 표시하지 않기 | beta.3 agents/run 및 별도 모델·도구 수용 검증 |

`/skills` 표시와 `/skill:name` 숨기기는 로드 중단과 다르다.
`enable_skill_commands=false`만 바꿔서는 모델 context에서 스킬이 사라지지 않는다.
로드를 끄려면 상속을 끄고 allowlist에서 제거해 재시작한다.
템플릿에는 `--no-prompt-templates`가 없으므로 선택된 경로 목록을 전역 템플릿 전체의 kill switch로 설명하면 안 된다.
시작 시 `--permission-mode` 옵션이 있는 것과 ACP에서 TUI와 같은 live 권한 제어를
제공하는 것은 구분한다. 이번 Preview는 동일 의미를 검증하지 않은 고급 모드를 노출하지 않는다.

## beta.2 TUI의 36개 기본 명령과 ACP 대응

| TUI 명령 | 현재 beta.2 ACP 대응 |
| --- | --- |
| help, model, thinking, mode, permissions | 제공. mode는 steering이며 권한은 permissions. permissions는 Ask만 |
| compact, cost, stats, session, name, context | 기존 RPC 사용. context는 통계 응답이며 TUI의 모든 패널과 동일하지 않음 |
| tools | launch grant 내 도구 조회/선택. 빈 선택은 tools/set_active names=[] |
| skills | ACP 선택 리소스·관리 명령·진단 표시. 전체 native loaded inventory라는 주장은 하지 않음 |
| agents | list/show/use 제공. 신규 저장은 agents/save 관리 API. run은 미지원 |
| extension, reload | 설치/관리 CLI 및 승인 후 native child 재시작. loadState=not_observed와 실제 extension loader 성공은 구별 |
| settings | 세션 configOptions만 조회/변경. 테마·모든 native 영구 설정은 미지원 |
| export, copy | HTML 내보내기 / 마지막 assistant text 반환. OS clipboard 복사는 Studio 책임 |
| tree | fork 가능한 message 목록. TUI의 부모 lineage 화면과 동일하지 않음 |
| quit | native child 종료, ACP index와 transcript 보존 |
| new, resume, clear, import, statusline, hotkeys, expand | Studio clientActions를 명시적으로 광고한 경우에만 UI 요청 전달. 실제 화면·import 작업은 Studio 책임 |
| login, logout, trust, hooks, mcp, scoped-models | Preview 명령으로 제공하지 않음. 지원하지 않는 명령은 구조화된 오류 |
| fork, clone | beta.2 ACP에는 미노출. control-v1의 비파괴 branch API 사용 |

알 수 없거나 지원하지 않는 `/명령`은 LLM 요청으로 흘려보내지 않는다.
명시 선택 스킬·템플릿은 관리된 시작 시점 snapshot을 native 공개 loader로 읽고,
native `expand_resource_command`로 user turn text를 만든다. 파일 수정 후에는 reload가 필요하다.
ambient resource나 임의 확장 handler의 전면 호환을 이 경로의 성공으로 간주하지 않는다.
기본 명령 이름과 같은 템플릿 이름은 기본 명령을 덮어쓰지 않는다.

## 프로필의 의미와 제한

native 필드: `name`, `description`, `model`, `provider`, `thinking`, `tools`,
`builtin_tools`, `skills`, `inherit_skills`, `extensions`, `inherit_extensions`,
`context_files`, `system_prompt`, `role`, `output_cap`, `timeout_ms`, `approval_mode`.

- `approval_mode`는 위임 정책 필드이며 최상위 permission_mode가 아니다.
  프로필을 적용해도 Preview가 Auto/전체 접근으로 바뀌지 않는다.
- native parser의 경로 정규화, 프로젝트 신뢰·스코프 제한과 필수 경로 검사를 사용한다.
  프로젝트 프로필을 허용하려면 운영자가 ACP `--trust-project`를 설정해야 한다.
- 프로필 use는 저장된 이전 모델·추론·도구 override를 초기화한 다음 프로필을 적용한다.
  운영자의 명시적 ACP `--provider`/`--model`은 프로필 기본값보다 우선한다.
  이후 사용자 모델·추론·도구 선택은 세션 override로 저장된다.
- `--no-extensions`와 `--agent`의 native 충돌을 우회하기 위해 native가 제공하는
  `profile_to_flags`를 사용한다. bridge는 항상 마지막 확장으로 별도 로드한다.
  프로필의 inherit_extensions=true도 ACP의 자동 확장 탐색 차단을 해제하지 않는다.
- 프로필을 선택한 뒤에는 스킬 구성이 프로필 소유다. resources/configure에서
  스킬을 덮어쓰지 않는다. 프로필 수정·재선택으로 변경한다.
- 프로필 원본 hash와 컴파일된 허용 CLI 옵션만 저장한다. 종료 후 원본이 바뀌거나
  사라졌으면 resume을 거부한다. 파일을 복원하거나 새 세션에서 수정 프로필을 선택한다.
  실행 중인 세션에서는 수정 프로필을 다시 use할 수 있다.
- 생성 API는 기존 파일을 덮어쓰지 않는다. 업데이트·삭제 편집은 기존 native 파일을
  사용자가 편집한 뒤 다시 선택한다. prompt_templates는 native 프로필 필드가 아니므로
  이번에는 세션의 별도 리소스 선택으로 관리한다.
- `agents/use`의 name="--none"은 승인 후 프로필을 해제하고 기본 실행 구성으로 재시작한다.

## 최소 Studio 변경과 출시 순서

필수 호환 변경은 최종 ACP 버전/commit/checksum pin과 기존 PoC patch 중복 적용 제거다.
공개 beta.2 wheels를 유지한다. 모델·추론은 기존 표준 config UI를 사용한다.
도구·스킬·템플릿·프로필·확장 전용 관리 화면은 Studio의 추가 구현 범위다.
API는 아래 [Preview 관리 계약](beta2-preview.md)을 따른다.

1. ACP 코드·문서·회귀 검증 후 고정된 wheel을 만든다.
2. Studio Preview를 해당 ACP + 공개 beta.2로 재패키징하고 실제 UI·설치 검증한다.
3. beta.3 공개 후 실제 wheels를 대상으로 control-v1을 검증한다.
4. beta.2 리소스/프로필 preferences의 명시적 migration과 업그레이드 검증 후
   공식 desktop/web beta를 배포한다. 지금은 제한을 버리는 자동 전환을 거부한다.

남은 본체 의존 작업은 권한 posture, 범용 native 명령/UI, MCP, 인증/위임 제어다.
네이티브 16 MiB aggregate frame/history 제한도 ACP에서 해결된 것으로 표시하지 않는다.
검증 결과는 [제품 검증 기록](product-validation.md)에 실제 프로세스/합성 모델,
실제 provider, UI, OS 패키지, 원격 CI를 나눠 기록한다.

## 조사 및 구현 근거

- [공개 beta.2 TUI 36개 명령·native 프로필 필드·원본 source hash](reviews/evidence/2026-10-23-beta2-feature-surface-audit.json)
- [초기 확장 레지스트리 연결 실증](reviews/evidence/2026-10-23-beta2-registry-extension-proof.json)
- [현재 126개 테스트·설치 wheel·Studio 호스트 검증](reviews/evidence/2026-10-24-beta2-strengthening.json)
- [native 공개 API 연결 코드](../src/aelix_acp/bridge_controls.py), [Preview 관리 코드](../src/aelix_acp/runtime/preview.py), [실제 beta.2 회귀 테스트](../tests/test_beta2_runtime.py)
