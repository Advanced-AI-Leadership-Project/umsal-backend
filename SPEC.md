# 엄살 백엔드 SPEC

## 목표 구조

엄살 PWA(`Advanced-AI-Leadership-Project/umsal-pwa`)가 호출하는 서버리스 백엔드다. PWA 업무 API(사용자·worker operation 전체)는 이 저장소가 담당한다(2026-11-04 결정, [API 계약 초안](docs/api-contract.md) D1). 구현된 범위와 후속 범위는 아래 표로 구분한다.

| 계층 | 선택 |
|---|---|
| 앱 | FastAPI (Python 3.12) |
| 실행 | AWS Lambda + API Gateway, Mangum 어댑터. Lambda는 전용 VPC의 프라이빗 서브넷에서 실행한다 |
| 관계형 저장소 | Aurora PostgreSQL serverless (자동 일시정지) |
| 키-값 저장소 | DynamoDB |
| 파일 저장소 | S3 (비공개 버킷) |
| 리전 | ap-southeast-2 (Sydney) 단일 리전 |

제품 불변조건(동의 grant·공유 scope·철회 후 비노출 등)은 [AGENTS.md](AGENTS.md)의 "제품 불변조건"을 따른다.
PWA 업무 API의 목표 계약과 현재 차이·결정 항목은 [API 계약 정합성 초안](docs/api-contract.md)을 따른다(초안. D1·D2·D3·D5·D6·D7·D8·D9·D10·D11·D12 확정, D4 미확정. D8 1차 범위 설계는 [MVP 녹음·분석 흐름 설계](docs/mvp-recording-design.md)). 계약 원본은 [contracts/openapi.yaml](contracts/openapi.yaml)이다.

## 저장 계층

2026-11-04 사용자 결정, 2026-11-05 작업 상태 배치 변경(API 계약 초안 D5). **2026-11-05 사용자 결정(#44): 관계형 저장소는 Aurora PostgreSQL 대신 VPC 안 RDS for PostgreSQL 17을 쓴다**(Free 플랜에서는 Aurora를 VPC 없는 express로만 만들 수 있다). 이 문서와 코드의 'Aurora'(예: ready 응답의 `checks.aurora`, 계약 필드라 이름 유지)는 이 관계형 PostgreSQL 저장소를 뜻한다.

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

2026-11-05 사용자 결정으로 이 원칙을 유지한다(Aurora express configuration은 채택하지 않음). 2단계 설계는 [docs/aws-stage2-design.md](docs/aws-stage2-design.md)(#40)를 따른다.

- NAT 게이트웨이를 두지 않는 것을 기본으로 한다. DynamoDB·S3는 게이트웨이 VPC 엔드포인트(추가 요금 없음)로 연결한다.
- Aurora 접속은 IAM 데이터베이스 인증을 우선 검토해 Secrets Manager 인터페이스 엔드포인트 비용을 피한다.
- RDS Proxy는 DB 용량에 비례하는 추가 요금이 들므로 연결 수가 실제 문제가 될 때 도입한다. 도입 전에는 Lambda 동시성과 연결 수 한도를 함께 정한다.
- 게이트웨이 엔드포인트가 없는 AWS 서비스(SQS·KMS 등)를 VPC 안에서 쓰게 되면 인터페이스 엔드포인트 비용을 해당 설계에 포함한다.
- RDS PostgreSQL은 자동 정지가 없어 항상 과금된다. 오래 쓰지 않을 때는 수동으로 정지한다(7일 뒤 자동 재시작).
- VPC 안 Lambda는 인터넷에 나가지 않는다. Cognito JWKS는 배포 때 받아 Lambda 설정에 고정하고(키 교체 시 재배포), 마이그레이션은 VPC 안 전용 Lambda로 실행한다(#40).

## 현재 구현 상태

| 항목 | 상태 |
|---|---|
| `GET /health` | 구현. 배포 확인용. 프로세스 생존만 응답하며 Aurora·DynamoDB·S3 등 의존성 상태는 보증하지 않는다 |
| `GET /api/health/live`·`GET /api/health/ready` | 구현(D2, #17). PWA와 같은 응답 모양. ready는 `checks.aurora`·`checks.dynamodb`를 따로 표시하고 둘 다 `ok`일 때만 `ready`(PWA 호환 `checks.database`도 그때만 `ok`). PostgreSQL 확인(`checks.aurora`, 2단계 배포에서 `ok` 확인)과 DynamoDB 확인(`DescribeTable`로 `ACTIVE`, #52)이 있다. 2026-11-05 3단계 배포 뒤 배포 환경의 ready는 200 `ready`(세 `checks` 모두 `ok`)다(#54) |
| Lambda 핸들러 | 구현. 합성 API Gateway HTTP API v2 이벤트로만 테스트했다 |
| AWS 배포(IaC) | 1단계(#19, API Gateway HTTP API + Lambda)와 2단계(VPC·RDS PostgreSQL·Cognito, 2026-11-05 배포, 기록 #49)를 CloudFormation으로 배포했다([배포 문서](docs/aws-deployment.md)). 3단계 저장소 기반(DynamoDB 테이블·원음 S3 버킷, `storage.yaml`, #52)도 2026-11-05 생성했다([설계](docs/aws-stage3-design.md), 기록 #54). 2026-11-08부터 PWA CloudFront `/api/*`(푸시 `/api/push/*` 제외)가 이 API로 간다(umsal-pwa #115) |
| 2단계 AWS | 배포·확인 완료(2026-11-05). IAM 토큰 DB 접속(verify-full TLS, 토큰 10분 재사용), 배포 때 고정한 Cognito JWKS, 마이그레이션 Lambda(2026-11-08 `0005`까지 적용), 매일 정리 작업 스케줄, `network.yaml`·`database.yaml`(RDS PostgreSQL, #44)·`auth.yaml`·`jobs.yaml`·일회용 `db-bootstrap.yaml`(사용 후 삭제) |
| 사용자 인증 | Cognito access token 검증 의존성 구현(D3, #21). 배포 환경에서 합성 계정의 access token으로 `/api/me` 200, id token 401을 확인했다(#49) |
| 사용자·관계 조회 | `GET /api/me`(인증된 첫 성공 요청에서 사용자 생성, 실패한 요청의 트랜잭션은 되돌림), `GET /api/connections`(cursor 페이지), `GET /api/connections/{id}`(비참여자 404) 구현(#29). `users`·`connections` 마이그레이션 `0002`. 실제 PostgreSQL 테스트를 거쳤고 2단계로 AWS에 배포했다. 표시 이름은 빈 문자열이고, 가족 이름은 연결별 호칭으로 보여 준다(D10) |
| 초대 | `POST /api/invitations`(24시간 유효 코드, 해시만 저장), `POST /api/invitations/{id}/accept`(ACTIVE 관계 생성, 1회 수락, 동시 수락 방지) 구현(#31). 두 경로 모두 `Idempotency-Key` 필수(같은 요청 재전송, 다른 요청 409). 마이그레이션 `0003`. 실제 PostgreSQL 테스트를 거쳤고 2단계로 AWS에 배포했다. 요청 한도(429) 미구현. 만료 기록·만료 미수락 초대 정리 작업 `python -m umsal_backend.maintenance`(#36)는 있으나 주기 실행은 2단계 jobs.yaml의 매일 03:00(KST) 일정으로 연결 |
| 동의 | `GET/PUT /api/connections/{id}/consents` 구현(#34). 방향별 grant(분석·공유 scope·확인 preview), 관계 생성 시 전부 거부로 시작, 본인 행만 `expected_version`으로 수정, 서버가 양쪽 허용 여부 계산(ACTIVE일 때만). 마이그레이션 `0004`. 실제 PostgreSQL 테스트를 거쳤고 2단계로 AWS에 배포했다. 분석 동의 철회 시 처리 취소는 녹음·작업이 없어 미연결 |
| 연결 해제 | `DELETE /api/connections/{id}` 구현(#36). 관계 행 잠금 → DISCONNECTED·version+1 → 양방향 grant 전부 거부·version+1, 이미 해제면 변화 없는 202, `Idempotency-Key` 필수. 보관 자료 삭제는 하지 않는다(별도 삭제 요청). 실제 PostgreSQL 테스트를 거쳤고 2단계로 AWS에 배포했다 |
| 연결별 호칭 | `PUT /api/connections/{id}/label`(`setConnectionLabel`) 구현(#62, D10). 초대 생성·수락 본문의 선택 `label`, 응답 `Connection.label`(요청자 본인의 호칭만). 1~16자·제어 문자 불가를 API와 DB CHECK 양쪽에서 검사, 참여자만·ACTIVE 연결만 변경(해제된 연결의 기존 호칭은 본인 조회에 계속 보임), `Idempotency-Key` 필수. 마이그레이션 `0005`(`connection_labels`, `invitations.inviter_label`). 실제 PostgreSQL 테스트를 거쳤고 2026-11-08 AWS에 배포했다(코드 `110b6b4`, [기록](docs/aws-deployment.md#연결별-호칭-배포-기록-62)) |
| 녹음 업로드·조회 | `createUpload`·`finalizeUpload`·`getJob`·`getRecording`·`listRecordings` 구현(#73). 마이그레이션 `0006`. 신규 S3 키는 인증된 내부 `user_id`별로 분리하고 기존 키도 DB `s3_key`로 재발급·finalize한다(#77, [운영 소스 복원 기록](docs/deployed-mobile-restoration.md)). 외부 API 계약·동의·권한 범위는 동일하다 |
| 분석 | `stub-1` 대역의 오디오 준비 단계만 구현(`demo: true`). 실제 모델·화자 확인·결과·삭제 API는 후속 범위 |
| PostgreSQL 연결·마이그레이션 | 기반 구현(#23). 로컬 Docker PostgreSQL 17, `DATABASE_URL` 연결(연결 3초·쿼리 3초 제한), ready의 `checks.aurora` 실제 확인, Alembic(`0001` 기준, `0002` users·connections, `0003` invitations·idempotency_keys, `0004` consent_grants). AWS에서는 RDS PostgreSQL(IAM 인증)에 `0004`까지 적용했다(2단계, #49). 나머지 업무 테이블은 미구현 |
| DynamoDB·S3 저장 | 기반(#52)에 S3 서명 PUT·버전/체크섬 확인·finalize 이전 버전 정리 구현(#73). 사용자는 비공개 버킷에 제한된 URL로 업로드하며 API는 원음 본문을 읽지 않는다. 사용자별/기존 키 호환은 #77. 결과 저장·삭제 작업 전체는 후속 범위 |
| API 계약 | 원본을 `contracts/openapi.yaml`(v0.4.0)로 이관(#25). 정적 검사, 구현 operation과 계약의 자동 비교(`tests/test_contract_parity.py`, #36), 응답 스키마 검사를 테스트로 실행한다. 계약 v0.4.0의 operation 32개 중 health 2개를 포함한 16개 구현 |
| PWA 연동 | health 경로만 PWA 계약에 맞춤. 업무 API와 실제 연결 확인은 미구현 |

목표 설계를 구현 완료로 보고하지 않는다. 기능이 추가되면 위 표를 실제 상태로 갱신한다.

CPU 분석은 양쪽 계정의 현재 분석 동의를 확인한 실제·합성 녹음을 처리한다. `CpuRealAudioEnabled=true`에서 WAV·MP3·M4A, 최대 50 MiB·10분을 모노 화자 분리 후 사용자 화자 확인과 텍스트 분석으로 연결한다. 기존 cpu-1 작업·결과도 지원한다. 모델과 AWS 작업 큐의 구현 범위는 [CPU 분석](workers/cpu/README.md)에 기록한다.
CPU 작업 큐는 1분 주기 용량 제어와 최대 1대 Auto Scaling을 사용한다. 처리 중인 워커는 축소 보호를 유지하고, 60초간 새 메시지가 없으면 큐를 다시 확인한 후 보호를 해제해 종료한다. 실제 자동 기동·분석·종료의 배포 검증은 별도로 기록한다.

## 가족 음성 통화

활성 가족 간 전경 WebRTC 음성 통화 API와 AWS coturn 릴레이 구성을 추가했다. 수락·거절·중복 통화 방지·세션 소유·유지 신호·종료를 처리한다. 최대 30분, 미디어 저장 없음. 구성과 배포 절차·실기기 한계는 [통화 설명서](docs/voice-calls.md)를 따른다.
