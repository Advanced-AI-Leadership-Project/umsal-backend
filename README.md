# 엄살 백엔드

엄살 서비스의 FastAPI 서버리스 백엔드(API Gateway, Lambda, Aurora PostgreSQL, DynamoDB, S3 · ap-southeast-2)다.
현재 구현 범위는 [SPEC](SPEC.md), 에이전트 작업 규칙은 [AGENTS](AGENTS.md), 협업 절차는 [CONTRIBUTING](CONTRIBUTING.md)을 따른다.

## 시작하기

필요 도구: Git, [GitHub CLI](https://cli.github.com/)(`gh auth login`), [uv](https://docs.astral.sh/uv/) 0.12.3, Python 3.12, make.

```bash
make setup                          # 잠금 의존성 설치 + 이 clone의 Git hook 연결
python3 tools/ai_workflow.py doctor # 원격·도구 버전·GitHub 인증 확인
make api                            # http://127.0.0.1:8000/health, /api/health/live, /api/health/ready
```

## 검사

```bash
make verify-fast      # 전체 tracked tree 정책 검사 + Ruff·포맷·pytest(coverage 80%)
make verify-security  # pip-audit (네트워크 필요)
make verify           # 위 검사 전체 = CI와 같은 suite
```

PR 검사·자동 병합 구조는 [검증 하네스](docs/verification-harness.md)를 따른다.

AWS 배포 절차와 확인 기록은 [백엔드 API AWS 배포](docs/aws-deployment.md)를 따른다.
