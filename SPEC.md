# 엄살 백엔드 SPEC

## 목표 구조

엄살 PWA(`Advanced-AI-Leadership-Project/umsal-pwa`)가 호출하는 서버리스 백엔드다.

| 계층 | 선택 |
|---|---|
| 앱 | FastAPI (Python 3.12) |
| 실행 | AWS Lambda + API Gateway, Mangum 어댑터 |
| 저장소 | DynamoDB |
| 리전 | ap-southeast-2 (Sydney) 단일 리전 |

제품 불변조건(동의 grant·공유 scope·철회 후 비노출 등)은 [AGENTS.md](AGENTS.md)의 "제품 불변조건"을 따른다.

## 현재 구현 상태

| 항목 | 상태 |
|---|---|
| `GET /health` | 구현. 프로세스 생존만 응답하며 DynamoDB 등 의존성 상태는 보증하지 않는다 |
| Lambda 핸들러 | 구현. 합성 API Gateway HTTP API v2 이벤트로만 테스트했다 |
| AWS 배포(IaC) | 미구현 |
| 인증·동의·분석·DynamoDB 저장 | 미구현 |
| PWA 연동 | 미구현 |

목표 설계를 구현 완료로 보고하지 않는다. 기능이 추가되면 위 표를 실제 상태로 갱신한다.
