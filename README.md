# aelix-acp

Aelix Studio와 다른 ACP 클라이언트를 실제 Aelix 런타임에 연결하는
Python 3.11+ 어댑터입니다. `0.2.0a1`은 첫 공동 Beta를 위한 **개발 후보**입니다.
ACP 패키지는 표준 라이브러리만 사용하며, Aelix는 별도 프로세스로 실행합니다.

이 버전은 Aelix의 **runtime-control v1**을 필요로 합니다. 공개 `aelix==0.1.0b2`에는
이 API가 없어 세션을 시작하지 않습니다(`-32004`). 검증한 native 소스는
[`runtime-reference.json`](runtime-reference.json)에 고정합니다. 지금은 beta.3 출시 전
aelix-ai main 후보이며, beta.3가 나오면 그 tag로 다시 고정합니다.
beta.2 Preview 내부 검증 빌드는 ACP `117cf6c9d536c4aa98b1f3a6481c12464584960c`를 그대로 씁니다.

## 구현 범위

| 기능 | 어댑터·본체 구현 | Studio 연결 |
|---|---|---|
| 세션 생성·목록·복구·분기·종료 | native session + SQLite 이력·설정·요청 기록 | 표준 ACP, 분기 전환은 client action |
| 모델·추론·권한 변경 | native setter, 모델별 추론 수준, 실행 전 승인 | `configOptions`, legacy modes/models |
| 텍스트·추론·도구·이미지 입력 | 스트리밍, 실패·취소, 파일 위치 | 표준 ACP |
| 서브에이전트 진행 | native `aelix_subagent` → 부모 `agent` 도구 호출의 `tool_call_update` + `_meta` | 표준 ACP (기존 도구 카드) |
| 세션 잠금 | native "already open" 거부와 ACP 세션 lease를 구조화된 오류로 | 오류 표시 |
| 슬래시 명령 | 명시적 라우터 + native 확장·스킬·프롬프트 실행 | 목록·텍스트는 표준, TUI 대화창은 client action |
| 설정·인증 | 타입을 제한한 사용자 설정, 같은 AuthStorage의 키·OAuth | `_aelix/manage` 화면 연결 필요 |
| 스킬·도구·프롬프트·훅 | 조회, 활성 도구 변경, 경로 설정·리로드 | 관리 화면·파일 편집기는 Studio 작업 |
| 확장 | 설치·삭제·업데이트 승인, 명시적 로드·해제·리로드 | 관리 화면과 쓰기 가능한 실행 환경 필요 |
| MCP | ACP stdio/HTTP/SSE 서버 전달, 연결 교체·종료 | `mcpServers` 또는 관리 API |
| 프로필 | native 프로필 선택·복구, 위임 활성화 | 관리 화면 연결 필요 |

**현재 Studio의 모든 관리 화면이 완성됐다는 뜻은 아닙니다.**
클라이언트 계약은 [Studio 연동 명세](docs/studio-integration.md)에 있습니다.
임의의 TUI 위젯, SSH 호스트 관리, 팀 서버·다중 사용자 인증, 터미널 패널,
전체 브라우저·HTML 렌더러는 이 stdio 어댑터와 별도의 기능입니다.
자식별 승인·중지·steer(S3)는 aelix-ai #612 판정 뒤의 작업입니다.

## 실행

검증된 본체 체크아웃에서 `uv sync --all-packages`로 workspace 패키지를 함께 설치합니다.
그 환경에 ACP를 설치하거나 독립 환경에서 해당 Aelix 경로를 지정합니다.
본체 umbrella 패키지만 Git URL로 설치하면 이전 공개 하위 패키지가 섞일 수 있습니다.

```sh
python -m pip install /absolute/path/to/aelix-acp
aelix-acp --profile coding --aelix /absolute/path/to/aelix-core/.venv/bin/aelix
```

Windows에서는 `.venv/Scripts/aelix.exe`를 지정합니다. Python 진입점은
`--aelix /path/to/python --aelix-arg=-m --aelix-arg=aelix`처럼 반복 인자로 전달하며
셸 문자열로 결합하지 않습니다. 접두 인자로 native 세션 선택(`--session`, `--resume`,
`--continue`, `--fork`, `--no-session`)을 넘길 수 없습니다. `session/load`를 사용합니다.

```sh
aelix-acp --profile analytics --aelix /absolute/path/to/aelix \
  --analytics-extension /absolute/path/to/aelix_analytics.py

aelix-acp --extension installed_module:setup \
  --extension /absolute/path/to/trusted_extension.py \
  --extension-manager /writable/managed/environment/bin/aelix
```

`--state-dir` 기본값은 `~/.aelix/acp`이며 `AELIX_ACP_STATE_DIR`로 바꿀 수 있습니다.
이 디렉터리는 ACP ID와 native transcript의 연결, 설정, 표시 이력, 요청 결과를 보관합니다.
`session/delete`는 ACP 기록을 삭제하며 native transcript 파일을 지우지는 않습니다.
`sessions/close` 또는 `/quit`는 기록을 유지하면서 프로세스 슬롯을 반환합니다.
같은 저장 세션을 두 ACP 프로세스가 열면 OS 파일 잠금으로 두 번째를 거부합니다.
CLI 등 다른 native 프로세스가 같은 transcript를 쓰고 있으면 native가 거부하며,
어댑터는 이를 `-33001` 구조화 오류로 전달합니다. 보유자 정보는 aelix-ai의
잠금 보유자 API(Studio 요청 R7)가 생길 때까지 `null`입니다.

`--max-sessions` 기본 16, `--turn-timeout`·`--command-timeout` 기본 600초입니다.
시간 제한 0은 무제한입니다. `--enable-agents`는 새 세션의 native 위임 기본값입니다.

## 권한과 실행 환경

권한 모드는 **Ask(`default`)와 Yolo(`yolo`)** 두 가지입니다. Ask는 모든 도구 실행 전에
ACP **Allow once / Reject** 승인을 받습니다. Yolo는 client 질문만 생략하며 native guardrail,
활성 도구 제한, 다른 확장의 거부, 승인 브리지는 그대로 둡니다. native에는 Plan·Auto Edit·Auto도
있지만 headless RPC 동작이 수용되지 않아 광고하지 않습니다.

`--permission-mode default|yolo`는 새 세션의 초기 모드입니다. 저장 세션은 기록된 모드로
native를 시작하고 `permissions/get`으로 확인합니다. 기록이 없는 이전 세션은 Ask이고,
알 수 없는 모드는 재개를 거부합니다. Ask로 낮출 때는 client 승인과 저장값을 먼저 Ask로
바꾸고, Yolo는 native가 확인한 뒤에만 적용합니다. 실패해도 권한이 넓어지지 않습니다.
열기 응답 뒤에는 실제 모드를 `current_mode_update`로 다시 알립니다.

자동 확장 탐색을 끄고 선택한 확장과 승인 브리지를 명시적으로 로드합니다.
브리지는 마지막에 등록하며 실행별 nonce를 확인합니다. 승인 채널은 POSIX의 세션별
0700 임시 디렉터리/0600 Unix 소켓과, Windows의 worker별 토큰으로 인증하는 127.0.0.1 TCP입니다.
요청은 native가 먼저 알린 도구 호출 ID·이름과 일치해야 합니다.
연결 실패·잘못된 토큰·승인 거부는 실행을 차단합니다. 이는 OS 샌드박스가 아니며,
설치한 신뢰 확장의 Python 코드 자체를 격리하지 않습니다.

Windows에서는 native를 ACP 소유 gate 뒤에서 시작하고, kill-on-close Job에 연결한 뒤에만
실행합니다. MCP 등 첫 RPC 전의 시작 자식도 같은 Job을 상속합니다.

키 관리는 Aelix AuthStorage를 사용하고 API 결과·재생 이력에 키를 넣지 않습니다.
MCP의 환경변수·헤더·URL은 설정 DB에 저장하지 않습니다. 클라이언트는
`session/load` 시 자신의 `mcpServers`를 다시 전달해야 합니다.
저장된 모델이 사라졌으면 native fallback과 경고로 복구합니다. 저장된 agent 프로필을
불러올 수 없으면 도구·권한 제한이 사라지므로 재개를 거부합니다(`-32004`, `profile_unavailable`).
그 프로필을 복구하거나 새 세션을 시작합니다. 저장된 도구 제한을 적용할 native 기능이 없을 때도
넓은 기본값으로 재개하지 않고 시작을 거부합니다.

서명된 `.app` 내부에는 확장 패키지를 설치하지 않습니다. 별도의 관리 환경을
`--extension-manager`로 제공하고 사용자 승인 후 native CLI를 호출합니다.
실행 중인 bundle에서 새 패키지를 사용하려면 Studio가 쓰기 가능한 런타임과
의존성을 구성해야 합니다. `--offline`은 패키지·카탈로그 동작에 전달되며,
모델·MCP 네트워크 전체를 차단하는 방화벽 기능은 아닙니다.
어댑터 시작 설정(`--extension`, Analytics 확장)이 로드하는 확장은 세션에서 끄거나
켤 수 없습니다. 해제 뒤에는 native 목록에서 실제로 빠졌는지 확인합니다.

## 중복 전송 방지

native가 일을 받은 뒤(프롬프트, 상태를 바꾸는 명령, 확장 관리)에는 내부 오류(`-32603`)가 될 실패를
`-33002`(`turn_failed_after_dispatch`, `retryable: false`)로 응답합니다. 이미 구체적인 코드가 있는 실패
(`-32007` RPC 시간 초과(native가 일을 시작했을 수 있음), `-32000`, `-32602`, `-33001` 등)는 그 코드를 유지합니다.
AionCore는 `retryable == true`인 오류만 한 번 자동 재실행하며 이 코드들은 모두 재시도 불가로 분류하므로,
모델 호출·도구 효과를 반복하지 않습니다. native에 닿기 전의 실패는 원래 코드를 유지합니다.
어댑터 프로세스 자체가 죽거나 연결이 끊기면 어댑터가 보낼 오류가 없습니다. 2026-10-31 사용자 결정에 따라
이 경우의 재실행 방지는 host에서 고치기로 했습니다(handochan/aelix-studio-host#6, 예정). 그 수정 전까지
v0.2.2 host는 이런 턴을 한 번 다시 보냅니다. 응답을 잃은 뒤 사용자가 다시 보낸 요청은 결정에 따라 새 턴입니다.
`_meta["aelix/requestId"]`로 같은 요청을 다시 보내면 완료된 결과를 재생하고,
결과가 불확실한 요청은 다시 실행하지 않습니다(`-32006`).

자세한 경계와 복구 보장은 [설계 기록](docs/architecture.md)에 있습니다.

## Analytics 미리보기

기존 보고서 원본을 유지하고 성공한 `analytics_run`에 대해
`workspace/aion-preview/<run-id>.html`을 만듭니다. CSS와 공식 Plotly basic 3.7.0을
포함하는 스냅샷이며 bar/pie/scatter와 1MB 출력 예산을 지원합니다.
이 예산은 어댑터의 스냅샷 제한입니다. Studio의 미리보기 크기 설정·상대 자산
로드·브라우저 렌더링은 별도로 확인해야 합니다. Plotly MIT 고지는 보존합니다.

```sh
python -m aelix_acp.preview /absolute/path/to/analysis-project RUN_ID
```

## 검증

```sh
PYTHONPATH=src python -m unittest discover -s tests -v

# 고정한 native workspace(uv sync --all-packages)로 실제 프로세스 테스트까지 실행합니다.
# 로컬 합성 모델 endpoint와 임시 MCP만 사용하고 사용자 키나 인터넷 모델 호출은 쓰지 않습니다.
python scripts/test_native.py --core /absolute/path/to/aelix-ai --check-ref

# 실제 AionCore host + 이 ACP + native. 합성 모델, renderer는 실행하지 않습니다.
python scripts/smoke_host.py --host /absolute/path/to/aioncore \
  --aelix /absolute/path/to/aelix-ai/.venv/bin/aelix --report .local/host-smoke.json
```

검증 근거와 남은 수용 검증은 [제품화 검증 기록](docs/product-validation.md)에 구분합니다.
[공개 beta.2 기준선](docs/beta2/README.md)과 [기존 PoC 검증 기록](docs/local-poc.md)은
Preview 시점의 기록입니다. 공통 Preview/Beta 설계는 [개발 착수 안내](docs/studio-preview.md)를 따릅니다.
