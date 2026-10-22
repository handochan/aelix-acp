# 로컬 AionUi + Aelix PoC 검증

검증 일시: 2026-10-21~22, Asia/Seoul. 환경: macOS Apple Silicon.

## 실행 환경

| 항목 | 실제 확인값 |
|---|---|
| AionUi 이전 버전 | 2.1.15 |
| AionUi 업데이트 | 2.2.2, `/Applications/AionUi.app` |
| Aelix 설치 버전 | 0.1.0b2, `~/.local/bin/aelix` |
| Aelix Python | uv tool 환경의 Python 3.12 |
| 모델 | 기존 Aelix 설정 `openrouter / deepseek/deepseek-v4.1-flash-1017` |
| Analytics 체크아웃 HEAD | `184910c775009dd0468837c4d89b6e42cf9f12bb` |
| 어댑터 | 이 체크아웃의 0.1.0 구현 |

공식 [GitHub 최신 릴리스](https://github.com/iOfficeAI/AionUi/releases/tag/v2.2.2),
[공식 웹사이트](https://www.aionui.com/)의 `releases/latest.yml`을 교차 확인했습니다.
설치 파일은 공식 `static.aionui.com`에서 받았습니다. DMG 체크섬 검증과
`codesign --verify --deep --strict`가 통과했고, 새 앱은 Developer ID
`AionUi Inc. (52JQX2HUSC)` 및 stapled notarization ticket을 포함합니다.

업데이트 전 앱·대화 DB·설정 백업:
저장소 루트의 `.local/backups/pre-update-2.1.15/`.
DB의 `PRAGMA integrity_check`는 `ok`였습니다.
복구 시에는 앱을 종료하고 백업 앱과 해당 버전의 DB/설정을 함께 복원해야 합니다.

## 실제 런타임 검증

| 검증 | Coding | Analytics |
|---|---|---|
| 기존 Aelix 프로세스로 ACP 초기화·세션 생성 | 통과 | 통과 |
| 실제 모델 텍스트 스트리밍 | 통과 | 통과 |
| 승인 후 HTML 파일 쓰기 | 통과 | 통과 |
| 거부한 파일이 생성되지 않음 | 통과 | 통과 |
| 어댑터 프로세스 재시작 후 session/load | 통과 | 통과 |
| 저장된 대화 이력 재생 및 표식 회상 | 통과 | 통과 |
| Analytics 프로젝트 초기화·상태 조회 | 해당 없음 | 통과 |
| analytics_run 실제 계산·차트·코드 기록 | 해당 없음 | 통과 |

첫 Analytics 연결 검증은 프로젝트가 없는 폴더에서 `analytics_status`의
`no_project` 오류를 확인했습니다. 그 응답을 분석 성공으로 판정하지 않고,
별도의 합성 프로젝트를 생성해 아래 실행까지 확인했습니다.

Analytics 합성 실행: `20261022-001457-poc-mean`, status=`ok`, 4.119초.
`[10,20,30]`의 평균을 실제 계산해 stdout에 `mean: 20`을 출력했습니다.
Plotly 차트 1개와 3행 Parquet 차트 데이터, 코드 스냅샷과 SHA-256,
Aelix 세션 경로가 `runs/<run-id>/run.json`에 기록되었습니다.
입력이 코드 안의 합성 값이므로 원본 입력 파일은 없으며, 실제 업무 데이터
검증이나 현장 수용 테스트를 의미하지 않습니다.

전체 증거:

- `.local/coding-transcript.json`
- `.local/analytics-transcript.json`
- `.local/analytics-workspace/recorded-analysis/runs/20261022-001457-poc-mean/run.json`
- `.local/analytics-workspace/recorded-analysis/workspace/runs/20261022-001457-poc-mean.html`

## AionUi 앱에서 확인한 결과

- Custom Agent **Aelix Coding**, **Aelix Analytics** 등록 및 각 연결 테스트 성공
- 새 채팅의 **더보기**에서 두 항목 선택 가능
- Coding 대화 `04d7f73d`: 실제 도구 승인 카드, `ui-coding.html` 생성,
  응답 `CODING_UI_OK`, AionUi 미리보기에서 제목과 한글 문장 렌더링 확인
- Analytics 대화 `ee23a64c`: `analytics_init`, `analytics_run` 승인 후 실행 성공,
  `ANALYTICS_UI_OK` 응답 확인. 실제 실행 ID `20261022-002105-01-synthetic-mean`,
  3.43초, 평균 `20.0`, 차트 데이터 3행, 코드 SHA-256 prefix `e59f7d918c90`.
- AionUi HTML 미리보기에서 원본 보고서의 상대 CSS/JS가 로드되지 않는 문제와
  기본 1MiB 설정에서 큰 HTML 파일이 차단됨을 실제 확인했습니다.
  이후 v2.2.2 설치 번들에서 `preview.textSizeLimitMb`가 기본 1, 최소 1,
  최대 100이며 1024×1024 바이트 단위를 사용함을 확인했습니다.
  설정 → 시스템 → 미리보기 크기 제한(MB)에서 조절할 수 있고 새 탭에 적용됩니다.
  앞서 고정 1MB 제한으로 기술한 것은 부정확했습니다. 별도 미리보기 스냅샷으로 보완하여
  서식·Plotly 막대 10/20/30·평균 20·코드 기록이 앱 안에서 보임을 확인했습니다.
  파일은 `.local/aion-analytics-project/workspace/aion-preview/20261022-002105-01-synthetic-mean.html`.
  CSS와 gzip+base64 Plotly basic을 포함하며 외부 네트워크 호출이 없습니다.
  원본 `workspace/runs` 페이지와 `run.json`은 수정하지 않았습니다.
- AionUi의 **에이전트 다시 연결** 실행 뒤 기존 ACP ID
  `63e6c50a-f5e7-40ee-bdd6-419ce596e105`로 `session/load` 요청·응답이 기록됐습니다.
  새 프롬프트에 표식을 다시 전달하지 않고 `AION-ANALYTICS-731` 응답을 받아
  앱 경유 세션 복구를 확인했습니다.
- 최종 `scripts/smoke.py --profile analytics` 재실행에서 실제 `analytics_run`
  성공 후 미리보기 파일과 대화 링크의 자동 생성까지 통과했습니다.
  최신 전체 증거는 `.local/analytics-transcript.json`에 있습니다.

## 자동 회귀 검증

`python -m unittest discover -s tests -v`: **15개 통과**.
승인 전 대기, 승인 대기 중 취소 거부, 도구 카드 중복 방지, 자식 stdout 종료,
프로필 교차 복구 거부, 미지원 입력 거부, 모델 실행값 표시, 승인 우회 모드
거부, 실제 OS 프로세스의 SIGTERM 종료와 자식 정리, 브리지 누락 시 시작 거부,
자산 내장과 프로젝트 밖 경로·원격 자산 거부를 확인합니다.
실제 AionUi 중단 버튼이나 모든 OS의 프로세스 트리 정리를
검증한 결과로 확대하지 않습니다.

## 남은 범위

동적 모델 변경, AionUi MCP 전달, 스킬/프롬프트/확장 관리 UI, 이미지 입력,
Windows/Linux 검증은 남아 있습니다. Aelix 본체와 기존 Analytics 소스는
수정하지 않았습니다. `uv build`로 sdist와 wheel 생성이 통과했고 프로필·브리지·
Plotly 자산·라이선스가 wheel에 포함됨을 확인했습니다. 새 컴퓨터 설치,
PR/CI 및 외부 배포 검증은 없습니다.

미리보기는 [Plotly 공식 partial bundle](https://github.com/plotly/plotly.js/blob/main/dist/README.md)을
사용합니다. 공식 CDN의 `plotly-basic-3.7.0.min.js`를 gzip으로 포함했고,
원본 SHA-256은 `c23b03591a6bdad0bd0f47a0c5c52305a5519812b73ce254be65cd841553c9c2`입니다.
지원 trace는 bar/pie/scatter이며 런타임에 자산 무결성을 확인합니다.
