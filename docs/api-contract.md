# PWA 업무 API 계약 정합성 초안

2026-11-04 · 이슈 #9 · 대응: umsal-pwa #20 (DEV-04)

**초안이다.** 아래 업무 API는 모두 미구현이며 "결정 필요" 항목은 사용자·팀 확인 전까지 확정이 아니다. 이 문서는 계약 차이와 결정 항목을 정리할 뿐 OpenAPI 파일을 바꾸지 않는다.

## 1. 기준 원본

| 항목 | 값 |
|---|---|
| 목표 계약 | umsal-pwa [`contracts/openapi.yaml`](https://github.com/Advanced-AI-Leadership-Project/umsal-pwa/blob/e54088b9489d67ab3269cb626c860d3b0a4bf79e/contracts/openapi.yaml) v0.2.0, 23 paths / 28 operations |
| 설계 설명 | umsal-pwa [`docs/design/09_API_계약_및_데이터설계.md`](https://github.com/Advanced-AI-Leadership-Project/umsal-pwa/blob/e54088b9489d67ab3269cb626c860d3b0a4bf79e/docs/design/09_API_%EA%B3%84%EC%95%BD_%EB%B0%8F_%EB%8D%B0%EC%9D%B4%ED%84%B0%EC%84%A4%EA%B3%84.md) (2026-10-27 Android 원안) |
| 고정 commit | `e54088b9489d67ab3269cb626c860d3b0a4bf79e` (umsal-pwa main, 2026-11-04 조회) |
| 저장 계층 | 이 저장소 [SPEC](../SPEC.md)의 "저장 계층" (#7) |

## 2. 실행 API와 목표 계약의 차이

| 구분 | 경로 | 위치 | 상태 |
|---|---|---|---|
| 실행 | `GET /health` | umsal-backend | 구현. 프로세스 생존만 응답 |
| 실행 | `GET /api/health/live`, `GET /api/health/ready` | umsal-pwa `services/api` | 구현. ready는 로컬 PostgreSQL `SELECT 1` |
| 실행 | `GET /api/push/config`, `POST·DELETE /api/push/subscription`, `POST /api/push/test` | umsal-pwa `services/api` | 테스트 채널만 구현(기본 비활성) |
| 목표 | 사용자 operation 24개(`/me`~`/devices/{id}`, 19 paths) | 미정(결정 D1) | 미구현 |
| 목표 | worker operation 4개(`/internal/worker/jobs/{id}/claim·heartbeat·complete·fail`) | 미정(결정 D1) | 미구현 |

PWA 화면은 같은 출처의 `/api/health/{live,ready}`를 호출하고 `service == "api"`, ready의 `checks.database == "ok"`를 확인한다. 이 백엔드의 `/health`는 경로와 응답 모양이 달라 그대로는 PWA와 연결되지 않는다(결정 D2).

## 3. 결정 필요 항목

| ID | 항목 | 현재 상태 | 권장안 |
|---|---|---|---|
| D1 | 업무 API를 구현할 저장소와 계약 원본 위치 | 목표 계약은 PWA 저장소에 있고, PWA 저장소에도 `services/api`(FastAPI)가 있다 | 업무 API는 umsal-backend가 구현한다. 계약 원본은 이 저장소 `contracts/openapi.yaml`로 옮기고, PWA는 이를 읽어 타입을 만든다. PWA `services/api`는 health·테스트 push 데모로 한정한다 |
| D2 | 기본 경로와 health | 백엔드 `/health`, PWA `/api/health/*` | 백엔드가 `/api` 접두사 아래에서 `GET /api/health/live`·`GET /api/health/ready`를 PWA와 같은 응답 모양으로 제공한다. ready의 `checks`는 Aurora·DynamoDB를 따로 표시한다. 기존 `/health`는 배포 확인용으로 유지할지 함께 정한다 |
| D3 | 사용자 인증 | 원안은 Cognito access token(Bearer) | 원안 유지. PWA는 Cognito 관리형 로그인 + 인가 코드·PKCE를 쓴다. 토큰 저장 위치(메모리/세션)는 umsal-pwa #24·#25에서 정한다 |
| D4 | 알림 기기 등록 | `POST /devices`가 `platform: ANDROID`, FCM `push_token` 전제 | PWA는 Web Push 구독(`endpoint`, `keys.p256dh`, `keys.auth`)을 쓴다. `DeviceRegistration`을 웹 구독 형태로 바꾸거나 별도 `/push-subscriptions`로 분리한다. 알림 방식 결정(umsal-pwa #52) 뒤 확정한다 |
| D5 | 저장 계층 반영 | 원안은 PostgreSQL 한 DB에서 job·동의·recording을 잠그고 결과·상태·outbox를 한 트랜잭션으로 commit한다(설계 문서 "기계 작업 계약" 8번) | 상태와 fencing에 관여하는 `recordings`·`analysis_jobs`·`speaker_assignments`·`deletion_jobs`·`outbox`·`idempotency_keys`는 Aurora에 두어 grant와 같은 트랜잭션으로 검사한다. DynamoDB에는 `utterances`·`observations`·결과 뷰·주간 집계 항목처럼 양이 많고 키로 읽는 자료를 둔다. **#7의 "분석 작업 상태는 DynamoDB" 결정을 바꾸는 제안이라 사용자 확인이 필요하다.** 이 제안으로도 결과 본문(DynamoDB·S3) 쓰기는 Aurora 트랜잭션에 들어가지 않으므로, SPEC의 저장소 간 철회 경합은 그대로 남는다. 결과 쓰기 순서와 노출 조건은 별도로 설계한다 |
| D6 | S3 버전 고정 | 계약의 `InputRef`·`ArtifactRef`·`Snapshot`이 S3 `version_id`로 입력을 고정한다 | SPEC은 원음 버킷 버전 관리를 "끄거나 이전 버전을 바로 만료"한다고 적었다. 계약을 유지하려면 버전 관리를 켜고, 이전 버전 만료 규칙과 삭제 시 모든 버전 삭제를 함께 둔다. 확정되면 SPEC 문구를 고친다 |
| D7 | 관계 목록 조회 | `GET /me`는 `Actor`만 반환하고 관계 목록 경로가 없다 | 가족 화면을 위해 `GET /connections`(내 관계 목록)를 추가하거나 `/me` 응답에 관계 요약을 넣는다 |
| D8 | MVP 범위 | 28개 operation 전체가 P0 후보 | 발표 범위(umsal-pwa #17)에 맞춰 1차 구현 operation을 고른다. 결정 전에는 계약에서 operation을 삭제하지 않는다 |
| D9 | 작업자의 S3 산출물 쓰기 | 원안은 작업자가 결과 artifact를 S3의 job 전용 출력 prefix에 직접 쓰고, complete에서 참조만 제출한다(설계 문서 "기계 작업 계약" 3·5번). AGENTS의 "작업자는 Aurora·DynamoDB·S3에 직접 쓰지 않고 API를 거친다"(#7, PR #8에서 추가)와 충돌한다 | 원안 구조를 유지하고 AGENTS 문구를 좁힌다. 작업자는 Aurora·DynamoDB와 원음 key에는 쓰지 않고, S3에는 claim이 허용한 job 전용 출력 prefix에만 쓴다. 그 산출물은 API가 검증·commit하기 전까지 결과로 취급하지 않는다. 강한 job별 격리가 필요하면 API가 출력용 presigned PUT URL을 발급하는 방식을 검토한다. **AGENTS 제품 불변조건을 좁히는 제안이라 사용자 확인이 필요하다.** 확정 전에는 현재 AGENTS 규칙이 유효하며 작업자의 S3 직접 쓰기를 구현하지 않는다. 확정되면 AGENTS를 고친다(harness 범위 검토 필요) |

## 4. 상태 흐름 정합

목표 흐름: `UPLOADING → PREPARING_INPUT → QUEUED_AUDIO → PROCESSING_AUDIO → NEEDS_SPEAKER_CONFIRMATION → (사람 확인) → QUEUED_TEXT → PROCESSING_TEXT → COMPLETED / ABSTAINED / FAILED`, 추가 종료 `CANCELLED / EXPIRED`.

| `Recording.status` | 작업(`Job.stage` / `Job.status`) | `GET /recordings/{id}/result` |
|---|---|---|
| `UPLOADING` | 없음 | 결과 없음 |
| `PREPARING_INPUT` | 없음(입력 고정 복사 중) | 결과 없음 |
| `QUEUED_AUDIO` | `AUDIO_PREP` / `QUEUED` | 결과 없음 |
| `PROCESSING_AUDIO` | `AUDIO_PREP` / `RUNNING` | 결과 없음 |
| `NEEDS_SPEAKER_CONFIRMATION` | `AUDIO_PREP` / `SUCCEEDED`, lease 종료 | 결과 없음. 심리 단서를 만들지 않는다 |
| `QUEUED_TEXT` | `TEXT_ANALYSIS` / `QUEUED` | 결과 없음 |
| `PROCESSING_TEXT` | `TEXT_ANALYSIS` / `RUNNING` | 결과 없음 |
| `COMPLETED` | `TEXT_ANALYSIS` / `SUCCEEDED` | `OBSERVED` 또는 `NOT_OBSERVED` |
| `ABSTAINED` | 해당 단계 / `ABSTAINED` | `ABSTAINED` |
| `FAILED` | 해당 단계 / `FAILED` | `FAILED` |
| `CANCELLED` | 해당 단계 / `CANCELLED` | 결과 없음 |
| `EXPIRED` | 해당 단계 / `EXPIRED` | 결과 없음 |

원본 계약에서 정해지지 않았거나 해석이 갈리는 지점이다. "원본 확인"은 원본에 이미 있는 규칙이고, "제안"과 "결정"만 새로 정할 내용이다.

| ID | 지점 | 원본 확인 / 제안 |
|---|---|---|
| S1 | 화자 확인 `decision: UNSURE`일 때 다음 상태 | 원본 확인: `UNSURE`는 `ABSTAINED`로 종료하고 `TEXT_ANALYSIS`를 만들지 않는다(`PUT /recordings/{id}/speaker-assignment` 설명). 결정: 확인 기한 전에 `CONFIRMED`로 다시 제출할 수 있게 할지 |
| S2 | `AUDIO_PREP`의 `ABSTAINED`와 `FAILED` 구분 | 디코딩 불가·손상은 `FAILED`(`INPUT_INVALID`, 재시도 없음), 무음·과도한 겹침처럼 처리는 됐지만 판단 자료가 부족하면 `ABSTAINED`로 둔다 |
| S3 | 재시도 가능한 실패의 대기 상태 | `retryable=true`인 `FAILED`는 `POST /jobs/{id}/retry` 전까지 `FAILED`로 두고, 재시도하면 해당 단계의 `QUEUED_*`로 돌아간다 |
| S4 | 완료 후 화자 재선택 | 원본 확인: 선택을 바꾸면 이전 결과와 집계를 무효화한다. 제안: 무효화한 결과와 주간 항목은 즉시 비노출하고 `QUEUED_TEXT`로 돌아가며, 새 결과가 나오기 전 결과 조회는 "결과 없음"으로 둔다 |
| S5 | 결과가 없는 상태에서 결과 조회 응답 | 존재를 숨길 필요가 없는 참여자에게는 409 `RESULT_NOT_READY`, 권한 없는 사용자에게는 404로 구분한다 |
| S6 | 확인 기한 만료 | `NEEDS_SPEAKER_CONFIRMATION`에서 `expires_at`이 지나면 `EXPIRED`로 종료하고, 원음 만료(`raw_expires_at`)와 구분해 표시한다 |

## 5. 오류 응답

원본 `Error`는 `code`(자유 문자열)·`message`·`request_id`·`retryable`이다. HTTP 상태 구분(401·403·404·409·413·415·422·429·503)은 원안을 유지하고 `code`를 다음 목록으로 고정할 것을 제안한다.

| HTTP | `code` 제안 | 의미 |
|---|---|---|
| 401 | `UNAUTHENTICATED` | 토큰 없음·만료·서명 오류 |
| 403 | `FORBIDDEN`, `CONSENT_REQUIRED` | 권한 없음, 현재 grant 부족 |
| 404 | `NOT_FOUND` | 없거나 존재를 숨겨야 하는 자원 |
| 409 | `VERSION_CONFLICT`, `LEASE_CONFLICT`, `IDEMPOTENCY_CONFLICT`, `RESULT_NOT_READY`, `INVALID_STATE` | 버전·lease·멱등 키·상태 충돌 |
| 413 | `PAYLOAD_TOO_LARGE` | 신고 크기 한도 초과(원본의 50MiB·10분은 제안 한도) |
| 415 | `UNSUPPORTED_MEDIA_TYPE` | 신고 형식 미지원 |
| 422 | `VALIDATION_FAILED`, `SEMANTIC_CHECK_FAILED` | 스키마 위반, 의미 검사 실패 |
| 429 | `RATE_LIMITED` | 요청·사용량 한도 |
| 503 | `TEMPORARILY_UNAVAILABLE` | 일시 장애 |

`message`에는 원문·전사·토큰·키·서명 URL을 넣지 않는다(AGENTS "민감 자료").

## 6. 계약 변경 기준

umsal-pwa #20의 "OpenAPI·JSON 예시·의미 검사·프런트 타입의 변경 기준" 제안이다. D1이 정해지면 원본 저장소에 같은 규칙을 둔다.

1. OpenAPI를 바꾸는 PR은 같은 PR에서 관련 JSON 예시(`contracts/examples`)와 정적 계약 검사를 갱신한다.
2. 필드 삭제·이름 변경·enum 값 삭제·필수 필드 추가는 호환성을 깨는 변경이다. `info.version`의 주 버전을 올리고, PR에 PWA 측 후속 작업을 적는다(AGENTS "환경과 계약").
3. 선택 필드·enum 값 추가는 부 버전을 올린다. PWA는 모르는 enum 값을 "알 수 없음"으로 표시하고 정상으로 해석하지 않는다.
4. FastAPI가 내보내는 `/openapi.json`과 원본 계약의 차이를 CI에서 비교한다. 차이가 있으면 구현 또는 계약 중 하나를 고친다.
5. 프런트 타입은 원본 계약에서 생성하고 손으로 고치지 않는다.
6. JSON Schema 검증은 필드·형식만 확인한다. 권한·경합·동의 같은 의미 검사는 서버 테스트로 따로 검증한다.

## 7. 다음 단계

1. D1·D5·D9를 먼저 정한다. D5는 #7 결정을 바꾸므로 사용자 확인 후 SPEC을, D9는 AGENTS를 갱신한다.
2. D2·D7을 반영한 계약을 이 저장소 `contracts/openapi.yaml`로 옮긴다(별도 이슈).
3. umsal-pwa #17의 발표 범위로 1차 operation(D8)을 정한 뒤 DB 스키마(umsal-pwa #23)와 인증(#24)을 시작한다.
