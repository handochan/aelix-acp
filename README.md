# aelix-acp

AionUi 등 ACP 클라이언트를 기존 `aelix --mode rpc`에 연결하는 로컬 PoC입니다.
Aelix 본체는 변경하지 않습니다. Python 표준 라이브러리 실행기와 명시적으로
로드하는 Aelix 승인 브리지 확장으로 구성됩니다. Python 3.11 이상이 필요합니다.

## 로컬 사용

현재 컴퓨터에는 AionUi **2.2.2**와 다음 Custom Agent 두 개가 등록되어 있습니다.
새 채팅 → **더보기**에서 선택합니다.

| 항목 | 실행기 인자 | 로드되는 확장 |
|---|---|---|
| Aelix Coding | `--profile coding` | ACP 승인 브리지 |
| Aelix Analytics | `--profile analytics --analytics-extension /absolute/path/to/aelix-analytics/extension/aelix_analytics.py` | ACP 승인 브리지 + Analytics |

두 항목의 실행 명령은 이 체크아웃의 `bin/aelix-acp`입니다. AionUi에 등록한
인자에는 `--state-dir /Users/handochan/dev/aelix-extensions/aelix-acp/.local/state`도
포함되어 있습니다. 이 폴더를 지우면 ACP 세션 ID와 실제 Aelix 세션 파일의 연결이
사라집니다. Aelix 원본 대화는 기존 `~/.aelix/sessions`에 저장됩니다.

이 PoC의 프로필은 **어댑터의 실행 구성**입니다. `~/.aelix/agent/agents`에
동명의 Aelix 프로필을 설치하거나 기존 사용자 프로필을 덮어쓰지 않습니다.
`--no-extensions`로 자동 탐색을 끄고, 브리지와 선택한 확장 전체를 `-e`로
명시합니다. Aelix의 내장 도구와 상태 도구는 그대로 제공됩니다.

`bin/aelix-acp`는 이 컴퓨터의 uv로 설치된 Aelix Python을 사용합니다.
다른 환경에서는 `AELIX_ACP_PYTHON`을 Python 3.11+ 실행 파일로 지정하거나,
독립 가상환경에 이 패키지를 설치해 `aelix-acp` 진입점을 사용할 수 있습니다.

```sh
bin/aelix-acp --profile coding --aelix /absolute/path/to/aelix
bin/aelix-acp --profile analytics \
  --analytics-extension /absolute/path/to/aelix-analytics/extension/aelix_analytics.py
```

인증은 기존 Aelix 설정을 사용합니다. API 키를 AionUi 설정에 복사하지 않았습니다.
모델은 Aelix 실행 시 결정되며 `--provider PROVIDER --model MODEL`로 고정할 수
있습니다. UI에는 실제 실행 모델 하나를 표시합니다. 대화 중 모델 전환은 아직
구현하지 않았습니다.

## 현재 구현 범위

- ACP v1 stdio 초기화, 세션 생성·목록·복구, 사용자·에이전트 이력 재생
- 텍스트·추론 스트리밍과 도구 시작·결과·실패 표시, 파일 위치 전달
- Ask: 모든 도구의 실행 전 ACP **Allow once / Reject** 승인
- 로컬 개발 후보의 Yolo: client 질문 생략, native guardrail·도구 제한·승인 bridge 유지
- idle 모드 변경 시 동일 native 이력으로 worker 재시작·설정 보존
- 승인 대기 중 취소 시 거부 처리, RPC abort, 자식 프로세스 종료 시 오류 반환
- Aelix `get_commands`에서 가져오는 명령 목록
- 텍스트, 텍스트 내장 리소스, 파일 리소스 링크 입력
- Coding / Analytics 세션의 프로필 교차 복구 거부

승인 브리지 로딩을 임시 파일의 실행별 nonce로 확인한 뒤에만 세션 생성을
완료합니다. 브리지가 없거나 소켓이 끊기거나 승인 시간이 지나면 도구를
차단합니다. 승인 채널은 POSIX의 세션별 0700 임시 디렉터리/0600 Unix 소켓과 Windows의 세대별 토큰으로 인증하는 127.0.0.1 TCP입니다.
이는 로컬 실행 승인 경로이며 OS 샌드박스나 악성 확장 격리를 제공하지 않습니다.

ACP #5 개발 후보의 초기 모드는 `--permission-mode default|yolo`이며 저장 세션은
기록된 모드를 복원합니다. Ask/Yolo 동작과 추가 Plan/Auto Edit/Auto의 가능성은
[권한 검증 기록](docs/beta2/permissions.md)에 구분합니다. 실제 AionCore에서 실패한
권한 변경 뒤 표시와 실행 정책이 달라지는 수용 blocker가 있어 제품 완료 상태는 아닙니다.

AionUi가 전달하는 MCP 서버는 Aelix에 전달하지 않습니다. 비어 있지 않으면
대화에 제한을 알립니다. Aelix 자체에 설정된 MCP 서버는 별도 설정입니다.
이미지·오디오 입력, 동적 모델 변경, MCP 설정 동기화, 스킬/프롬프트 편집과
Python 확장 설치·삭제·리로드 UI는 이번 구현 범위에 포함되지 않습니다.
HTML 미리보기는 AionUi의 로컬 파일 패널 기능을 사용합니다.

Analytics 원본 보고서는 상대 자산 경로 때문에 AionUi의 HTML 미리보기에서
서식과 차트가 빠집니다. 어댑터는 성공한 `analytics_run` 결과에 대해 원본을
보존하면서 `workspace/aion-preview/<run-id>.html`을 추가로 만들고 대화에 링크를
제공합니다. CSS와 압축된 공식 Plotly basic 3.7.0을 포함하는 1MB 이하의 스냅샷으로,
외부 서버가 필요 없습니다. 현재 Plotly bar/pie/scatter만 지원하며, 이 PoC의
1MB 스냅샷 예산을 넘는 페이지나 다른 차트 형식은 원본 보고서를 사용해야 합니다.
전체 Workspace의 탭·경로 탐색을 재현하는 기능은 아닙니다.

AionUi v2.2.2 자체의 텍스트/HTML 미리보기 제한은 **기본 1MiB이며 고정 제한이
아닙니다**. 설정 → 시스템 → 미리보기 크기 제한(MB)에서 1~100으로 변경할 수
있고 새로 연 탭에 적용됩니다. 내부 키는 `preview.textSizeLimitMb`입니다.
크기 설정을 높이는 것과 상대 경로 자산을 로드하는 문제는 별개입니다.

기존 실행의 미리보기를 수동으로 만들 수도 있습니다.

```sh
PYTHONPATH=src python -m aelix_acp.preview /absolute/path/to/analysis-project RUN_ID
```

Plotly 자산은 MIT 라이선스이며 패키지의 `assets/PLOTLY-LICENSE.txt`에 보존합니다.

## Preview 개발 착수

공통 Preview/Beta 설계와 실행 Issue·선행 작업은 [개발 착수 안내](docs/studio-preview.md)를
따릅니다. 이 README의 기존 PoC 기능 설명과 Preview 출시 수용 완료는 구분합니다.
공개 native wheel의 정확한 기준과 실제 subprocess 검증·현재 PR의 재사용 판단은
[beta.2 qualification](docs/beta2/README.md)에 기록합니다.

## 검증

Opt-in `--managed-operations`는 stable prompt operation ID, 중복/충돌 처리,
bounded outcome과 native 시작 없는 status-only transport를 추가합니다.
원본 public beta.2 wheel은 그대로이며 기존 CLI 공통 writer lock이나 exactly-once
외부 효과를 보장하지 않습니다. [wire·저장·검증 계약](docs/beta2/operations.md)을
따르며 Studio #16 forwarding/UI 수용은 별도입니다.

```sh
# Aelix가 설치된 Python 3.11+ 사용. Unix 소켓을 허용하는 환경에서 실행합니다.
python -m unittest discover -s tests -v

# 실제 모델 호출 및 합성 파일 쓰기를 포함합니다. 기존 Aelix 인증을 사용합니다.
python scripts/smoke.py --profile coding
python scripts/smoke.py --profile analytics
```

실제 실행 결과는 [로컬 PoC 검증 기록](docs/local-poc.md)에 구분하여 기록합니다.
전체 대화 로그, 분석 프로젝트, 업데이트 전 백업은 Git에 포함되지 않는
`.local/`에 보존합니다.


## Windows portability candidate (#15)

Windows 표준 입력은 bounded raw-descriptor daemon으로 처리하며 Proactor subprocess 지원을 유지합니다. UTF-8을 명시적으로 설정합니다.
Native 실행 prefix는 반복 `--aelix-arg=VALUE` 배열이고 shell parsing을 사용하지 않습니다.
Windows native bootstrap은 ACP 소유 gate에서 대기하고, kill-on-close Job 연결이 끝난 뒤에만 시작합니다.
MCP 등 첫 RPC 전의 시작 자식도 같은 Job을 상속하며, 승인 토큰은 UI/자동 승인 전에 검사합니다.
잘못된 token·stale generation·cancelled 요청과 초과 연결은 거절합니다. 기존 Ask/Yolo·재개/복구 및 public native wheel은 유지합니다.

이 절은 #15 개발 후보 설명입니다. 전체 OS/설치 수용 결과는 Issue의 현재 SHA·CI·Studio #5 연결 결과로 확인합니다.

독립 소스 리뷰에서 Job 연결 전 native/MCP 시작 자식이 빠질 수 있는 문제를 발견해,
native 대신 소유 gate를 먼저 연결하고 release byte 뒤에만 실행하도록 수정했습니다.
시작 즉시 자식을 만드는 fixture와 의도적인 Job 연결 지연을 Windows 회귀로 추가했습니다.
지원 Python 3.12 로컬 검사 44개 중 43개 통과/Windows Job 1개 미실행이며, 실제 Windows와
5개 native target CI 결과가 나오기 전에는 해당 플랫폼 수용으로 표시하지 않습니다.
