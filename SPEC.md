# 엄살 백엔드 SPEC

## 목표 구조

엄살 PWA(`Advanced-AI-Leadership-Project/umsal-pwa`)가 호출하는 서버리스 백엔드다.

| 계층 | 선택 |
|---|---|
| 앱 | FastAPI (Python 3.12) |
| 실행 | AWS Lambda + API Gateway, Mangum 어댑터. Lambda는 전용 VPC의 프라이빗 서브넷에서 실행한다 |
| 관계형 저장소 | Aurora PostgreSQL serverless (자동 일시정지) |
| 키-값 저장소 | DynamoDB |
| 파일 저장소 | S3 (비공개 버킷) |
| 리전 | ap-southeast-2 (Sydney) 단일 리전 |

제품 불변조건(동의 grant·공유 scope·철회 후 비노출 등)은 [AGENTS.md](AGENTS.md)의 "제품 불변조건"을 따른다.

## 저장 계층

2026-11-04 사용자 결정. 엔진 버전·용량·스키마는 후속 이슈에서 정한다.

| 저장소 | 저장 내용 | 선택 이유 |
|---|---|---|
| Aurora PostgreSQL | 사용자·가족 관계·동의 grant·공유 scope | 관계와 제약(외래 키·유일성)이 필요하고 grant 판단에 트랜잭션 일관성이 필요하다 |
| DynamoDB | 음성 메타데이터·분석 작업 상태·분석 결과·근거 | 키 기반 조회와 쓰기가 많은 작업·결과 데이터 |
| S3 | 원음 파일 | DynamoDB 항목 400KB 제한을 넘는 파일. 보관 기한은 수명 주기 규칙으로 관리한다 |

### 저장소를 넘나드는 불변조건

- grant·scope의 원본은 Aurora 하나다. DynamoDB·S3의 결과와 파일을 저장하거나 반환할 때마다 Aurora의 현재 grant를 확인하는 공통 계층을 거친다. 결과 항목에 복사한 grant 사본으로 노출 여부를 판단하지 않는다.
- 철회·연결 해제·삭제는 Aurora·DynamoDB·S3 모두에 전파하고 각 저장소의 처리 상태를 추적한다. 일부 저장소만 처리된 상태를 완료로 표시하지 않는다.
- 원음 버킷은 백엔드 스택이 소유한다. 버전 관리를 끄거나 이전 버전을 바로 만료해 삭제 후 사본이 남지 않게 한다. 계정에 있는 PWA 데모 버킷(`umsal-demo-storage`·`umsal-pwa-demo*` 스택)은 합성 자료·정적 파일 전용이므로 실제 원음을 넣지 않고 그 스택 이름을 재사용하지 않는다.

### VPC 구성 원칙

- NAT 게이트웨이를 두지 않는 것을 기본으로 한다. DynamoDB·S3는 게이트웨이 VPC 엔드포인트(추가 요금 없음)로 연결한다.
- Aurora 접속은 IAM 데이터베이스 인증을 우선 검토해 Secrets Manager 인터페이스 엔드포인트 비용을 피한다.
- RDS Proxy는 연결 수가 실제 문제가 될 때 도입한다. 도입 전에는 Lambda 동시성과 연결 수 한도를 함께 정한다.
- 발표·체험 기간처럼 재개 지연을 피해야 할 때만 Aurora 최소 용량을 올리고 끝나면 되돌린다.

## 현재 구현 상태

| 항목 | 상태 |
|---|---|
| `GET /health` | 구현. 프로세스 생존만 응답하며 Aurora·DynamoDB·S3 등 의존성 상태는 보증하지 않는다 |
| Lambda 핸들러 | 구현. 합성 API Gateway HTTP API v2 이벤트로만 테스트했다 |
| AWS 배포(IaC)·VPC | 미구현 |
| 인증·동의·분석 | 미구현 |
| Aurora·DynamoDB·S3 저장 | 미구현 |
| PWA 연동 | 미구현 |

목표 설계를 구현 완료로 보고하지 않는다. 기능이 추가되면 위 표를 실제 상태로 갱신한다.
