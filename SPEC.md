# 엄살 백엔드 SPEC

## 목표 구조

엄살 PWA(`Advanced-AI-Leadership-Project/umsal-pwa`)가 호출하는 서버리스 백엔드다. PWA 업무 API(사용자·worker operation 전체)는 이 저장소가 담당한다(2026-11-04 결정, [API 계약 초안](docs/api-contract.md) D1). 구현된 범위와 후속 범위는 아래 표로 구분한다.

| 계층 | 선택 |
|---|---|
| 앱 | FastAPI (Python 3.12) |
| 실행 | AWS Lambda + API Gateway, Mangum 어댑터. Lambda는 전용 VPC의 프라이빗 서브넷에서 실행한다 |
| 관계형 저장소 | VPC 안 RDS for PostgreSQL 17. Aurora에서 변경한 결정과 호환 필드는 아래 기록을 따른다 |
| 키-값 저장소 | DynamoDB |
| 파일 저장소 | S3 (비공개 버킷) |
| 리전 | ap-southeast-2 (Sydney) 단일 리전 |

제품 불변조건(동의 grant·공유 scope·철회 후 비노출 등)은 [AGENTS.md](AGENTS.md)의 "제품 불변조건"을 따른다.
PWA 업무 API의 목표 계약과 현재 차이·결정 항목은 [API 계약 정합성 초안](docs/api-contract.md)을 따른다(초안. D1·D2·D3·D5·D6·D7·D8·D9·D10·D11·D12 확정, D4 미확정. D8 1차 범위 설계는 [MVP 녹음·분석 흐름 설계](docs/mvp-recording-design.md)). 계약 원본은 [contracts/openapi.yaml](contracts/openapi.yaml)이다.

## 저장 계층

2026-11-04 사용자 결정, 2026-11-05 작업 상태 배치 변경(API 계약 초안 D5). **2026-11-05 사용자 결정(#44): 관계형 저장소는 Aurora PostgreSQL 대신 VPC 안 RDS for PostgreSQL 17을 쓴다**(Free 플랜에서는 Aurora를 VPC 없는 express로만 만들 수 있다). 이 문서와 코드의 'Aurora'(예: ready 응답의 `checks.aurora`, 계약 필드라 이름 유지)는 이 관계형 PostgreSQL 저장소를 뜻한다.

| 저장소 | 저장 내용 | 선택 이유 |
|---|---|---|
| RDS PostgreSQL | 사용자·가족 관계·동의 grant·공유 scope, 녹음(recordings)·분석 작업(analysis_jobs)·화자 확인(speaker_assignments)·outbox·멱등 키・통화(voice_calls). 삭제 작업(deletion_jobs)은 목표 범위 | 관계와 제약(외래 키·유일성)이 필요하다. 동의 확인과 작업 상태 확정을 한 트랜잭션으로 묶어야 철회 직후의 작업 상태 확정을 막을 수 있다(결과 본문 경합은 아래 불변조건 참조) |
| DynamoDB | 현재 CPU 결과 본문, 별도 통화 신호 테이블. 발화·관찰·주간 집계의 세부 분해 저장은 목표 설계 | 양이 많고 키로 읽는 결과 본문. 어떤 결과가 유효한지는 PostgreSQL의 작업 상태가 정한다 |
| S3 | 원음 파일·작업 산출물(작업자는 API가 허용한 작업 전용 출력 경로에만 쓴다, [API 계약 초안](docs/api-contract.md) D9) | DynamoDB 항목 400KB 제한을 넘는 파일. 기한·모든 버전·파생 산출물의 물리 삭제는 후속 정책/구현과 대조해야 한다 |

현재 구현/목표의 구분은 아래 표를 따른다. 다음 불변조건은 목표 요구사항도 포함하며, 물리 삭제·백업 만료까지 완료됐다는 의미는 아니다.

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

2026-11-17 main `e0b14a9`의 소스 기준이다. 과거 배포 근거는 [배포 기록](docs/aws-deployment.md)과 [통화 문서](docs/voice-calls.md)로 분리한다. 이번 문서 정리는 AWS 현재 설정·실기기를 새로 검증하지 않았다.

| 항목 | 구현·남은 범위 |
|---|---|
| health·Lambda | `/health`와 `/api/health/live`, `/api/health/ready`, Mangum 진입점. ready의 `checks.aurora`는 호환 필드명이며 실제 DB는 RDS PostgreSQL이다 |
| 인증·가족 | Cognito access token 검증, 사용자 첫 요청 생성, 관계 목록/상세, 초대 생성/수락, 연결별 호칭·해제. 초대 수락은 동의가 아니다 |
| 동의·권한 | 방향별 grant와 scope·버전 검사, 기본 전부 거부. 현재 grant·활성 관계·화자 버전에 따라 작업 확정과 결과 노출을 제한한다 |
| 업로드·조회 | `createUpload`, `finalizeUpload`, `getJob`, `getRecording`, `listRecordings`. 제한된 S3 PUT·버전/체크섬 고정·내부 사용자별 키·기존 키 호환 |
| 화자·결과 | `assignSpeaker`, `getResult` (`assignments.py`). 확인한 대상/버전·현재 공유 scope로 결과를 조회하며 미확인 상태에서 텍스트 결과를 만들지 않는다 |
| 실제 CPU 분석 | `workers/cpu`·`worker_api.py`·outbox/SQS. `stub-1` 합성 대역과 `cpu-1` 합성 입력·`cpu-2` 실제/합성 입력을 구분한다. 모노 화자 준비 → 사용자 확인 → 전사/텍스트 분석. 모델 품질·임상 타당성 완료를 뜻하지 않는다 |
| CPU 자동 기동 | `cpu-analysis.yaml`·`worker_control.py`: 최대 1대, 처리 중 축소 보호, 작업 후 유휴 종료. 배포된 실제 용량/정상 종료는 별도 AWS 확인이 필요하다 |
| 전경 1:1 통화 | `calls.py` 9개 operation·RDS 상태·별도 DynamoDB 신호·coturn. 분석과 별도이며 통화 미디어를 저장하지 않는다. 화면 잠금/백그라운드 수신·실제 단말 통화는 별도 범위 |
| 데이터·IaC | PostgreSQL 마이그레이션 `0001`~`0009`, RDS/S3/DynamoDB·인증·CPU·통화 CloudFormation. 코드에 있는 마이그레이션과 현재 운영 DB 적용 버전을 혼동하지 않는다 |
| API 계약 | 원본 v0.5.0, 33 paths / 41 operations. 공개 FastAPI `/api` 구현은 health 2개 포함 27개. 내부 claim/heartbeat/complete/fail은 IAM Lambda 경로이며 목표 HTTP operation 4개를 구현한 것으로 세지 않는다. [상세 대조](docs/api-contract.md#2-현재-구현과-원안의-차이) |
| PWA 연동 | PWA의 계정·가족·동의·접수·화자/결과·통화 클라이언트가 이 API를 호출한다. PWA 로컬 health/push scaffold는 업무 API와 다르다 |
| 후속 기능 | 녹음 DELETE·삭제 상태/일괄 삭제, 주간 집계, 설정/기기 등록, 피드백, 공개 retry 경로. `maintenance.py`의 녹음 task는 `not_implemented`. 철회 후 비노출·작업 fencing과 원음/결과/백업 물리 삭제를 구분한다 |

목표 설계를 구현 완료로 보고하지 않는다. 통화 DELETE는 통화 종료이며 녹음 DELETE가 아니다. 오래된 이슈는 수용 기준 전체를 대조한 뒤 닫는다.

CPU 분석은 양쪽 계정의 현재 분석 동의를 확인한 실제·합성 녹음을 처리한다. `CpuRealAudioEnabled=true`에서 WAV·MP3·M4A, 최대 50 MiB·10분을 모노 화자 분리 후 사용자 화자 확인과 텍스트 분석으로 연결한다. 기존 cpu-1 작업·결과도 지원한다. 모델과 AWS 작업 큐의 구현 범위는 [CPU 분석](workers/cpu/README.md)에 기록한다.
CPU 작업 큐는 1분 주기 용량 제어와 최대 1대 Auto Scaling을 사용한다. 처리 중인 워커는 축소 보호를 유지하고, 60초간 새 메시지가 없으면 큐를 다시 확인한 후 보호를 해제해 종료한다. 실제 자동 기동·분석·종료의 배포 검증은 별도로 기록한다.

## 가족 음성 통화

활성 가족 간 전경 WebRTC 음성 통화 API와 AWS coturn 릴레이 구성을 추가했다. 수락·거절·중복 통화 방지·세션 소유·유지 신호·종료를 처리한다. 최대 30분, 미디어 저장 없음. 구성과 배포 절차·실기기 한계는 [통화 설명서](docs/voice-calls.md)를 따른다.
