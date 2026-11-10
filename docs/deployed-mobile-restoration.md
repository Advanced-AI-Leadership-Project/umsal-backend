# 운영 코드·Cognito 설정을 Git으로 복원 (#77)

## 목적과 범위

2026-11-10 읽기 전용 감사에서 운영 API에만 사용자별 녹음 경로 처리가 있고, 운영 Cognito에만 Android 복귀 주소가 있는 것을 확인했다. 이 변경은 그 소스·설정을 검토 가능한 저장소 기준으로 복원한다. 실제 분석 모델, API 계약, DB 스키마, IAM 권한, 동의·공유 범위는 바꾸지 않는다.

| 경계 | 이번 변경 |
|---|---|
| API 소스 | 운영 ZIP의 `recordings.py`·`storage.py` 두 파일을 그대로 복원 |
| 회귀 검증 | 사용자별 신규 키, 기존 키 재발급·finalize, 정리 범위와 식별자 검증 |
| 인증 IaC | 웹 콜백은 유지하고 검토된 Android 콜백 1개를 선택적으로 선언 |
| 운영 배포 | 이 PR 생성 단계에서 수행하지 않음. 병합·재배포·실기기 검증은 각각 별도 기록 |

## 확인한 배포 아티팩트

조회 시각: 2026-11-10 15:54 KST. 프로젝트 `AI+X Final`, 리전 `ap-southeast-2`.

| 항목 | 값 |
|---|---|
| API Lambda | `umsal-backend-api-api` |
| 조회 당시 main | `a97815a97f4681354d09bd945b88d12a389e6087` |
| CloudFormation `ArtifactKey` | `lambda/1833e0bb03c0e385105a83cd7747830e32866153.zip` |
| Lambda 마지막 코드 변경 | `2026-11-09T09:42:37Z` |
| Lambda `CodeSha256`와 ZIP SHA-256(base64) | `PK6jlz+uaJKhrZg/sj/5HJWxww33EwPvA95E/CqLFcY=` — 일치 |
| 자체 패키지 비교 | 25개 파일 중 `recordings.py`·`storage.py`만 main과 다름. RDS 공개 CA 번들은 빌드 시 추가되는 파일 |

아티팩트 키의 40자리 값은 빌드에 사용한 식별자이며 ZIP 바이트의 SHA-256과 다르다. 복원 PR의 커밋과도 구분한다. 서명 다운로드 URL·ZIP·AWS 설정 원문·토큰·실제 사용자 자료는 저장소에 넣지 않는다. 복원 소스가 운영 ZIP과 같다는 사실은 그 ZIP의 당시 검사·독립 리뷰·실기기 성공을 증명하지 않는다.

## 녹음 경로와 호환성

- 새 업로드는 서버가 인증 토큰에서 찾은 내부 `user_id`로 `recordings/users/<user_id>/<recording_id>/source`를 만든다. 요청자가 S3 경로·업로더 ID를 선택하지 않는다.
- 녹음·사용자 ID는 영문자·숫자로 시작하고 영문자·숫자·`_`·`-`로 구성된 3~128자다. `/`, `..`, URL 인코딩 경로는 허용하지 않는다.
- URL 재발급은 녹음 ID로 경로를 다시 계산하지 않고 DB `recordings.s3_key`를 사용한다. 기존 `recordings/<recording_id>/source`의 열린 업로드도 같은 경로로 이어진다. 기존 객체를 이동하거나 DB 키를 일괄 변경하지 않는다.
- finalize는 DB의 키·요청한 S3 버전·크기·체크섬을 검증한 뒤 해당 버전을 고정한다. 이전 버전 정리는 저장된 키의 녹음 디렉터리 안에서만 수행한다. 사용자별 상위 디렉터리 전체를 정리하지 않는다.
- 권한 판단은 기존 참여자·동의 검사다. 사용자별 디렉터리 분리는 추가 저장 규칙이며 S3 자체의 사용자별 IAM 격리를 뜻하지 않는다.
- 이전 main 소스로 단순 롤백하면 새 사용자별 경로의 URL 재발급·정리가 달라질 수 있다. 장애 복구 때도 DB 키를 읽는 호환 처리를 유지한 버전을 사용한다.

관련 설계: [MVP 녹음·분석 흐름](mvp-recording-design.md). `stub-1` 분석 대역은 그대로이며, 실제 음성 분석·삭제 작업 전체가 구현되었다고 표시하지 않는다.

## Cognito 웹·Android 복귀 설정

`auth.yaml`의 `NativeCallbackUrl` 기본값은 빈 문자열이다. 새 웹 전용 환경에는 기존 웹 주소만 생긴다. 허용하는 비어 있지 않은 값은 `kr.ac.smu.umsal.inputprobe://oauth/callback` 하나다. 기존 public client, authorization code + PKCE, `openid`·`email` scope, client secret 없음, 웹 로그아웃 주소를 유지한다.

현재 데모 환경은 [auth-demo.parameters.json](../infra/aws/auth-demo.parameters.json)에 네 파라미터를 명시했다. 이 파일에는 공개 주소만 있으며 자격 증명이 없다.

| 파라미터 | 데모 값 |
|---|---|
| `DomainPrefix` | `umsal-family` |
| `CallbackUrl` | `https://d86lxzcp0rvt4.cloudfront.net/auth/callback` |
| `NativeCallbackUrl` | `kr.ac.smu.umsal.inputprobe://oauth/callback` |
| `LogoutUrl` | `https://d86lxzcp0rvt4.cloudfront.net/` |

**현재 데모에 최초 적용할 때는 이 파일을 명시한다.** 새 파라미터를 생략하면 빈 기본값이 적용되어 운영에 먼저 추가된 Android 콜백이 사라질 수 있다. 이후 갱신에서도 이 파일을 쓰거나 현재 스택 파라미터의 `UsePreviousValue`를 명시한다. 다른 환경은 검토한 별도 파라미터 파일을 사용한다.

### 배포자가 실행할 절차

아래는 재현용 절차이며 이번 복원 PR에서 실행한 배포 기록이 아니다. AWS MCP를 사용하는 에이전트는 같은 CloudFormation API 호출로 실행한다. 이 JSON은 `create-change-set --parameters` 형식이며 기존 `stage2_params.py`의 `deploy --parameter-overrides` 문자열 목록과 섞지 않는다.

```bash
# 읽기 전용 문법 검증
aws cloudformation validate-template --profile ai-x-final --region ap-southeast-2 \
  --template-body file://infra/aws/auth.yaml

# 리소스를 아직 변경하지 않는 검토용 change set
aws cloudformation create-change-set --profile ai-x-final --region ap-southeast-2 \
  --stack-name umsal-backend-auth --change-set-type UPDATE \
  --change-set-name restore-mobile-callback \
  --template-body file://infra/aws/auth.yaml \
  --parameters file://infra/aws/auth-demo.parameters.json \
  --tags Key=Project,Value=umsal-backend
aws cloudformation describe-change-set --profile ai-x-final --region ap-southeast-2 \
  --stack-name umsal-backend-auth --change-set-name restore-mobile-callback \
  --include-property-values
```

배포자는 `CREATE_COMPLETE`와 콜백 변경을 확인하고 사용자 풀·클라이언트 교체, 도메인·OAuth flow·scope·태그 삭제가 없는지 검토한다. 검토한 change set만 실행하고 스택 완료를 기다린다. 완료 뒤 `DescribeUserPoolClient`로 웹+Android 콜백과 웹 로그아웃 주소를 재조회한다. 소스 복원만으로 CloudFormation drift가 해소됐다고 보고하지 않는다.

API ZIP은 병합된 HEAD에서 [기존 빌더·배포 절차](aws-deployment.md#배포-절차)를 사용한다. auth 스택 갱신과 API ZIP 갱신은 별개 작업이다. `ArtifactKey`, `CodeSha256`, `/api/health/ready`, 인증 없는 `/api/me`의 401을 새 배포 기록에 함께 남긴다. 실제 DB·버킷의 사용자 자료를 회귀 테스트에 사용하지 않는다.

## 검증과 남은 확인

자동 회귀 검사는 합성 자료·격리 PostgreSQL·S3 대역을 사용한다. `tests/test_recordings.py`는 두 참여자의 경로 분리, 비참여자 거부, 기존/사용자별 키 재발급·finalize·다른 녹음 보호를 확인한다. `tests/test_storage.py`는 ID·키 검증을, `tests/test_auth_callbacks.py`는 웹 전용/Android 포함 설정을 확인한다. 전체 검사 명령은 `make verify`; 실제 실행 결과와 독립 리뷰 head는 PR에 기록한다.

- 실제 Android 브라우저 → 앱 OAuth 왕복, 합성 음성 파일 업로드, 설치한 PWA의 기존 로그인 동작은 [후속 실기기 검증 #79](https://github.com/Advanced-AI-Leadership-Project/umsal-backend/issues/79)에서 별도로 확인한다.
- 실제 음성 모델 정확도·통화 자동 수집·푸시 수신은 이 복원 범위가 아니다.
- GitHub 요금·예산·러너 워크플로 설정은 변경하지 않는다.
