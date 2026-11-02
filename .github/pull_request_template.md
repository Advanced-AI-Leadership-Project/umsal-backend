Closes #이슈번호

## 변경 내용

문제와 달라진 동작, 관련 SPEC/FR/WF를 적는다.

## 검증

실제 실행한 명령·환경·결과를 적는다. 미실행 검사는 이유와 함께 남은 사항에 적는다.

## AI 검토

Reviewer: 실제 별도 검토 에이전트 이름 또는 세션 ID
Head-SHA: 검토한 PR head의 40자리 commit SHA
결과: 검토 범위, 발견 사항과 수정 결과, 미해결 결함 여부

검증·운영 규칙 변경 시 별도 검토 후 `Review-Scope: harness`를 위 검토란에 기록한다. 해당하지 않으면 이 안내를 삭제한다.

## 남은 사항

미구현·미검증·배포 여부와 제약을 적는다. 없으면 없음이라고 적는다.

Ready 전 [공통 검증 루프](https://github.com/Advanced-AI-Leadership-Project/umsal-backend/blob/main/docs/verification-harness.md)를 따른다. 실패는 담당 AI가 원인을 수정하고 새 head를 검토한다. Ready 이후 최신 main 통합 검사와 자동 병합은 컨트롤러가 진행한다. 일시 보류는 `harness:hold` 라벨을 사용한다.
