# 협업 방법

AI가 이슈·브랜치·커밋·PR·검증·병합을 담당한다. 팀원은 기능과 완료 조건을 설명하고 저장소의 공통 환경을 사용한다.

1. [README](README.md)의 시작하기로 각자의 clone과 로컬 환경을 준비한다.
2. [SPEC](SPEC.md), [AGENTS](AGENTS.md), [AI 공동 개발 절차](docs/ai-collaboration.md)를 읽는다.
3. 작업 이슈 하나에 `codex/<이슈번호>-<영문-설명>` 브랜치와 PR 하나를 연결한다.
4. 커밋은 `feat(scope): 설명` 등의 Conventional Commits 형식으로 작고 의미 있게 남긴다.
5. Draft PR → 관련 검사 → 현재 head의 별도 AI 검토 → Ready → 컨트롤러의 통합 검증·자동 squash merge 순서로 진행한다.
6. 원격 main 반영을 확인하고 작업 브랜치를 정리한다. 완료 보고에 PR·commit·검증·남은 제약을 적는다.

각 에이전트의 작업 폴더·자격 증명은 분리하고 코드·버전·잠금 파일·검사 절차만 공유한다.
직접 main push·force push·다른 사람 변경 덮어쓰기를 하지 않는다. 민감 자료 처리와 실제 구현 범위는 AGENTS와 SPEC를 따른다.
세부 규칙은 [AI 공동 개발 절차](docs/ai-collaboration.md), 자동 검사 값은 [.github/ai-workflow.json](.github/ai-workflow.json)을 기준으로 맞춘다.
