# 3단계 AWS 설계: 저장소 기반 (DynamoDB·S3)

기준일: 2026-11-05 · 이슈 #52 · **템플릿·코드만 있고 AWS 자원은 아직 만들지 않았다.** 생성은 비용·구성을 사용자에게 확인받은 뒤 한다.

## 1. 범위

**사용자 결정(2026-11-05):** MVP 범위(D8, umsal-pwa #17)와 무관한 기반만 지금 만든다. 항목 키 설계, 업로드(`createUpload`·`finalizeUpload`)·작업·결과 API, S3 CORS·presigned URL, API의 S3 권한은 D8 결정 뒤 별도 작업으로 한다.

| 포함 | 제외(D8 뒤) |
|---|---|
| DynamoDB 테이블 1개(범용 `pk`/`sk` 키) | 항목 종류별 키·GSI 설계 |
| 원음 S3 버킷(비공개, 버전 관리) | 업로드 경로·CORS·presigned URL·입력 한도 |
| API의 ready에서 DynamoDB 확인 | 테이블 항목 읽기·쓰기, S3 읽기·쓰기 권한 |

## 2. 구성 ([storage.yaml](../infra/aws/storage.yaml), 스택 `umsal-backend-storage`)

| 자원 | 설정 | 근거 |
|---|---|---|
| DynamoDB `ResultsTable` | 온디맨드, `pk`(S)·`sk`(S), PITR, TTL 속성 `expires_at`, 삭제 보호, AWS 소유 키 암호화(기본), `Retain` | SPEC 저장 계층(전사 발화·관찰·결과 뷰·주간 항목). 키 이름을 범용으로 두어 D8 뒤 항목 설계가 테이블 교체 없이 가능하다. TTL은 보관 기한 정책(umsal-pwa #19) 확정 뒤 항목에 값을 넣을 때만 동작한다 |
| S3 `AudioBucket` | 공개 차단 4개, BucketOwnerEnforced, SSE-S3, 버전 관리, TLS 전용 정책, 이전 버전 1일 뒤 만료·만료된 delete marker 정리·미완료 멀티파트 1일 정리, `Retain` | D6(버전으로 입력 고정, 삭제 시 모든 버전을 명시 삭제, 수명 주기는 남은 버전의 안전망). `umsal-demo-storage`(합성 데모 전용)와 분리 |
| API 실행 역할 | `DynamoTableName`을 주면 그 테이블 하나에 `dynamodb:DescribeTable`만 추가 | ready 확인에 필요한 최소 권한. 항목 권한은 결과 API에서 범위를 정해 추가한다 |
| API 환경 변수 | `DYNAMODB_TABLE` | [dynamo.py](../src/umsal_backend/dynamo.py)가 읽는다 |

- Lambda는 VPC 안에서 기존 DynamoDB·S3 게이트웨이 엔드포인트(무료, [network.yaml](../infra/aws/network.yaml))로 접속한다. NAT·인터페이스 엔드포인트는 추가하지 않는다.
- ready 확인은 `DescribeTable`로 테이블이 `ACTIVE`인지 본다. 연결 2초·응답 3초 제한, 재시도 없이 1회만 호출하고, 오류 내용은 응답·로그에 남기지 않는다(D2와 같은 방식). 클라이언트는 첫 확인 때 만들어 재사용한다.
- 원음 버킷에는 아직 API 권한이 없다. 업로드 API를 만들 때 CORS(PWA 출처)와 presigned URL 범위를 함께 정한다. 버킷 정책에 VPC 엔드포인트 제한을 두지 않은 이유는 PWA가 presigned URL로 인터넷에서 직접 올릴 예정이기 때문이다.

## 3. 비용 (ap-southeast-2)

자원 자체에 시간당 요금은 없다. 둘 다 사용량 과금이다.

- DynamoDB 온디맨드: 요청 수와 저장 용량에 비례한다. PITR은 테이블 크기에 비례해 과금된다. 지금은 ready 확인의 `DescribeTable`(제어 평면 호출, 요청 단위 과금 대상 아님)뿐이고 항목이 없어 사실상 0이다.
- S3: 저장 용량·요청 수 비례. 원음을 올리기 전에는 0이다.
- 정확한 단가는 생성 전 AWS 가격 도구로 확인해 사용자에게 함께 보고한다.

## 4. 배포 순서

1. `umsal-backend-storage` 스택 생성(change set 확인 → 실행)
2. 이 커밋으로 API zip 빌드·업로드 → `stage2_params.py --with-storage`로 `api-params.json` 재생성 → `umsal-backend-api` 갱신(change set 확인: `FunctionRole`·`ApiFunction` 수정, 교체 없음 예상)
3. `/api/health/ready`가 200 `ready`, `checks`가 모두 `ok`인지 확인

모든 생성·갱신은 실행 전에 사용자 확인을 받는다. 두 자원 모두 `Retain`이라 스택을 지워도 남으며, 삭제는 사용자가 판단해 별도로 한다(테이블은 삭제 보호를 먼저 꺼야 한다).

## 5. 남은 결정

- 항목 키·GSI 설계, 결과 본문 쓰기와 철회 경합 처리(SPEC "저장소를 넘나드는 불변조건"): 결과 API 설계 때
- 업로드 입력 한도·CORS·presigned URL 수명: D8 뒤 업로드 API 설계 때
- 보관 기한(TTL 값, S3 현재 버전 만료): umsal-pwa #19 결정 뒤
