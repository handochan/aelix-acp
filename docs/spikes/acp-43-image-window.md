# ACP #43 · 모델 요청 이미지 윈도우 판정

**ADOPT.** Studio의 고정 native에서 ACP 번들 bridge의 `context` 훅으로 일반 전송, steer, follow-up, 도구 결과와 재개 이력의 이미지 재전송을 제한할 수 있다. 요청당 **8,388,608 B·20장**을 적용하고, 실제 provider가 더 낮은 한도를 요구하면 그 한도를 적용한다. H-1은 이 판정을 독립 리뷰한 뒤 진행할 수 있다. A-1의 픽셀 활성화에는 native stdout 초과 처리와 provider별 최종 요청 검증이 필요하다. [ACP #43](https://github.com/handochan/aelix-acp/issues/43) · [Studio #94 설계 §10](https://github.com/handochan/aelix-studio/blob/865777ec4c13a031ecd7b20f6e42412bcd8da8e6/docs/studio-release-train/session-and-management-contracts.md#running-input-images)

## 시험 범위와 재현

macOS arm64 26.6.2, CPython 3.12.13에서 ACP `369df47844d0b702e45808b0de7b2e2b05dedcd5`의 세션 런처와 파일 세션을 사용했다. native는 Studio의 `83714045439478b17ab0c61248dc5c414435927a`에서 만든 wheel 네 개를 별도 환경에 설치했다. 소스 아카이브, wheel SHA-256과 설치된 패키지 파일 377개를 대조했다. [Studio pin](https://github.com/handochan/aelix-studio/blob/865777ec4c13a031ecd7b20f6e42412bcd8da8e6/scripts/studio-preview/baseline.json)

| 대상 | SHA-256 |
| --- | --- |
| native 소스 아카이브 | `f9b317f25fecad4ed488e86fc0928680ab4d59cbedb077e1e84812e73cadf400` |
| `aelix` | `ff9b0b5b6f654542eb1e505418f8057cfe1fa59176cbf9b571a559aedd58f4aa` |
| `aelix-agent-core` | `d68581737871bf9a4c0aa4a39286c12b211411e2103ec788524f5a01eba580fd` |
| `aelix-ai` | `22e1109b72acaa8c88a6585bf29c75a84c85aa97b7df46c6c46319a2b8070b21` |
| `aelix-coding-agent` | `e3e8b8af2f2cb650bf320b30893af395f22d514a6cf39e2a967304f71c81b9a6` |

[재현 스크립트](../../scripts/spike_image_window.py)는 임시 디렉터리에 번들 bridge를 복사해 마지막 `context` 핸들러를 등록한다. 일반 전송, 재개와 분기는 ACP의 실제 dispatch를 사용한다. 두 큐는 실험 드라이버에서 native RPC로 각 한 번 쓴다. Provider는 실제 native OpenAI Chat Completions 직렬화와 localhost HTTP/SSE 응답을 사용한다. PNG의 raw 길이는 유효한 ancillary chunk로 만들고 steer reducer에 JPEG도 넣는다. Rust Host, Studio renderer, 외부 provider는 후속 통합 검증 대상이다.

지정된 Studio 커밋에서 [번들 준비 스크립트](https://github.com/handochan/aelix-studio/blob/865777ec4c13a031ecd7b20f6e42412bcd8da8e6/scripts/studio-preview/prepare.py)의 `--output`으로 생성한 native 환경이나 위 wheel을 설치한 환경을 사용한다. 아래 명령은 ACP worktree에서 실행한다. 보고서에는 요청별 순서, 이미지 MIME·SHA-256·raw 길이, HTTP 본문 길이와 native RPC 줄 길이가 기록된다.

```sh
mkdir -p .local
native=/absolute/path/to/prepared/native/bin
PYTHONPATH=src "$native/python" scripts/spike_image_window.py \
  --aelix "$native/aelix" --report .local/image-window.json
```

## 요청별 관측

표는 한 실행에서 측정한 값이다. 각 요청의 raw 바이트는 loopback 수신기가 이미지별 base64를 디코드해 셌다. HTTP 본문은 UTF-8 본문 전체 길이다. base64는 이미지 발생 건마다 `4 × ceil(raw/3)`을 더하며, 동일 digest가 두 번 나타나면 두 장으로 센다. 후보는 최신부터 연속해서 유지한다. 첫 경계를 넘은 이미지와 그보다 오래된 이미지는 원래 block 위치에서 텍스트로 교체한다.

| 요청 | HTTP | 이미지 수 | Raw B | Base64 B | HTTP 본문 B | 자리표시 수 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 · 일반 | 200 | 2 | 4,194,304 | 5,592,408 | 5,607,121 | 0 |
| 2 · 일반 | 200 | 3 | 6,291,456 | 8,388,612 | 8,403,508 | 0 |
| 3 · 일반, 정확히 8 MiB | 200 | 4 | 8,388,608 | 11,184,816 | 11,199,890 | 0 |
| 4 · 도구 + steer | 200 | 7 | 7,340,803 | 9,787,744 | 9,803,478 | 1 |
| 5 · follow-up | 200 | 11 | 7,341,079 | 9,788,112 | 9,804,223 | 1 |
| 6 · close/load 후 첫 입력 | 200 | 11 | 7,341,079 | 9,788,112 | 9,804,309 | 1 |
| 7 · 일반 | 200 | 15 | 7,341,355 | 9,788,480 | 9,805,058 | 1 |
| 8 · 일반 | 200 | 19 | 7,341,631 | 9,788,848 | 9,805,807 | 1 |
| 9 · 20번째 후보 | 200 | 20 | 7,341,700 | 9,788,940 | 9,806,082 | 1 |
| 10 · 21번째 후보 | 200 | 20 | 5,244,617 | 6,992,828 | 7,010,178 | 2 |
| 11 · provider가 19장으로 제한 | 400 | 20 | 5,244,617 | 6,992,828 | 7,010,286 | 2 |
| 12 · 명시적 입력, 19장 적용 | 200 | 19 | 3,147,465 | 4,196,624 | 4,214,161 | 3 |
| 13 · provider의 1 MiB 한도 적용 | 200 | 17 | 1,737 | 2,316 | 20,004 | 5 |
| 14 · 인증 실패 대조 | 401 | 17 | 1,737 | 2,316 | 20,113 | 5 |
| 15 · 인증 복구 뒤 입력 | 200 | 17 | 1,737 | 2,316 | 20,168 | 5 |
| 16 · fork 첫 입력 | 200 | 17 | 1,737 | 2,316 | 20,254 | 5 |
| 17–18 · 수동 compact | 200 | 0 | 0 | 0 | 2,434 / 1,007 | 0 |
| 19 · compact 뒤 입력 | 200 | 0 | 0 | 0 | 14,853 | 0 |

- **원본과 순서.** `context` 핸들러가 새 메시지 목록과 영향받는 메시지·content 목록만 만든다. 반환된 목록이 `convert_to_llm`으로 전달됐다. 처음 6개 JSONL 줄은 큐 실행 후 13줄로 늘면서 앞부분이 바이트 단위로 같았다. 최종 부모와 fork는 각각 33줄, 이미지 22건·9,438,921 B였다. native `get_messages`, JSONL, close/load, fork에서 원본 MIME·digest·순서를 비교했다. 파일에는 윈도우 자리표시가 없었다. [고정 native 훅](https://github.com/handochan/aelix-ai/blob/83714045439478b17ab0c61248dc5c414435927a/packages/aelix-agent-core/src/aelix_agent_core/harness/core.py#L5438-L5463) · [모델 입력 변환](https://github.com/handochan/aelix-ai/blob/83714045439478b17ab0c61248dc5c414435927a/packages/aelix-agent-core/src/aelix_agent_core/loop.py#L320-L337)
- **reducer와 도구.** `tool_result`와 steer의 `message_end`가 각각 한 장을 교체하고 한 장을 추가했다. 변환은 이 최종 결과를 셌다. 13번째 요청은 오래된 도구 이미지도 자리표시로 바꿨고, `role:tool`, `call_image`, 앞뒤 텍스트와 더 최신 도구 이미지가 보존됐다. Chat Completions 변환기는 유지한 도구 이미지를 바로 뒤의 합성 user 메시지에 싣는다. [고정 provider 변환](https://github.com/handochan/aelix-ai/blob/83714045439478b17ab0c61248dc5c414435927a/packages/aelix-ai/src/aelix_ai/providers/openai_completions.py#L402-L465)
- **거절과 회복.** Provider를 19장으로 낮추고 bridge 정책을 20장으로 둔 요청이 실제 HTTP 400과 ACP `-33002`·`retryable:false`를 반환했다. 새 한도를 반영한 다음 명시적 입력은 같은 worker와 JSONL에서 성공했다. HTTP 재시도는 0건이고 native 큐 쓰기는 전체에서 steer 1건·follow-up 1건이었다. 별도 HTTP 401은 같은 윈도우로 재현했고, fixture 인증을 복구한 뒤 명시적으로 다시 입력했다.
- **수동 compact.** 같은 fork에서 요약 요청 두 건은 이미지가 0장이었다. `compaction_end` 뒤 활성 이력은 0장이고 이전 JSONL 줄은 유지됐다. 마지막 대화용 윈도우 표본은 그대로였으며 다음 대화 요청에서 갱신됐다. 이 관측만으로 일반 입력·도구·확장 경로의 하드캡 집행을 검증한 것으로 볼 수 없다.

## Studio 조회

시험 등록표는 H1의 `notifications: []`를 유지했다. Studio는 표준 턴 변화나 재연결 시 현재 binding으로 읽기 전용 요청을 보낸다. 아래 응답은 12번째 HTTP 요청을 만들 때의 값이다. `reasons`는 오래된 구간을 제외하게 된 첫 경계를 센다. [고정 H1 메서드 광고 및 binding 검사](https://github.com/handochan/aelix-studio-host/blob/8f55032a0c45b86193ab2207743ffb52a8df6563/crates/aionui-ai-agent/src/protocol/acp_relay.rs#L355-L395)

```json
{"jsonrpc":"2.0","id":1,"method":"_aelix/session/input/image-state","params":{"sessionId":"s","bindingId":"b","payload":{}}}
{"jsonrpc":"2.0","id":1,"result":{"status":"available","requestSequence":7,"keptCount":19,"keptRawBytes":3147465,"omittedCount":3,"reasons":{"image_count":3}}}
```

진행 중인 HTTP 응답을 보류한 상태와 턴 종료 뒤 모두 조회했다. 반복 조회는 같은 값을 반환했고 native 쓰기는 발생하지 않았다. 재개 전 binding은 `-33102`로 거부됐고 새 binding과 fork에서는 첫 모델 요청 전 `{"status":"unavailable"}`을 받았다. 표본은 대화용 `context` 투영이 발생할 때 갱신된다. 마지막 표본을 `(binding, requestSequence)`로 중복 제거하고 `omittedCount > 0`이면 **‘이전 이미지 N장은 이번 모델 요청에서 제외됨’**을 표시할 수 있다. 한 번의 폴링 사이 여러 요청이 끝나면 최근 표본 한 건을 받는다. 이 값은 원본 삭제·철회나 모델의 열람 완료를 뜻하지 않는다. A-1에서 worker 교체·모델 변경·compact 전후의 표본 유효성과 최종 payload의 상관관계를 확정한다.

## stdout 측정과 후속 카드

큐를 포함한 이미지 턴에서 `agent_end` 줄은 **4,197,895 B**, 전체 fixture에서 가장 큰 `get_messages` 응답은 **12,593,386 B**였다. 별도 동일 입력 두 번으로 경계를 재현했다. 일반 6,291,456 B + 도구 6,815,744 B에서 두 HTTP 요청은 200이고 각 요청은 8 MiB 이하였다. 측정용 32 MiB reader에서 native `agent_end` 한 줄은 **17,477,603 B**였다. ACP 기본 reader **16,777,216 B**로 실행하면 같은 native 이벤트에서 RPC가 닫히고 ACP `-33002`가 반환됐다. 바이트 수에는 개행이 포함된다. [고정 ACP reader](https://github.com/handochan/aelix-acp/blob/369df47844d0b702e45808b0de7b2e2b05dedcd5/src/aelix_acp/session.py#L665-L701)

- **H-1**은 generic byte grant·현재 binding의 역요청·RAM 사본을 구현한다. 이미지 예산·provider 판단은 ACP가 소유한다. 실제 Rust SDK의 청크 교환, Stop/permission 경합과 trace 내 픽셀 표식 0건을 확인한다. H1 광고의 빈 notifications는 유지한다.
- **A-1**은 마지막 bridge의 요청 투영과 읽기 전용 상태를 제품 경로에 연결한다. 모델별 바이트·개수·치수·토큰 및 최종 HTTP 본문을 확인하고, 실제 provider보다 낮은 보수적 한도나 검증된 상한을 사용한다. 최신 제출 전체의 수용 여부를 큐 쓰기 전에 결정한다. legacy 이미지 표현과 나중에 실행되는 payload 훅도 최종 검사에 포함한다. 3,750,000 B/장, 4장/제출, raw 6 MiB/제출, 실제 UTF-8 JSONL 8 MiB/줄은 별도로 검사한다.
- **A-1 활성화 조건**에는 실제 16 MiB 초과 이벤트, 대형 이력 응답, Stop 및 결과 불명 복구가 포함된다. 관측한 조합에서 안전한 이벤트 처리와 세션 생존을 입증할 때까지 픽셀을 보류한다. D01에서 해결할 수 없는 경로는 N-1에 bounded native 이벤트·이력 및 요청 관찰 계약을 기록한다.
- **고정 상한 대안**을 적용할 조합이 생기면 같은 파일 세션의 활성 보관량 + 미정산 예약 + 후보를 합산해 8,388,608 B·20장 이하인 경우에만 받는다. 도구·확장까지 집행할 수 없는 경우 픽셀을 차단한다. 턴 종료·Stop·재연결에서는 유지하고, compact에서는 완료 이벤트 뒤 활성 이력에서 실제 줄어든 양만 반환한다. load/fork는 복원된 분기를 다시 센다. 사용자에게 수동 compact, 경로만 보내기, 새 대화를 안내한다. 이미 거절되는 이력은 텍스트 제출만으로도 같은 오류가 날 수 있다.

[Studio #94 후속 카드와 결정](https://github.com/handochan/aelix-studio/issues/94#issuecomment-7060213770)
