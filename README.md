# aelix-acp

Aelix Studio와 다른 ACP 클라이언트를 실제 Aelix 런타임에 연결하는
Python 3.11+ 어댑터입니다. `0.2.0a1`은 PoC 이후의 **개발 후보**입니다.
ACP 패키지는 표준 라이브러리만 사용하며, Aelix는 별도 프로세스로 실행합니다.

이 버전은 Aelix의 **runtime-control v1**을 필요로 합니다.
기존 PyPI `aelix==0.1.0b2`에는 이 API가 없습니다. 검증한 본체 소스는
[`runtime-reference.json`](runtime-reference.json)에 고정합니다.
후보 브랜치의 본체와 ACP를 함께 설치해야 하며, 기존 Studio 설치에 자동 적용되지 않습니다.

## 구현 범위

| 기능 | 어댑터·본체 구현 | Studio 연결 |
|---|---|---|
| 세션 생성·목록·복구·분기·종료 | native session + SQLite 이력·설정·요청 기록 | 표준 ACP, 분기 전환은 client action |
| 모델·추론·권한 변경 | native setter, 모델별 추론 수준, 실행 전 승인 | `configOptions`, legacy modes/models |
| 텍스트·추론·도구·이미지 입력 | 스트리밍, 실패·취소, 파일 위치 | 표준 ACP |
| 슬래시 명령 | 명시적 라우터 + native 확장·스킬·프롬프트 실행 | 목록·텍스트는 표준, TUI 대화창은 client action |
| 설정·인증 | 타입을 제한한 사용자 설정, 같은 AuthStorage의 키·OAuth | `_aelix/manage` 화면 연결 필요 |
| 스킬·도구·프롬프트·훅 | 조회, 활성 도구 변경, 경로 설정·리로드 | 관리 화면·파일 편집기는 Studio 작업 |
| 확장 | 설치·삭제·업데이트 승인, 명시적 로드·해제·리로드 | 관리 화면과 쓰기 가능한 실행 환경 필요 |
| MCP | ACP stdio/HTTP/SSE 서버 전달, 연결 교체·종료 | `mcpServers` 또는 관리 API |
| 프로필·서브에이전트 | native 프로필 선택·복구, 위임 활성화·진행 이벤트 | 전용 진행 화면 연결 필요 |

**현재 Studio의 모든 관리 화면이 완성됐다는 뜻은 아닙니다.**
클라이언트 계약은 [Studio 연동 명세](docs/studio-integration.md)에 있습니다.
임의의 TUI 위젯, SSH 호스트 관리, 팀 서버·다중 사용자 인증, 터미널 패널,
전체 브라우저·HTML 렌더러는 이 stdio 어댑터와 별도의 기능입니다.

## 실행

검증된 본체 체크아웃에서 `uv sync --all-packages`로 workspace 패키지를 함께 설치합니다.
그 환경에 ACP를 설치하거나 독립 환경에서 해당 Aelix 경로를 지정합니다.
본체 umbrella 패키지만 Git URL로 설치하면 이전 공개 하위 패키지가 섞일 수 있습니다.

```sh
python -m pip install /absolute/path/to/aelix-acp
aelix-acp --profile coding --aelix /absolute/path/to/aelix-core/.venv/bin/aelix
```

Windows에서는 `.venv/Scripts/aelix.exe`를 지정합니다. Python 진입점은
`--aelix /path/to/python --aelix-arg=-m --aelix-arg=aelix`처럼 전달합니다.
접두 인자를 셸 문자열로 결합하지 않습니다.

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
같은 저장 세션을 동시에 열면 OS 파일 잠금으로 두 번째 프로세스를 거부합니다.

`--max-sessions` 기본 16, `--turn-timeout`·`--command-timeout` 기본 600초입니다.
시간 제한 0은 무제한입니다. `--allow-legacy-runtime`은 명시적인 축소 호환 모드이며,
일반 배포에는 runtime-control v1을 포함해야 합니다.

## 권한과 실행 환경

권한 모드는 `default`, `plan`, `auto-accept-edits`, `auto`, `yolo`입니다.
Plan은 출처가 확인된 내장 읽기 도구만 허용하고, 편집 자동 승인은
native 정책이 허용한 프로젝트 내부의 비민감 파일 쓰기·편집만 허용합니다.
프로젝트 밖·민감 경로는 승인을 요청합니다. 임의 확장이 도구 이름을 `read`로
정해도 읽기 권한을 얻지 못합니다. Auto의 shell 판단은 native 분류기를 사용합니다.

자동 확장 탐색을 끄고 선택한 확장과 승인 브리지를 명시적으로 로드합니다.
브리지는 마지막에 등록하며 실행별 nonce와 인증된 loopback 채널을 확인합니다.
연결 실패·잘못된 토큰·승인 거부는 실행을 차단합니다. 이는 OS 샌드박스가 아니며,
설치한 신뢰 확장의 Python 코드 자체를 격리하지 않습니다.

키 관리는 Aelix AuthStorage를 사용하고 API 결과·재생 이력에 키를 넣지 않습니다.
MCP의 환경변수·헤더·URL은 설정 DB에 저장하지 않습니다. 클라이언트는
`session/load` 시 자신의 `mcpServers`를 다시 전달해야 합니다.
저장된 모델·프로필이 사라졌으면 native fallback과 경고로 복구합니다.
프로필 복구가 실패하면 Plan을 유지하거나 명시적 승인 모드로 낮춥니다.

서명된 `.app` 내부에는 확장 패키지를 설치하지 않습니다. 별도의 관리 환경을
`--extension-manager`로 제공하고 사용자 승인 후 native CLI를 호출합니다.
실행 중인 bundle에서 새 패키지를 사용하려면 Studio가 쓰기 가능한 런타임과
의존성을 구성해야 합니다. `--offline`은 패키지·카탈로그 동작에 전달되며,
모델·MCP 네트워크 전체를 차단하는 방화벽 기능은 아닙니다.

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

# 실제 Aelix 프로세스 + 임시 MCP + 로컬 합성 모델 endpoint.
# 기존 사용자 키나 인터넷 모델 호출을 사용하지 않습니다.
AELIX_ACP_TEST_AELIX=/absolute/path/to/aelix-core/.venv/bin/aelix \
  PYTHONPATH=src python -m unittest discover -s tests -v
```

검증 근거와 남은 수용 검증은 [제품화 검증 기록](docs/product-validation.md)에 구분합니다.
[기존 PoC 검증 기록](docs/local-poc.md)은 과거 설치 상태에 관한 기록입니다.
