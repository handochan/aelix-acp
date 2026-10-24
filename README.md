# aelix-acp

Aelix Studio와 다른 ACP 클라이언트를 실제 Aelix 런타임에 연결하는
Python 3.11+ 어댑터입니다. `0.2.0a1`은 PoC 이후의 **개발 후보**입니다.
ACP 패키지는 표준 라이브러리만 사용하며, Aelix는 별도 프로세스로 실행합니다.

기본 실행은 **공개 PyPI `aelix==0.1.0b2` 기반 프리뷰**입니다.
새 runtime-control v1 기능은 `--runtime-profile control-v1`을 명시하고
[`runtime-reference.json`](runtime-reference.json)의 native 후보를 함께 설치한 경우에만 사용합니다.
main의 패키지 버전 문자열도 아직 `0.1.0b2`이므로 버전 문자열만으로 지원 기능을 판단하지 않습니다.
beta.3 후보 검증과 beta.3 배포 완료는 다릅니다. 기존 Studio 설치에 자동 적용되지 않습니다.

## 구현 범위

| 기능 | 기본 beta.2 프리뷰 | 명시적 control-v1 후보 |
|---|---|---|
| 생성·목록·재개·종료·삭제 | 지원, SQLite 이력·설정·요청 기록 | 지원, 비파괴 분기 추가 |
| 모델 | 공개 확장 API로 인증된 목록 조회·실시간 전환 | native RPC로 목록 조회·실시간 전환 |
| 추론·steering·follow-up·자동 압축·재시도 | 기존 RPC로 설정·복원 | 지원 |
| 권한 | 모든 도구에 명시적 승인, 거절·취소 시 차단 | native가 제공하는 추가 권한 모드 |
| 텍스트·추론·도구·이미지·리소스 입력 | 표준 ACP 스트리밍·실패·취소·파일 위치 | 지원 |
| 슬래시 명령 | 조회·설정·관리 및 선택된 스킬·프롬프트 실행 | 임의 native 확장 명령·대화상자 추가 |
| 도구·확장·스킬·프롬프트 | 도구 live 선택, 확장/리소스는 승인 후 재시작 | native inventory·reload 제어 |
| 관리 설정·인증 | 세션 설정 지원, 인증은 기존 native 환경에서 구성 | `_aelix/manage`, 키·OAuth·설정 관리 |
| 클라이언트 MCP 서버 전달 | 미지원, 명시적 오류 | stdio/HTTP/SSE 및 연결 교체 |
| native 프로필·위임 | 기존 형식의 생성·조회·재시작 적용, 위임 미지원 | 선택·복구·위임·진행 이벤트 |

공개 beta.2 CLI는 RPC에 모델 registry를 연결하지 않아 `get_available_models`와
`set_model`을 사용할 수 없습니다. 함수가 소스에 존재하는 것과 배포 CLI에서 실행 가능한 것은 다릅니다.
어댑터의 세션별 인증 채널이 공개 ExtensionAPI의 실제 registry/set_model을 연결합니다.
본체 수정이나 별도 credential store 없이 표준 모델 선택기를 제공합니다.

**현재 Studio의 모든 관리 화면이 완성됐다는 뜻은 아닙니다.**
클라이언트 계약은 [Studio 연동 명세](docs/studio-integration.md)에 있습니다.
임의의 TUI 위젯, SSH 호스트 관리, 팀 서버·다중 사용자 인증, 터미널 패널,
전체 브라우저·HTML 렌더러는 이 stdio 어댑터와 별도의 기능입니다.

## 실행

프리뷰는 공개 beta.2 wheel을 사용합니다. Python 3.11+ 환경에서 설치합니다.

```sh
python -m pip install 'aelix==0.1.0b2' /absolute/path/to/aelix-acp
aelix-acp --profile coding --aelix /absolute/path/to/environment/bin/aelix
```

control-v1 후보는 고정된 본체 체크아웃에서 `uv sync --all-packages` 후 실행합니다.
본체 umbrella만 Git URL로 설치하여 이전 공개 하위 패키지와 섞지 않습니다.

```sh
aelix-acp --runtime-profile control-v1 --aelix /absolute/path/to/aelix-core/.venv/bin/aelix
```

Windows에서는 `.venv/Scripts/aelix.exe`를 지정합니다. Python 진입점은
`--aelix /path/to/python --aelix-arg=-m --aelix-arg=aelix`처럼 전달합니다.
접두 인자를 셸 문자열로 결합하지 않습니다.

```sh
aelix-acp --profile analytics --aelix /absolute/path/to/aelix \
  --analytics-extension /absolute/path/to/aelix_analytics.py

aelix-acp --runtime-profile control-v1 --extension installed_module:setup \
  --extension /absolute/path/to/trusted_extension.py \
  --extension-manager /writable/managed/environment/bin/aelix
```

`--state-dir` 기본값은 `~/.aelix/acp`이며 `AELIX_ACP_STATE_DIR`로 바꿀 수 있습니다.
이 디렉터리는 ACP ID와 native transcript의 연결, 설정, 표시 이력, 요청 결과를 보관합니다.
`session/delete`는 ACP 기록을 삭제하며 native transcript 파일을 지우지는 않습니다.
`sessions/close` 또는 `/quit`는 기록을 유지하면서 프로세스 슬롯을 반환합니다.
같은 저장 세션을 동시에 열면 OS 파일 잠금으로 두 번째 프로세스를 거부합니다.

`--max-sessions` 기본 16, `--turn-timeout`·`--command-timeout` 기본 600초입니다.
시간 제한 0은 무제한입니다. `--runtime-profile` 기본값은 `beta2`입니다.
이전 `--allow-legacy-runtime`은 호환 인자로 남지만 이제 필요하지 않습니다.
프리뷰에서도 관리 불가능한 MCP·명령·권한을 실행되는 것처럼 표시하지 않습니다.

## 권한과 실행 환경

beta.2 프리뷰는 `default`만 제공하며 모든 도구가 명시적 승인을 받습니다.
control-v1 후보는 native가 실제 제공하는 `default`, `plan`, `auto-accept-edits`, `auto`, `yolo`를 노출합니다.
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
저장된 모델이 사라졌으면 native fallback과 경고로 복구합니다.
beta.2의 선택 프로필 파일이 바뀌거나 사라지면 제한을 버리지 않고 재개를 거부합니다.
control-v1의 프로필 복구 실패 시에는 Plan을 유지하거나 명시적 승인 모드로 낮춥니다.

서명된 `.app` 내부에는 확장 패키지를 설치하지 않습니다. 별도의 관리 환경을
`--extension-manager`로 제공하고 사용자 승인 후 native CLI를 호출합니다.
실행 중인 bundle에서 새 패키지를 사용하려면 Studio가 쓰기 가능한 런타임과
의존성을 구성해야 합니다. `--offline`은 패키지·카탈로그 동작에 전달되며,
모델·MCP 네트워크 전체를 차단하는 방화벽 기능은 아닙니다.

자세한 경계와 복구 보장은 [설계 기록](docs/architecture.md)에 있습니다.
프리뷰의 실제 배포본 제한과 검증은 [beta.2 프리뷰](docs/beta2-preview.md),
호스트에서 필요한 변경은 [Studio 최소 변경](docs/studio-minimum-changes.md)을 참고합니다.
TUI 전체 기능의 가능 범위와 ACP·Studio·본체 담당은
[beta.2 기능·담당 표](docs/beta2-feature-matrix.md)에 정리했습니다.

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

# 공개 beta.2 wheel만 설치한 별도 환경. 소스·editable native 설치는 검증에서 거부합니다.
python scripts/test_beta2.py --python /absolute/path/to/beta2-environment/bin/python

# 실제 Aelix 프로세스 + 임시 MCP + 로컬 합성 모델 endpoint.
# 기존 사용자 키나 인터넷 모델 호출을 사용하지 않습니다.
AELIX_ACP_TEST_AELIX=/absolute/path/to/aelix-core/.venv/bin/aelix \
  PYTHONPATH=src python -m unittest discover -s tests -v
```

검증 근거와 남은 수용 검증은 [제품화 검증 기록](docs/product-validation.md)에 구분합니다.
[실제 라이브 검증](docs/live-validation.md)에는 공개 beta.2와 설치 wheel의
Anthropic 16개 검증, 기존 Studio 화면의 모델 전환·승인·거절·중지·이력 복구를 기록했습니다.
[기존 PoC 검증 기록](docs/local-poc.md)은 과거 설치 상태에 관한 기록입니다.
