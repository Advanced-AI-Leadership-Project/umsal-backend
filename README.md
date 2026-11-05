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

## 로컬 DB (PostgreSQL)

Docker Desktop이 켜져 있어야 한다. DB는 `127.0.0.1:5434`에만 열리고 합성 데이터만 넣는다.

```bash
make db-up        # PostgreSQL 17 컨테이너 시작(healthy까지 대기)
make db-migrate   # Alembic 마이그레이션 적용
make db-test      # 실제 DB 통합 테스트(마이그레이션을 적용했다 되돌림). CI는 같은 테스트를 PostgreSQL 서비스로 항상 실행
DATABASE_URL=postgresql://umsal:umsal_local@127.0.0.1:5434/umsal_backend make api
make db-down      # 컨테이너 중지(데이터 볼륨은 유지)
```

만료된 멱등 기록·초대 정리: `DATABASE_URL=... uv run python -m umsal_backend.maintenance`(AWS 주기 실행은 2단계에서 연결).

`make`가 없는 Windows에서는 `docker compose up -d --wait db`, `uv run alembic upgrade head`처럼 Makefile의 명령을 직접 실행하고 `DATABASE_URL`(통합 테스트는 `TEST_DATABASE_URL`)을 환경 변수로 지정한다. `POSTGRES_PORT`·`POSTGRES_USER`·`POSTGRES_PASSWORD`를 바꿨다면 `make db-migrate LOCAL_DATABASE_URL=...`처럼 같은 값을 넘긴다. Alembic·SQLAlchemy는 개발 의존성이라 Lambda zip에 들어가지 않는다. `DATABASE_URL`이 없으면 API는 DB에 접속하지 않고 ready의 `checks.aurora`는 `not_configured`다. 새 마이그레이션은 `uv run alembic revision -m "설명"`으로 만들고 SQL을 직접 쓴다. Aurora 적용 전에는 `uv run alembic upgrade head --sql`로 SQL을 검토한다.

## 검사

Python 테스트는 실제 PostgreSQL이 필요하다. 먼저 `make db-up`으로 로컬 DB를 띄운다. DB가 없으면 `make python-check`는 건너뛰지 않고 실패한다. 테스트는 그 로컬 DB에 마이그레이션을 적용하고 테이블을 비우므로, 손으로 넣어 둔 합성 데이터는 지워진다. CI는 자체 PostgreSQL 서비스로 같은 테스트를 실행한다.

```bash
make verify-fast      # 전체 tracked tree 정책 검사 + Ruff·포맷·pytest(coverage 80%)
make verify-security  # pip-audit (네트워크 필요)
make verify           # 위 검사 전체 = CI와 같은 suite
```

PR 검사·자동 병합 구조는 [검증 하네스](docs/verification-harness.md)를 따른다.

업무 API 계약 원본은 [contracts/openapi.yaml](contracts/openapi.yaml)이고 결정 기록은 [API 계약 정합성 문서](docs/api-contract.md)에 있다.

AWS 배포 절차와 확인 기록은 [백엔드 API AWS 배포](docs/aws-deployment.md)를 따른다.
