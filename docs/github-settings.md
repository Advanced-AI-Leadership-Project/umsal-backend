# GitHub 설정과 적용 범위

대상은 비공개 저장소 `Advanced-AI-Leadership-Project/umsal-backend`다. 다른 저장소나 조직 전체의 설정을 변경하지 않는다.

## 2026-11-03 적용 상태

조직 요금제는 Free다. 이 비공개 저장소의 rulesets·branch protection 조회 API는 403과 `Upgrade to GitHub Pro or make this repository public to enable this feature.`를 반환했다. 관리자 권한 부재가 아니라 기능 제공 범위의 제한이다.
따라서 서버가 main 직접 push·실패한 CI의 병합·관리자 우회를 강제로 차단하는 상태가 아니다. 공개 범위나 요금제를 변경하지 않았다. GitHub 화면의 "Your main branch isn't protected" 배너는 이 제한 때문이며 현재 요금제에서는 해소할 수 없다.

[repository-settings.json](../.github/repository-settings.json)을 저장소 API로 적용하고 응답을 다시 확인했다.

| 설정 | 적용 결과 |
|---|---|
| 기본 브랜치 / 공개 범위 | main / private 유지 |
| 병합 방식 | squash 허용, merge commit·rebase 비활성화 |
| squash 제목 / 본문 | PR_TITLE / PR_BODY |
| 병합 후 작업 브랜치 | 자동 삭제 활성화 |
| GitHub 자동 병합 | 비활성화. 신뢰된 Actions 컨트롤러가 검증 후 병합 |
| GitHub Update branch 버튼 | 비활성화. AI가 `chore(sync)` commit으로 main을 반영 |

```bash
gh api repos/Advanced-AI-Leadership-Project/umsal-backend \
  --jq '{private,default_branch,allow_squash_merge,allow_merge_commit,allow_rebase_merge,delete_branch_on_merge,allow_auto_merge,allow_update_branch}'
```

## 자동화 저장소 값

| 항목 | 용도 | 상태 |
|---|---|---|
| Actions 변수 `UMSAL_AUTO_MERGE` | `true`일 때만 컨트롤러가 피드백·통합 검사·병합 수행 | `true` (설치 PR #2 병합과 main `377c9ce` 검증 성공 확인 후 설정) |
| 라벨 `harness:hold` | 개별 PR 자동 병합 보류 | 생성 |
| 라벨 `area:infra`, `priority:P0` | main 회귀 복구 이슈 자동 생성 | 생성 |

설치 PR #2는 컨트롤러가 main에 없던 상태라 `python3 tools/ai_workflow.py merge 2` plan → `--execute`로 병합했다. 변수 설정 후 수동 dispatch한 컨트롤러 실행은 열린 PR이 없는 상태에서 오류 없이 종료했다.

```bash
gh variable list --repo Advanced-AI-Leadership-Project/umsal-backend
gh variable set UMSAL_AUTO_MERGE --repo Advanced-AI-Leadership-Project/umsal-backend --body false  # 전체 중지
```

## 서버 보호 목표

[main-protection.json](../.github/main-protection.json)은 **향후 지원되는 플랜에서 적용할 목표 설정**이다. 현재 적용 결과 파일이 아니다.
목표는 최신 main 반영, GitHub Actions의 policy/python/quality/security/harness 성공, PR 경유, 관리자 포함 적용, 강제 push·main 삭제 금지와 검토 대화 해결이다. app ID 15368은 GitHub Actions의 ID이며 umsal-pwa의 check-run 응답에서 확인한 값이다.
같은 계정의 AI들이 자신이 작성한 PR을 GitHub 승인할 수 없으므로 필수 승인 수는 0이며, 별도 AI 검토 결과와 현재 head SHA를 PR에 남긴다.

```bash
gh api --method PUT \
  repos/Advanced-AI-Leadership-Project/umsal-backend/branches/main/protection \
  --input .github/main-protection.json
```

## 팀 계정의 접근 권한

다른 팀원의 AI가 branch를 push하려면 해당 계정·팀에 Write 이상 권한이 있어야 한다. 컨트롤러도 Write 이상 팀원의 내부 PR만 자동 병합한다. 조직 기본 권한을 일괄 확대하지 않고 팀원 계정·팀 단위로 부여한다.

## 현재 함께 적용하는 검사

- clone별 `make setup`(또는 `make ai-setup`): pre-commit·commit-msg·pre-push 훅 연결. 기존 다른 hooksPath는 덮어쓰지 않는다.
- PR의 `policy` job: 브랜치·commit·PR 제목/본문·현재 Head-SHA 검토 표기·금지 파일/키 패턴을 검사한다.
- CI의 quality/python/security/harness job: [검증 하네스](verification-harness.md) 참고.

로컬 훅과 병합 도구는 파일·Git 설정·명령으로 우회할 수 있고, CI 실패도 현재 서버가 병합 자체를 막아주지는 않는다. AI는 이 절차를 따라야 하며 이를 무조건적인 강제 보호로 설명하지 않는다.
키 탐지는 알려진 패턴과 금지 경로 검사이며 모든 비밀값을 찾아내는 보장은 아니다. 리뷰의 Head-SHA 일치는 검토 기록의 최신성 검사이며 검토자 신원·독립성을 플랫폼이 인증하지 않는다.
