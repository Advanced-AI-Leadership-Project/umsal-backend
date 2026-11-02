# 엄살 백엔드 개발 에이전트 규칙

제품 목표와 현재 범위는 [SPEC.md](SPEC.md), 실행 방법은 [README](README.md)를 따른다. [AGENT.md](AGENT.md)는 이 파일로 안내한다.
이 저장소는 엄살 서비스의 백엔드 전용이다. 프런트엔드(PWA)는 별도 저장소 `Advanced-AI-Leadership-Project/umsal-pwa`가 담당한다.

## 시작과 구조

1. 사용자의 최신 요청과 `git status --short --branch`를 확인한다. 무관한 변경을 덮어쓰지 않는다.
2. SPEC의 현재 범위와 관련 코드·테스트를 읽고 작은 단위로 작업한다. 작업은 이슈에 연결한 `codex/<이슈번호>-<작업명>` 브랜치에서 진행한다.
3. `src/umsal_backend`는 FastAPI 앱, `src/umsal_backend/lambda_handler.py`는 API Gateway → Lambda 진입점, `tests`는 앱 테스트, `tools`는 Git·CI 하네스다.
4. 현재는 `/health`만 있는 골격 단계다. 인증·동의·분석·DynamoDB 저장은 아직 구현되지 않았다. 목표 설계를 구현 완료로 보고하지 않는다.

## 환경과 계약

- Python `.python-version`, `pyproject.toml`의 uv `required-version`, `uv.lock`을 유지한다. 의존성 변경은 필요한 범위에 한정하고 `uv lock`으로 잠금 파일을 함께 갱신한다.
- 가상환경·캐시·빌드 산출물(`.venv`, `.aws-sam`, `cdk.out` 등)을 커밋하지 않는다.
- 실행 API 계약은 FastAPI의 `/openapi.json`이다. PWA와 공유하는 경로·필드를 바꾸면 PR에 호환성 영향과 PWA 측 후속 작업을 적는다.
- 앱 코드는 AWS 자격 증명을 코드·설정 파일에 넣지 않는다. Lambda 실행 역할과 환경 변수로 주입한다.
- 로컬 테스트는 AWS에 접속하지 않는다. AWS 연동 코드는 합성 이벤트·스텁·모의 객체로 검증한다.

## 민감 자료

- 실제 원음·전사·데이터셋·가중치·토큰·키·개인 `.env`를 Git·로그·오류 응답에 노출하지 않는다.
- 오류 응답과 로그에 요청 본문·인증 헤더·서명 URL을 그대로 남기지 않는다.
- 테스트와 예시에는 합성 데이터만 사용한다.

## 제품 불변조건

서비스 전체 불변조건 중 이 백엔드가 책임지는 부분이다.

- 양쪽 분석 grant, 방향별 공유 scope, 별도의 확인 preview 허용을 클라이언트가 아니라 서버에서 검사한다.
- 대상 확인 전 단서·집계를 만들거나 반환하지 않는다.
- 대상 변경·철회·삭제 후 늦게 도착한 결과를 저장하거나 다시 노출하지 않는다.
- 비동기 처리(분석 작업 등)가 추가되면 작업자는 DynamoDB에 직접 쓰지 않고 API를 거친다. 큐 메시지는 서버의 내구 commit/terminal 처분 뒤에만 삭제한다.
- 근거는 실제 원문·대상·시각에 연결하며 미관찰·보류·실패를 정상 또는 건강함으로 표시하지 않는다.

## 검사와 기록

| 변경 | 확인할 검사 |
|---|---|
| Python 코드·의존성 | `make python-check` (Ruff·포맷·pytest·coverage 80%) |
| 의존성 | 위 검사 + `make verify-security` |
| Git·CI 하네스(`tools/`, `.github/`, `.githooks/`) | `make workflow-check` 및 `Review-Scope: harness` 독립 리뷰 |
| AWS 배포·인프라 | 실제 배포 명령과 배포된 `/health` 응답. CI 통과로 대체하지 않는다 |
| 문서만 변경 | 실제 코드·명령 대조, 로컬 링크·코드 블록·`git diff --check` |

실제 영향을 확인하는 검사를 선택한다. 실행하지 못한 검사는 이유와 범위를 남긴다.
CI는 실제 AWS 자원·IAM·DynamoDB를 검증하지 않는다. 배포 확인은 별도 완료 항목이다.

## 문서와 Git

범위·구현 상태는 SPEC와 README를 함께 갱신한다.
공통 진입점은 이 AGENTS.md다. Git 운영 값은 `.github/ai-workflow.json`, 상세 절차는 `docs/ai-collaboration.md`, 검증·자동 병합은 `docs/verification-harness.md`를 따른다. AGENT.md·도구별 진입 파일에 규칙을 복제하지 않는다.
게시 전 diff·포함 파일·원격·브랜치를 확인한다. 사용자가 이미 허용한 게시 범위에서 작업하고 공개 범위 변경·force push를 임의로 하지 않는다.
완료 보고에는 실제 변경·검사·중요한 미검증 범위·원격 커밋을 구분해 적는다.

## AI 버전 관리 공통 절차

1. 시작 전에 `SPEC.md`, `docs/ai-collaboration.md`, `.github/ai-workflow.json`과 열린 이슈·PR을 읽는다. 새 clone은 `make setup`, `python3 tools/ai_workflow.py doctor`를 실행한다. 훅은 clone마다 연결한다.
2. AI가 이슈 생성·브랜치·stage·커밋·push·PR·검토 기록·조건을 충족한 병합까지 맡는다. 사용자는 기능과 완료 조건을 정한다. 이미 허용된 Git 운영에 추가 사람 승인을 임의로 요구하지 않는다.
3. 작업 이슈 하나에 브랜치 하나와 PR 하나를 연결한다. 동시 작업은 별도 clone/managed worktree로 분리한다. 보조 에이전트는 담당 파일만 수정하고 주 에이전트 한 명이 Git 변경을 확정한다.
4. 커밋과 PR 제목은 정책 파일의 Conventional Commits 형식을 따른다. 실제 계정의 commit identity를 유지하고 사람·다른 에이전트의 작성자로 위조하지 않는다. 무관한 파일을 함께 stage하지 않는다.
5. 첫 의미 있는 commit 후 Draft PR을 만든다. Ready 전 실제 검사와 별도 에이전트의 diff 검토를 완료한다. PR 템플릿의 `Reviewer:`와 실제 현재 head의 `Head-SHA:`를 기록한다. head가 바뀌면 다시 검토한다.
6. `main` 직접 push·force push·검사 우회·`reset --hard`는 하지 않는다. 게시된 브랜치에 최신 main을 반영할 때는 `chore(sync): main 변경 반영` 제목으로 merge한다. 의미가 불명확한 충돌만 사용자에게 확인한다.
7. 기본 병합은 [공통 검증 하네스](docs/verification-harness.md)가 맡는다. 담당 AI는 `make verify`와 별도 AI diff 리뷰를 마치고 현재 `Head-SHA`를 기록한 뒤 Ready로 바꾼다. CI/검증/운영 규칙을 바꾸면 독립 리뷰에 포함하고 `Review-Scope: harness`를 기록한다. 실패 댓글·아티팩트를 읽어 원인 수정→회귀 검사→push→새 head 리뷰를 반복한다. 현재 PR의 policy/quality/python/security/harness와 최신 main 통합 검증이 모두 성공하면 컨트롤러가 head SHA를 고정해 자동 squash한다. `harness:hold`는 개별 보류다. 자동화 최초 설치·main 회귀 복구 때만 설명서에 따라 `python3 tools/ai_workflow.py merge <PR번호>` plan 후 `--execute`를 사용한다. `--admin` 우회나 직접 API 병합을 사용하지 않는다.
8. 병합 후 원격 main·PR merged·브랜치 정리를 확인하고 이슈/PR/commit/실제 검증/남은 제약을 보고한다. 다른 에이전트로 넘길 때 `handoff` 출력에 검증·다음 작업을 보완해 이슈·PR에 남긴다.

GitHub 서버 보호의 실제 상태는 `docs/github-settings.md`를 따른다. Free 비공개 저장소의 요금제 제한으로 서버 보호가 적용되지 않았으며 로컬 훅·CI·병합 도구는 관리자가 우회할 수 있다. 이를 강제 서버 보호나 에이전트 신원 인증으로 표현하지 않는다. 규칙·검사 도구를 변경하는 PR도 다른 에이전트가 검토한다.

## AWS 프로젝트 연결

사용자가 확인한 값(2026-11-01, umsal-pwa와 같은 프로젝트):

| 항목 | 값 |
|---|---|
| 프로젝트 | AI+X Final |
| AWS experience | new |
| CLI profile | ai-x-final |
| selected Region | ap-southeast-2 (Sydney) |
| 대상 서비스 | API Gateway, Lambda, DynamoDB |
| help_level | HIGH. 설명·이유·개선안을 안내하며 반복해서 묻지 않는다. |

브라우저 로그인은 개인 환경에서 하며 자격 증명·SSO 캐시·개인 `.env`를 Git에 공유하지 않는다. 기본 프로파일을 임의 변경하지 않고 각 CLI 명령에 `--profile ai-x-final`을 명시한다.

new AWS experience의 제약(공식 원문: https://github.com/aws/agent-toolkit-for-aws/blob/main/rules/aws-starter-rules.md, 2026-11-01 확인). 아래와 기존 프로젝트 규칙이 충돌하면 프로젝트 규칙이 우선한다.

- 모든 리전 자원(Lambda·API Gateway·DynamoDB 등)은 selected Region `ap-southeast-2`에만 만든다. 다른 리전 생성·교차 리전 복제·다중 리전 KMS 키·StackSets·Lambda@Edge·Route 53 교차 리전 라우팅을 사용하지 않는다.
- us-east-1은 CloudFront처럼 전역 서비스가 요구하는 WAF·CloudWatch Logs에만 사용한다. 자원 점검 시 두 리전을 함께 본다.
- 용어는 account 대신 "project", IAM user 대신 "team member", 관리 화면은 "AWS Settings"(https://settings.aws.com/)라고 쓴다. 사람 권한은 AWS가 관리하므로 team member에게 역할을 임의로 부여하지 않는다. 서비스 간 권한(Lambda 실행 역할 등)은 IAM 정책으로 최소 권한을 만든다.
- 이전에 되던 작업이 갑자기 Access Denied면 spend limit 상태를 묻고 AWS Settings > Billing 확인을 안내한다. 서비스가 동작하지 않으면 `aws freetier get-account-plan-state --profile ai-x-final`로 플랜을 확인하고 지원 서비스 목록을 대조한다.
- AWS 작업 전에는 AWS 문서 도구의 `agent_skills` 검색으로 맞는 skill을 먼저 확인한다. 생성한 자원은 작업 후 유지/정리 여부를 사용자에게 묻는다.
