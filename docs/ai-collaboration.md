# AI 에이전트 공동 개발과 GitHub 운영

이 저장소를 모든 에이전트의 공통 기준으로 사용한다. 사용자는 만들 기능과 완료 조건을 정하고, AI가 이슈·브랜치·커밋·PR·검증·병합을 진행한다.
제품 기준은 [SPEC](../SPEC.md), 에이전트의 공통 작업 지침은 [AGENTS](../AGENTS.md), 자동 검사 값은 [.github/ai-workflow.json](../.github/ai-workflow.json)에 둔다.

## 1. 공유하는 것과 각자 준비하는 것

| 함께 공유 | 에이전트마다 분리 |
|---|---|
| GitHub 저장소·main·이슈·PR·작업 기록 | 별도 clone 또는 worktree와 작업 브랜치 |
| Python/uv 버전·`uv.lock`·검사 명령 | 개인 `.env`·GitHub 인증·AWS 자격 증명(SSO 캐시) |
| SPEC·AGENTS·PR/이슈 템플릿 | 로컬 서버 포트·로컬 DynamoDB·테스트 데이터 |

한 폴더에서 여러 에이전트가 동시에 checkout·stage·commit하면 서로의 변경이 섞인다. 동시 작업은 각자 별도 clone 또는 worktree에서 진행한다. 같은 기능에 참여하는 보조 에이전트는 담당 파일을 나누고, 커밋·푸시는 해당 작업의 담당 에이전트 한 명이 한다.
각 에이전트는 자신의 인증으로 GitHub에 접근한다. 같은 저장소에 branch를 push하는 계정에는 Write 이상 권한이 필요하다. Read 권한은 clone·조회용이며 이 절차의 push·병합 권한을 제공하지 않는다. 계정의 실제 commit identity를 사용하고 사람이 작성한 것처럼 작성자를 위조하지 않는다. 토큰이나 로그인 정보를 저장소에 공유하지 않는다.

같은 PC에서 여러 에이전트가 로컬 서버를 띄우면 기본 포트 8000이 충돌할 수 있다. 각자 `--port`를 다르게 지정한다. AWS 자원(Lambda·API Gateway·DynamoDB)은 팀이 공유하는 실제 환경이므로, 배포·삭제·테이블 변경은 이슈에 범위를 적은 담당 에이전트만 수행한다. 제품 API 연결은 각 환경의 실제 `/health` 응답으로 확인한다.

## 2. 작업 하나는 이슈·브랜치·PR 하나

1. 최신 `origin/main`과 관련 SPEC·열린 이슈·PR을 확인한다.
2. 구현 전 이슈에 목적·범위·완료 조건·담당 에이전트·예상 수정 영역을 적는다. 이미 같은 작업 이슈가 있으면 이어서 사용한다.
3. `codex/<이슈번호>-<짧은-영문-설명>` 브랜치를 만든다. 예: `codex/12-consent-flow`. 접두사는 사용하는 AI 제품과 관계없이 동일하다.
4. 관련 파일만 수정하고 작은 논리 단위로 커밋한다. 담당 범위를 바꿔야 하면 이슈에 이유와 새 범위를 남긴다.
5. 첫 의미 있는 커밋을 푸시한 뒤 Draft PR을 열어 진행 중인 영역을 공유한다.
6. 변경에 맞는 검증과 별도 AI 검토를 마친 뒤 Ready로 전환한다.
7. [공통 검증 하네스](verification-harness.md)가 현재 head의 검사·리뷰와 최신 main 통합 검증을 확인하고 자동 squash merge한다. 원격 main 재검증까지 확인한다.

장기 `develop` 브랜치는 만들지 않는다. `main`은 통합 기준으로 유지하고 작업 브랜치는 하나의 완료 조건을 해결할 정도로 짧게 운영한다. 긴 기능은 API 계약·동의 화면·서버 검증처럼 검증 가능한 이슈로 나눈다.

## 3. 시작·커밋·동기화

시작할 때 작업 트리가 깨끗한지와 원격 저장소가 맞는지 먼저 확인한다. 다른 사람의 미커밋 변경이 있으면 덮어쓰거나 함께 커밋하지 않는다. 자신의 작업을 보존한 뒤 별도 clone/worktree에서 계속할 수 있다.

```bash
git status --short --branch
git remote -v
git fetch origin main
```

[GitHub CLI](https://cli.github.com/)와 Git·uv를 팀 온보딩 버전으로 준비한 뒤 저장소 루트에서 공통 도구를 사용한다. `doctor`는 환경·원격·인증 사용 가능 여부를 확인하며, 인증 정보 자체를 출력하지 않는다. `hooks`는 이 clone의 Git hook을 연결하고 기존 다른 hook 설정을 임의로 덮어쓰지 않는다.

```bash
python3 tools/ai_workflow.py doctor
python3 tools/ai_workflow.py hooks
```

다음은 이슈 `#12`를 예로 든 시작 명령이다. 실제 작업의 이슈번호와 짧은 영문 설명을 사용한다. 시작 도구는 깨끗한 작업 트리와 최신 main을 확인한 뒤 브랜치를 만든다.

```bash
python3 tools/ai_workflow.py start 12 consent-flow
```

환경 준비와 hook 연결은 `make ai-setup`으로도 실행한다. clone마다 한 번 연결해야 하며 GitHub에 hook 파일이 존재하는 것만으로 로컬 hook이 자동 활성화되지는 않는다.

브랜치는 확인한 최신 `origin/main`에서 시작한다. 이미 다른 사람이 해당 이슈 브랜치를 사용 중이면 새 담당자가 별도 브랜치를 임의로 덮어쓰지 않고 이슈·PR의 인수인계 기록을 확인한다.

커밋 제목은 다음 형식을 사용한다. 설명은 한국어 또는 영어이며 제목 전체는 100자 이하다.

```text
<type>(<scope>): <무엇이 바뀌는지>
```

scope는 생략할 수 있다. 호환성이 깨지는 변경은 `type(scope)!:` 형태와 본문의 `BREAKING CHANGE:` 설명을 함께 남긴다.

| type | 쓰는 경우 | 예시 |
|---|---|---|
| feat | 기능 추가 | `feat(consent): 양쪽 동의 상태 조회 추가` |
| fix | 잘못된 동작 수정 | `fix(api): 철회 후 결과 노출 차단` |
| docs | 문서 변경 | `docs(team): AI 인수인계 절차 설명` |
| refactor | 동작 유지 구조 개선 | `refactor(api): 권한 검사 함수 분리` |
| test | 검증 코드 변경 | `test(consent): 철회 후 접근 차단 검사` |
| chore | 기타 유지 작업 | `chore(sync): main 변경 반영` |
| ci | GitHub Actions 변경 | `ci: PR 정책 검사 추가` |
| build | 빌드·의존성 변경 | `build(deps): uv 잠금 파일 갱신` |
| perf | 성능 개선 | `perf(api): DynamoDB 중복 조회 줄이기` |
| revert | 이전 변경 취소 | `revert: 동의 조회 변경 되돌리기` |

`수정`, `완료`, `WIP`만 쓰는 커밋은 피한다. 구현·그 동작의 테스트·직접 필요한 문서는 한 커밋에 담을 수 있다. 서로 관계없는 기능은 나눈다. 커밋 전 `git diff`와 staged diff를 확인하고 필요한 경로만 stage한다.

필요한 경로만 stage한 뒤 커밋 전 공통 검사를 실행한다. `check`는 staged 변경을 검사한다. 기능별 테스트를 대신하는 명령은 아니다.

```bash
python3 tools/ai_workflow.py check
git diff --cached --check
git commit -m "feat(consent): 양쪽 동의 상태 조회 추가"
git push --set-upstream origin codex/12-consent-flow
```

push 대상은 `HEAD`가 아니라 브랜치 이름으로 적는다. pre-push 훅은 `refs/heads/<브랜치>`가 아닌 로컬 ref를 우회 시도로 보고 차단한다.

공유된 작업 브랜치의 이력을 재작성하거나 `push --force`하지 않는다. main을 반영해야 할 때는 fetch 후 명시적인 동기화 커밋으로 합친다. 충돌은 양쪽 의도를 읽고 해결한 뒤 영향받은 검사를 다시 한다. 해결 과정에서 제품 범위나 동의·삭제 의미가 불명확하면 해당 판단만 사용자에게 확인하고 독립 작업은 계속한다.

```bash
git fetch origin main
git merge origin/main -m "chore(sync): main 변경 반영"
```

`reset --hard`, 다른 사람 변경 삭제, 실패를 숨기는 검사 제거, 본인 작업과 무관한 파일의 일괄 stage를 하지 않는다.

## 4. PR 작성과 AI 검토

PR 제목도 같은 커밋 형식을 사용한다. 병합 시 PR 제목을 squash commit 제목으로 사용한다. 본문은 저장소 [PR 템플릿](../.github/pull_request_template.md)을 따른다.

- `Closes #이슈번호`를 넣어 브랜치의 이슈와 연결한다.
- **변경 내용**: 실제 달라진 동작과 관련 SPEC/FR/WF를 설명한다.
- **검증**: 실제 명령·결과·수행 환경을 적는다. 실패와 미실행을 성공으로 적지 않는다.
- **AI 검토**: 구현 담당과 별도의 에이전트가 diff를 검토하고 담당자·검토한 commit SHA·발견 사항과 해결 결과를 적는다.
- **남은 사항**: 이번 범위 밖의 구현·검증·제약을 적는다. 없으면 `없음`이라고 명시한다.

PR 본문은 로컬 파일에 작성한 뒤 `--body-file`로 전달한다. 줄바꿈과 사용자 입력을 shell 코드로 해석하지 않도록 PR 본문을 긴 shell 문자열에 끼워 넣지 않는다. 다음은 `#12` 예시이며 파일을 실제 내용으로 작성한 뒤 실행한다.

```bash
gh pr create --draft --base main --head codex/12-consent-flow \
  --title "feat(consent): 양쪽 동의 상태 조회 추가" \
  --body-file .local/pr-body.md
```

AI 검토는 변경된 동작·실패 경로·동의/권한·기존 계약·민감 자료·검사 증거를 본다. 사람이 다른 AI 도구를 사용해 검토해도 같은 양식으로 남긴다. 다른 모델 사용 자체가 별도 검토의 증거는 아니며 실제 diff를 읽은 검토 결과가 필요하다.

검토 기록의 기본 형식은 다음과 같다. 예시 값을 그대로 제출하지 않는다.

```text
Reviewer: 실제 검토 에이전트 이름 또는 세션 ID
Head-SHA: 검토한 PR head의 40자리 commit SHA
결과: 발견 사항과 수정 여부, 또는 검토 범위와 미해결 사항 없음
```

검토 뒤 코드나 설정이 바뀌면 새 head를 다시 검토하고 SHA·검증 기록을 갱신한다. 병합 전 Ready PR에는 미완료 체크박스를 남기지 않는다. 완료할 수 없는 필수 검증이나 미해결 결함이 있으면 Draft로 두고 이유를 적는다.

같은 GitHub 계정으로 움직이는 AI끼리는 GitHub의 작성자 자기 승인 제한이 있으므로 사람 1명의 승인 수를 필수 조건으로 두지 않는다. 별도 에이전트 검토 기록과 CI 통과를 사용한다. 검토 기록은 행위의 증거이며 에이전트 신원을 플랫폼이 보증하는 인증서가 아니다.

## 5. 병합과 완료 보고

병합 담당 AI는 최신 원격 상태를 확인하고 다음 조건을 모두 만족할 때 squash merge한다.

- PR이 main을 대상으로 하고 현재 작업 이슈와 연결되어 있다.
- 현재 head에 필요한 CI가 성공했고 필수 작업이 완료됐다.
- 현재 head의 별도 AI 검토가 기록되어 있으며 미해결 결함·충돌·검토 대화가 없다.
- main에 앞선 병합이 있으면 컨트롤러가 새 main과 현재 head의 실제 통합 검증을 마쳤다. 충돌은 담당 AI가 수정하고 새 head를 리뷰한다.
- 원음·전사·개인 `.env`·키·가중치·빌드 산출물이 포함되지 않았다.

필수 검사 이름은 정책 파일에서 확인한다. 현재 `policy`·`quality`·`python`·`security`·`harness`다. 검사가 없거나 진행 중이면 성공으로 해석하지 않는다. 서버 보호 미적용 환경에서 `--required`만으로 판단하지 않는다. 기본 경로는 Ready 뒤 자동 컨트롤러이며, 아래 수동 도구는 최초 설치·main 회귀 복구에만 [하네스 설명서](verification-harness.md)의 절차로 사용한다.
검사·검토 뒤 새 commit이 올라오면 새 head를 기준으로 다시 확인한다. 병합 담당 AI가 PR을 순서대로 통합하고, 확인한 head SHA를 병합 요청에 고정한다.

```text
gh pr ready <PR번호>
gh pr checks <PR번호>
python3 tools/ai_workflow.py merge <PR번호>
python3 tools/ai_workflow.py merge <PR번호> --execute
```

위 꺾쇠 값은 실제 값으로 바꿔 실행한다. `merge`의 기본 동작은 읽기 전용 확인이며, `--execute`일 때만 병합한다. 도구가 거절한 상태를 직접 `gh pr merge`나 API로 우회하지 않는다. `--admin`으로 검사를 우회하지 않는다. 병합 요청 성공 응답 후에도 PR의 merged 상태와 원격 main commit을 확인한다.

버전 관리를 AI에게 맡긴 이번 사용자 지시 범위에서 AI가 병합까지 진행한다. 저장소 규칙 때문에 추가 사람 승인을 임의로 요구하지 않는다. 제품의 새 외부 배포·과금·데이터 삭제 권한이 자동으로 생기는 것은 아니다.
main 직접 push, force push, 검사 우회 merge는 하지 않는다. 운영 중 문제가 생기면 기존 커밋을 지워 이력을 바꾸는 대신 취소 PR을 만든다.

완료 보고에는 이슈·PR·병합된 commit, 실제 검증 결과, 중요한 남은 제약을 적는다. PR이 열렸다는 사실과 main 병합·AWS 배포 완료를 구분한다.

## 6. 에이전트를 바꿀 때

다음 AI에게 아래 내용을 전달하면 같은 작업을 이어갈 수 있다. 실제 소스·검사 로그는 이슈와 PR에서 다시 확인한다. 대화 기록만을 저장소의 최신 사실로 취급하지 않는다.

```text
이 저장소의 AGENTS.md, SPEC.md, docs/ai-collaboration.md를 먼저 읽어줘.
작업 이슈: #번호
PR: URL 또는 아직 없음
작업 브랜치와 현재 HEAD: 실제 값
완료한 변경: 실제 구현 내용
검증: 명령, 결과, 실행 환경, 미실행 이유
다음 작업과 미해결 사항: 실제 내용
담당 파일 또는 겹치면 안 되는 작업: 실제 경로
이어서 GitHub 버전 관리까지 공통 규칙대로 진행해줘.
```

현재 브랜치와 작업 정보를 정리할 때 `handoff`를 사용할 수 있다. 출력 내용을 이슈·PR에 남기고 실제 검증 결과와 다음 작업을 보완한다. 이 명령은 인수인계 문안을 출력하며 GitHub에 대신 게시하지 않는다.

```bash
python3 tools/ai_workflow.py handoff 12
```

Codex는 AGENTS를 기준으로 하고, Claude·Gemini CLI·Copilot·Cursor용 진입 파일은 같은 문서로 안내한다. 특정 도구가 지침 파일을 읽는지와 실제 로그인·권한은 해당 도구에서 확인한다. 진입 파일을 추가했다는 사실을 모든 에이전트의 실행 시험 통과로 보고하지 않는다.

## 7. 자동 검사와 GitHub 설정의 범위

저장소의 정책 검사와 GitHub 서버 설정은 별개다. 로컬 검사·PR CI는 잘못된 브랜치·커밋·PR 형식·민감 자료 포함을 찾아낸다. 모든 비밀값이나 모든 잘못된 동작을 탐지한다고 보장하지 않는다.
main 보호·필수 검사·병합 방식 설정의 실제 적용 여부는 [GitHub 설정 기록](github-settings.md)을 확인한다. 요금제·권한 때문에 적용되지 않은 서버 제약을 적용됐다고 표시하지 않는다. 그 경우에도 AI는 공통 지침과 병합 도구 검사를 따른다.

## 공식 참고

- [GitHub PR과 squash merge](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/incorporating-changes-from-a-pull-request/about-pull-request-merges)
- [Claude 프로젝트 지침](https://code.claude.com/docs/en/memory)
- [Gemini CLI GEMINI.md](https://geminicli.com/docs/cli/gemini-md/)
- [Copilot 저장소 지침](https://docs.github.com/en/copilot/how-tos/configure-custom-instructions/add-repository-instructions)
- [Cursor 프로젝트 규칙](https://cursor.com/docs/rules)
