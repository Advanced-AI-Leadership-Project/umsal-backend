# 엄살 백엔드 SPEC

## 목표 구조

엄살 PWA(`Advanced-AI-Leadership-Project/umsal-pwa`)가 호출하는 서버리스 백엔드다. PWA 업무 API(사용자·worker operation 전체)는 이 저장소에서 구현할 예정이다(2026-11-04 결정, 아직 미구현, [API 계약 초안](docs/api-contract.md) D1).

| 계층 | 선택 |
|---|---|
| 앱 | FastAPI (Python 3.12) |
| 실행 | AWS Lambda + API Gateway, Mangum 어댑터. Lambda는 전용 VPC의 프라이빗 서브넷에서 실행한다 |
| 관계형 저장소 | Aurora PostgreSQL serverless (자동 일시정지) |
| 키-값 저장소 | DynamoDB |
| 파일 저장소 | S3 (비공개 버킷) |
| 리전 | ap-southeast-2 (Sydney) 단일 리전 |

제품 불변조건(동의 grant·공유 scope·철회 후 비노출 등)은 [AGENTS.md](AGENTS.md)의 "제품 불변조건"을 따른다.
PWA 업무 API의 목표 계약과 현재 차이·결정 항목은 [API 계약 정합성 초안](docs/api-contract.md)을 따른다(초안. D1·D5·D6·D9 확정, 나머지는 미확정).

## 저장 계층

2026-11-04 사용자 결정, 2026-11-05 작업 상태 배치 변경(API 계약 초안 D5). 엔진 버전·용량·스키마는 후속 이슈에서 정한다.

| 저장소 | 저장 내용 | 선택 이유 |
|---|---|---|
| Aurora PostgreSQL | 사용자·가족 관계·동의 grant·공유 scope, 그리고 상태·fencing을 정하는 녹음(recordings)·분석 작업(analysis_jobs)·화자 확인(speaker_assignments)·삭제 작업(deletion_jobs)·outbox·멱등 키 | 관계와 제약(외래 키·유일성)이 필요하다. 동의 확인과 작업 상태 확정을 한 트랜잭션으로 묶어야 철회 직후의 작업 상태 확정을 막을 수 있다(결과 본문 경합은 아래 불변조건 참조) |
| DynamoDB | 전사 발화(utterances)·분석 관찰(observations)·결과 뷰·주간 집계 항목 | 양이 많고 키로 읽는 결과 본문. 어떤 결과가 유효한지는 Aurora의 작업 상태가 정한다 |
| S3 | 원음 파일·작업 산출물(작업자는 API가 허용한 작업 전용 출력 경로에만 쓴다, [API 계약 초안](docs/api-contract.md) D9) | DynamoDB 항목 400KB 제한을 넘는 파일. 보관 기한은 수명 주기 규칙으로 관리한다 |

### 저장소를 넘나드는 불변조건

- grant·scope와 작업 상태의 원본은 Aurora다. 작업 결과 확정은 Aurora 한 트랜잭션에서 현재 grant·lease·화자 확인 버전·삭제 표식을 재확인하고 작업 상태를 바꾼다. DynamoDB·S3의 결과와 파일을 반환할 때마다 Aurora의 현재 grant와 작업 상태를 확인하는 공통 계층을 거친다. 결과 항목에 복사한 grant 사본이나 DynamoDB 항목의 존재만으로 노출 여부를 판단하지 않는다.
- 결과 본문 쓰기(DynamoDB·S3)는 Aurora 트랜잭션 밖이므로, 확정 직후 철회되면 철회 뒤에 쓰인 결과 본문이 남을 수 있다. 노출은 Aurora의 작업 상태·grant 확인으로 막지만, 남은 본문의 정리 순서(쓰기 전후 버전 비교, 철회 시 삭제 전파)는 작업·결과 저장 설계에서 정한다. 정하기 전에는 이 경합을 처리했다고 표시하지 않는다.
- 철회·연결 해제·삭제는 Aurora·DynamoDB·S3 모두에 전파하고 각 저장소의 처리 상태를 추적한다. 일부 저장소만 처리된 상태를 완료로 표시하지 않는다.
- 원음 버킷은 백엔드 스택이 소유한다. 계약이 S3 `version_id`로 분석 입력을 고정하므로 버전 관리를 켠다(API 계약 초안 D6). 삭제 작업은 대상 객체의 모든 버전을 명시적으로 지우고 그 처리 상태를 추적하며, 수명 주기 규칙으로 이전 버전을 짧은 기한 안에 자동 만료해 놓친 사본을 정리한다. 모든 버전이 지워지기 전에는 라이브 삭제 완료로 표시하지 않는다. 삭제 대상에는 원음뿐 아니라 해당 녹음의 작업 전용 출력 경로(API가 아직 확정하지 않은 산출물 포함)와 `version_id`로 고정한 작업 산출물이 들어가며, 각 객체의 모든 버전·delete marker와 미완료 multipart 업로드를 함께 정리한다. 만료 기한 수치는 보관·삭제 정책(umsal-pwa #19)에서 정한다. 계정에 있는 PWA 데모 버킷(`umsal-demo-storage`·`umsal-pwa-demo*` 스택)은 합성 자료·정적 파일 전용이므로 실제 원음을 넣지 않고 그 스택 이름을 재사용하지 않는다.

### VPC 구성 원칙

- NAT 게이트웨이를 두지 않는 것을 기본으로 한다. DynamoDB·S3는 게이트웨이 VPC 엔드포인트(추가 요금 없음)로 연결한다.
- Aurora 접속은 IAM 데이터베이스 인증을 우선 검토해 Secrets Manager 인터페이스 엔드포인트 비용을 피한다.
- RDS Proxy는 DB 용량에 비례하는 추가 요금이 들므로 연결 수가 실제 문제가 될 때 도입한다. 도입 전에는 Lambda 동시성과 연결 수 한도를 함께 정한다.
- 게이트웨이 엔드포인트가 없는 AWS 서비스(SQS·KMS 등)를 VPC 안에서 쓰게 되면 인터페이스 엔드포인트 비용을 해당 설계에 포함한다.
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
