# PWA 업무 API 계약 정합성 초안

2026-11-04 · 이슈 #9 · 대응: umsal-pwa #20 (DEV-04)

**초안이다.** 업무 API의 구현 여부는 2절 표를 따른다(일부만 구현). "확정"으로 표시한 D1·D2·D3·D5·D6·D7·D9·D10·D11·D12를 제외한 "결정 필요" 항목은 사용자·팀 확인 전까지 확정이 아니다. 계약 원본은 2026-11-05부터 이 저장소 [`contracts/`](../contracts/openapi.yaml)(v0.4.0, 이관 #25)이고, 확정 결정 중 D2·D3·D7·D10을 반영했다. 이 문서는 원안과의 차이와 결정 기록이다.

## 1. 기준 원본

| 항목 | 값 |
|---|---|
| 목표 계약(현재 원본) | 이 저장소 [`contracts/openapi.yaml`](../contracts/openapi.yaml) v0.5.0, 33 paths / 41 operations. v0.2.0 대비 `servers: /api`, `/health/live`·`/health/ready`(D2), CognitoBearer 설명(D3), `GET /connections`(D7), 연결별 호칭 `PUT /connections/{id}/label`(D10, 0.4.0) 추가. 정적 검사는 `tests/test_contracts.py` |
| 이관 전 원안 | umsal-pwa [`contracts/openapi.yaml`](https://github.com/Advanced-AI-Leadership-Project/umsal-pwa/blob/e54088b9489d67ab3269cb626c860d3b0a4bf79e/contracts/openapi.yaml) v0.2.0, 23 paths / 28 operations |
| 설계 설명 | umsal-pwa [`docs/design/09_API_계약_및_데이터설계.md`](https://github.com/Advanced-AI-Leadership-Project/umsal-pwa/blob/e54088b9489d67ab3269cb626c860d3b0a4bf79e/docs/design/09_API_%EA%B3%84%EC%95%BD_%EB%B0%8F_%EB%8D%B0%EC%9D%B4%ED%84%B0%EC%84%A4%EA%B3%84.md) (2026-10-27 Android 원안) |
| 고정 commit | `e54088b9489d67ab3269cb626c860d3b0a4bf79e` (umsal-pwa main, 2026-11-04 조회) |
| 저장 계층 | 이 저장소 [SPEC](../SPEC.md)의 "저장 계층" (#7) |

## 2. 실행 API와 목표 계약의 차이

| 구분 | 경로 | 위치 | 상태 |
|---|---|---|---|
| 실행 | `GET /health` | umsal-backend | 구현. 배포 확인용, 프로세스 생존만 응답 |
| 실행 | `GET /api/health/live`, `GET /api/health/ready` | umsal-backend | 구현(D2, #17). ready는 PostgreSQL·DynamoDB 확인을 실행하며 배포 환경은 3단계(2026-11-05)부터 200 |
| 실행 | `GET /api/health/live`, `GET /api/health/ready` | umsal-pwa `services/api` | 구현. ready는 로컬 PostgreSQL `SELECT 1` |
| 실행 | `GET /api/push/config`, `POST·DELETE /api/push/subscription`, `POST /api/push/test` | umsal-pwa `services/api` | 테스트 채널만 구현(기본 비활성) |
| 목표 | 사용자 operation 24개(`/me`~`/devices/{id}`, 19 paths) | umsal-backend (D1 확정) | `getMe`·`listConnections`·`getConnection`(#29), `createInvitation`·`acceptInvitation`(#31), `getConsents`·`updateConsents`(#34), `disconnect`(#36), `setConnectionLabel`(#62) 구현, 나머지 미구현 |
| 목표 | worker operation 4개(`/internal/worker/jobs/{id}/claim·heartbeat·complete·fail`) | umsal-backend (D1 확정) | 미구현 |

PWA 화면은 같은 출처의 `/api/health/{live,ready}`를 호출하고 `service == "api"`, ready의 `checks.database == "ok"`를 확인한다. 이 백엔드도 D2에 따라 같은 경로와 응답 모양을 제공한다. 3단계 배포(2026-11-05) 뒤 배포 환경의 ready는 200 `ready`이고, 2026-11-08부터 PWA CloudFront의 같은 출처 `/api/health/*`도 이 백엔드로 간다(umsal-pwa #115).

## 3. 결정 필요 항목

D1·D2·D3·D5·D6·D7·D8·D9·D10·D11·D12는 확정됐고, 나머지(D4)는 미확정이다.

| ID | 항목 | 현재 상태 | 권장안 |
|---|---|---|---|
| D1 | 업무 API를 구현할 저장소와 계약 원본 위치 | 목표 계약은 PWA 저장소에 있고, PWA 저장소에도 `services/api`(FastAPI)가 있다 | **확정(2026-11-04, 사용자 결정):** 업무 API는 umsal-backend가 구현한다. 후속: 계약 원본을 이 저장소 `contracts/openapi.yaml`로 옮기고(별도 이슈), PWA는 이를 읽어 타입을 만든다. PWA `services/api`는 health·테스트 push 데모로 한정하는 것을 권장하며, 그 범위 조정은 umsal-pwa에서 정한다 |
| D2 | 기본 경로와 health | 백엔드 `/health`, PWA `/api/health/*` | 백엔드가 `/api` 접두사 아래에서 `GET /api/health/live`·`GET /api/health/ready`를 PWA와 같은 응답 모양으로 제공한다. ready의 `checks`는 Aurora·DynamoDB를 따로 표시한다. 기존 `/health`는 배포 확인용으로 유지할지 함께 정한다. **확정·구현(2026-11-05, 사용자 결정, #17):** 권장안대로 구현했다. `checks.aurora`·`checks.dynamodb`는 `ok`·`unavailable`·`not_configured` 중 하나이고, PWA 호환용 `checks.database`는 둘 다 `ok`일 때만 `ok`다. 하나라도 아니면 503 `not_ready`. 기존 `/health`는 배포 확인용으로 유지한다 |
| D3 | 사용자 인증 | 원안은 Cognito access token(Bearer) | 원안 유지. PWA는 Cognito 관리형 로그인 + 인가 코드·PKCE를 쓴다. 토큰 저장 위치(메모리/세션)는 umsal-pwa #24·#25에서 정한다. **확정(2026-11-05, 사용자 결정, #21):** 원안대로 Cognito access token(Bearer)을 쓴다. 백엔드는 API Gateway에만 맡기지 않고 앱에서 직접 검증한다: RS256 서명(사용자 풀 JWKS, `kid`), `iss`, `exp`·`iat`(30초 여유), `token_use == access`(ID token 거부), 허용 `client_id`. 실패는 401 `UNAUTHENTICATED`, 설정 없음·JWKS 조회 실패는 503 `TEMPORARILY_UNAVAILABLE`로 닫힌 쪽으로 실패한다. 사용자 풀 생성·배포 연결은 2단계다. Lambda를 VPC에 넣으면 JWKS 조회에 외부 연결이 필요하므로 2단계에서는 NAT 대신 JWKS를 배포 때 고정한다(#40, 키 교체 시 재배포) |
| D4 | 알림 기기 등록 | `POST /devices`가 `platform: ANDROID`, FCM `push_token` 전제 | PWA는 Web Push 구독(`endpoint`, `keys.p256dh`, `keys.auth`)을 쓴다. `DeviceRegistration`을 웹 구독 형태로 바꾸거나 별도 `/push-subscriptions`로 분리한다. 알림 방식 결정(umsal-pwa #52) 뒤 확정한다 |
| D5 | 저장 계층 반영 | 원안은 PostgreSQL 한 DB에서 job·동의·recording을 잠그고 결과·상태·outbox를 한 트랜잭션으로 commit한다(설계 문서 "기계 작업 계약" 8번) | **확정(2026-11-05, 사용자 결정):** 상태와 fencing에 관여하는 `recordings`·`analysis_jobs`·`speaker_assignments`·`deletion_jobs`·`outbox`·`idempotency_keys`는 Aurora에 두어 grant와 같은 트랜잭션으로 검사한다. DynamoDB에는 `utterances`·`observations`·결과 뷰·주간 집계 항목처럼 양이 많고 키로 읽는 자료를 둔다. #7의 "분석 작업 상태는 DynamoDB" 결정을 바꾸며 SPEC "저장 계층"에 반영했다. 이 배치로도 결과 본문(DynamoDB·S3) 쓰기는 Aurora 트랜잭션에 들어가지 않으므로, SPEC의 저장소 간 철회 경합은 그대로 남는다. 결과 쓰기 순서와 노출 조건은 별도로 설계한다 |
| D6 | S3 버전 고정 | 계약의 `InputRef`·`ArtifactRef`·`Snapshot`이 S3 `version_id`로 입력을 고정한다 | **확정(2026-11-05, 사용자 결정):** 원음 버킷 버전 관리를 켜 계약의 `version_id` 입력 고정을 유지한다. 삭제 시 모든 버전을 명시적으로 지우고, 이전 버전은 수명 주기 규칙으로 짧은 기한 안에 자동 만료한다. SPEC "저장소를 넘나드는 불변조건"에 반영했다 |
| D7 | 관계 목록 조회 | `GET /me`는 `Actor`만 반환하고 관계 목록 경로가 없다 | 가족 화면을 위해 `GET /connections`(내 관계 목록)를 추가하거나 `/me` 응답에 관계 요약을 넣는다. **확정(2026-11-05, 사용자 결정, #21):** `GET /connections`를 추가한다. `/me`는 `Actor`만 반환한다. 계약 v0.3.0에 `listConnections`·`ConnectionPage`로 반영했다(#25) |
| D8 | MVP 범위 | 28개 operation 전체가 P0 후보 | **확정(2026-11-08, 사용자 결정, #68):** 1차 범위는 음성 파일 1건 업로드 → 분석(처음엔 대역, 응답 `demo: true`) → 본인 결과 → 동의한 가족 공유 → 삭제. 1차 operation 9개(`createUpload`·`finalizeUpload`·`getJob`·`listRecordings`·`getRecording`·`assignSpeaker`·`getResult`·`deleteRecording`·`getDeletionStatus`), 나머지는 2차. 계약에서 operation은 지우지 않는다. 설계는 [MVP 녹음·분석 흐름 설계](mvp-recording-design.md) |
| D9 | 작업자의 S3 산출물 쓰기 | 원안은 작업자가 결과 artifact를 S3의 job 전용 출력 prefix에 직접 쓰고, complete에서 참조만 제출한다(설계 문서 "기계 작업 계약" 3·5번). AGENTS의 "작업자는 Aurora·DynamoDB·S3에 직접 쓰지 않고 API를 거친다"(#7, PR #8에서 추가)와 충돌한다 | **확정(2026-11-05, 사용자 결정):** 원안 구조를 유지하고 AGENTS 문구를 좁혔다. 작업자는 Aurora·DynamoDB와 원음 key에는 쓰지 않고, S3에는 claim이 허용한 job 전용 출력 prefix에만 쓴다. 그 산출물은 API가 검증·commit하기 전까지 결과로 취급하지 않는다. 강한 job별 격리가 필요하면 API가 출력용 presigned PUT URL을 발급하는 방식을 검토한다. AGENTS 제품 불변조건에 반영했다 |
| D10 | 표시 이름 출처 | 계약 `Actor.display_name`은 필수 문자열이지만 값을 설정하는 operation이 없고, Cognito access token에는 이름이 없다 | **확정(2026-11-08, 사용자 결정, #62):** 연결마다 각 참여자가 상대를 부르는 호칭(`Connection.label`, 요청자에게만 보임)을 쓴다. 초대 생성·수락 본문의 선택 `label`과 `PUT /connections/{id}/label`(`setConnectionLabel`)로 정한다. 계약 0.4.0. `Actor.display_name`은 계속 빈 문자열이며 프로필 이름은 필요해지면 별도로 정한다 |
| D11 | 초대 유효 기간·코드 | 계약은 `expires_at`만 있고 기간·코드 형식이 없다 | **확정(2026-11-05, 사용자 승인, #31):** 유효 24시간, 코드 10자(Crockford base32, 대소문자·하이픈·공백 무시), 수락에는 초대 ID와 코드가 모두 필요. 코드는 SHA-256만 저장한다. 다만 같은 `Idempotency-Key` 재전송에 같은 응답을 주려고 원문 코드가 멱등 기록에 남는다. 기록은 24시간 뒤 만료되어 재전송에 쓰이지 않고 같은 사용자의 다음 요청 때 지워지지만, 이를 막는 정리 작업 `umsal_backend.maintenance`(#36)가 모든 사용자의 만료 기록과 만료된 미수락 초대를 지운다. 이 작업의 주기 실행(EventBridge Scheduler → Lambda)은 2단계 배포에서 연결하며, 그 전에는 수동 실행해야 한다. 수락은 ACTIVE 관계를 만들고 grant 행은 전부 거부 상태로만 만들며 동의로 보지 않는다(FR-02, D12) |
| D12 | 동의 기본값·계산 규칙 | 계약은 `ConsentView.version ≥ 1`, `expected_version ≥ 0`, 서버 계산 `both_analysis_allowed`·`verification_preview_allowed`만 정한다 | **확정(2026-11-05, 사용자 승인, #34):** 관계가 생길 때 양쪽 방향 모두 전부 거부·버전 1 행을 만든다(초대 수락은 동의가 아님). `both_analysis_allowed`와 `verification_preview_allowed`는 양쪽이 모두 허용하고 관계가 ACTIVE일 때만 참이다. 해제된 관계의 동의는 바꿀 수 없다(409). 기본 행이 없는 관계(직접 삽입·이전 데이터)는 동의 API 첫 접근 때 기본 거부 행을 만들므로 GET도 한 번 쓰기를 할 수 있다. 관계 해제(#36)는 관계를 DISCONNECTED·version+1로 바꾸고 양방향 grant를 전부 거부·version+1로 되돌린다(행은 남김). 이미 해제된 관계의 해제 요청은 상태 변화 없이 같은 202다. 보관 자료 삭제는 하지 않는다. 동의 요청의 boolean·정수는 엄격히 검사해 `"yes"`·`1` 같은 값을 거절한다. 분석 동의 철회 시 진행 중 처리 취소는 녹음·작업 구현 때 연결한다 |

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

umsal-pwa #20의 "OpenAPI·JSON 예시·의미 검사·프런트 타입의 변경 기준" 제안이다. D1에 따라 계약 원본을 이 저장소로 옮길 때 같은 규칙을 함께 둔다.

1. OpenAPI를 바꾸는 PR은 같은 PR에서 관련 JSON 예시(`contracts/examples`)와 정적 계약 검사를 갱신한다.
2. 필드 삭제·이름 변경·enum 값 삭제·필수 필드 추가는 호환성을 깨는 변경이다. `info.version`의 주 버전을 올리고, PR에 PWA 측 후속 작업을 적는다(AGENTS "환경과 계약").
3. 선택 필드·enum 값 추가는 부 버전을 올린다. PWA는 모르는 enum 값을 "알 수 없음"으로 표시하고 정상으로 해석하지 않는다.
4. FastAPI가 생성하는 스키마(`app.openapi()`. HTTP `/openapi.json`은 로컬에서만 공개, #66)와 원본 계약의 차이를 CI에서 비교한다. 차이가 있으면 구현 또는 계약 중 하나를 고친다. **구현(#36):** `tests/test_contract_parity.py`가 `/api` 아래 구현 operation의 operationId·메서드·경로·성공 코드·`Idempotency-Key` 필수 여부를 계약과 비교하고, 계약에 없는 operation을 거부한다. 요청·응답 본문 스키마는 엔드포인트 테스트의 `contract_schema` 검사로 확인한다
5. 프런트 타입은 원본 계약에서 생성하고 손으로 고치지 않는다.
6. JSON Schema 검증은 필드·형식만 확인한다. 권한·경합·동의 같은 의미 검사는 서버 테스트로 따로 검증한다.

## 7. 다음 단계

1. D1·D2·D3·D5·D6·D7·D9는 확정했다(D2·D3 검증은 구현, D5·D6은 SPEC "저장 계층", D9는 AGENTS 제품 불변조건에 반영). D4는 umsal-pwa #52, D8은 #17 결정 뒤 정한다.
2. 계약을 이 저장소 `contracts/`로 옮기고 D2·D3·D7을 반영했다(#25). umsal-pwa 쪽 사본 정리(삭제·참조 전환·프런트 타입 생성)는 PWA 팀이 정한다.
3. umsal-pwa #17의 발표 범위로 1차 operation(D8)을 정한 뒤 DB 스키마(umsal-pwa #23)와 인증(#24)을 시작한다.

## D13 가족 음성 통화

#89에서 전경 WebRTC 1:1 통화 operation 9개와 요청/응답 예시 2개를 계약 v0.5.0에 추가했다. 기존 operation과 오류 구조를 유지한다. `createCall`만 Idempotency-Key 필수, accept/heartbeat/PUT signal/DELETE 종료는 같은 상태에 대한 재요청을 허용한다. 다른 세션은 CALL_OTHER_DEVICE, 종료는 CALL_ENDED, 중복은 CALL_BUSY다. 프런트엔드 #139가 이 계약을 연결한다. 자세한 저장·만료 정책은 [통화 설명서](voice-calls.md)를 따른다.
