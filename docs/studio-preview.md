# ACP Preview 개발 착수

공통 설계의 기준 저장소는 [Aelix Studio](https://github.com/handochan/aelix-studio/blob/main/docs/studio-release-train/README.md)다.
기능·계약·출시 조건을 ACP에 별도로 복제하지 않고 해당 문서와 실제 Issue를 함께 갱신한다.

## 먼저 읽기

- [착수 순서·현재 기준점](https://github.com/handochan/aelix-studio/blob/main/docs/studio-release-train/readiness.md)
- [공개 beta.2 제약과 Ask/Yolo](https://github.com/handochan/aelix-studio/blob/main/docs/studio-release-train/beta2-public-surface.md)
- [세션·관리 계약 제안](https://github.com/handochan/aelix-studio/blob/main/docs/studio-release-train/session-and-management-contracts.md)
- [TUI 기능→기존 UI 연결](https://github.com/handochan/aelix-studio/blob/main/docs/studio-release-train/tui-coverage.md)
- [Project 9](https://github.com/users/handochan/projects/9): 정확한 ID `PVT_kwHOA-ATMs4BojoA`

## 첫 실행과 선행 작업

1. [ACP #6 공개 beta.2 wheel·ACP 호환 기준선](https://github.com/handochan/aelix-acp/issues/6):
   Codex의 첫 실행 대상이다. 준비 문서 반영 후 Ready로 전환하며 아직 구현/수용 완료가 아니다.
2. [Studio #4 기준선 통합](https://github.com/handochan/aelix-studio/issues/4): ACP #6의 exact source
   pin·wheel hashes·지원/미지원/unknown 결과를 받아 기존 후보를 검토한다. 이 선행이 끝나기 전에는 Backlog다.
3. [ACP #7 관리 helper·세션 host 계약](https://github.com/handochan/aelix-acp/issues/7):
   위 기준선 후 W01/W02를 직렬로 정한다. 전역 helper와 live session 경로를 분리하고 host probe로 확인한다.
4. [ACP #5 Ask/Yolo](https://github.com/handochan/aelix-acp/issues/5): 기준선·관리 계약이 수용된 뒤
   실제 beta.2 launch posture/ACP policy와 기존 Studio selector를 연결한다.

제품 작업 착수 때 Issue별 새 branch/worktree·현재 main SHA·기존 PR/claim·검증 계획을 기록한다.
준비용 worktree를 제품 구현 Issue의 작업 공간으로 재사용하지 않는다. 공유 manifest/wire/lock은
한 명이 직렬로 변경하고 독립 구현은 최대 두 개다. CLAUDE는 root `@AGENTS.md`를 통해 같은 기준을 읽는다.

## 변경 경계

- Preview native는 공개 beta.2 네 패키지 wheel 무수정이다. metadata의 b2와 source/배포 bytes는 구분한다.
- ACP는 protocol/capability와 native 연결을 책임진다. Studio는 설치·기존 UI·전역 helper/host 통합을 소유한다.
- 첫 Beta까지 기존 Aion UI 표면을 재사용한다. slash 전수 복제와 신규 `/clear` 구현은 요구하지 않는다.
- Ask/Yolo는 adapter 구현·실제 native/host 검증 대상이다. 관리 bridge를 통째로 끄지 않는다.
- legacy CLI와 같은 파일 writer·전체 live 관찰·공유 config 동시 mutation은 협조 계약 없이 보장하지 않는다.

## 기존 작업 보존과 검증

준비 시작 main은 `04ff18edb04148f5f490c50d1f1fb3c3475b3c5b`다. 실행 시 최신 main을 다시 확인한다.
[PR #1](https://github.com/handochan/aelix-acp/pull/1)의 `8deb482bcd53261762979cf0e6ce1302dcc821c1`와
기존 beta2 dirty 작업은 별도 후보이며 이번 준비 문서에 포함하지 않는다. 승인된 제품 Issue에서
변경을 선별·검토하고 exact-head CI를 새로 확인한다. 기존 작업을 reset/stash/덮어쓰지 않는다.

준비 검증은 문서 참조·독립 리뷰·기존 ACP unittest/CLI·CI다. 개발 후에는 clean wheel import,
실제 subprocess/host wire, Provider, 설치 artifact·OS·폐쇄망 수용을 각각 구분한다.
원본 raw 검토 로그·사용자 대화·자격 증명·실데이터는 공유 문서에 넣지 않는다.
준비 완료는 제품 기능/설치 앱 수용이나 Preview 출시 승인이 아니다.
