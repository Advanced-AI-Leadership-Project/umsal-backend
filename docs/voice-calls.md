# 엄살 가족 음성 통화

## 가족 간 1:1 음성 통화 (#89)

로그인한 두 가족이 앱을 열어 둔 상태에서 전화 걸기·수락·거절·음소거·종료를 제공한다. 활성 가족 연결만 허용하고 분석 grant는 통화 수락과 별개다. API 9개는 `calls.py`, 호출 계약은 v0.5.0이다. 실제 전송은 WebRTC 오디오와 Sydney의 인증된 coturn 릴레이가 담당한다. 미디어는 서버 파일·분석 큐로 저장하지 않는다. 녹음 파일 접수·양쪽 분석 동의·화자 확인은 기존 과정으로 별도 진행한다.

통화는 사용자별 한 건이다. 사용자 행을 정렬해 잠근 뒤 중복을 검사하고, 수락은 첫 앱 세션 하나에만 부여한다. 전화 요청은 60초, 참여자 유지 신호는 90초, 통화는 최대 30분이다. 가족 연결 해제는 다음 상태/신호 조회에서 통화를 만료시킨다. 인증·참여자·활성 상태·세션 소유를 매 요청 확인한다.

상태와 시각은 RDS `voice_calls`, 네트워크 신호 SDP는 전용 DynamoDB에 5분 논리 만료로 둔다. DynamoDB TTL 실제 삭제는 비동기다. 종료/만료된 통화의 신호는 즉시 조회 차단한다. 릴레이 공유 비밀은 Secrets Manager에만 두며 IAM 전용 Lambda가 최대 1시간의 개별 자격을 발급한다. 브라우저는 relay-only로 연결한다.

`infra/aws/voice-calls.yaml`은 별도 공개 VPC의 t3.micro 1대·고정 IPv4·TURN 보안 그룹·전용 DDB·자격 발급 Lambda를 정의한다. EC2와 IPv4는 통화가 없어도 유지 비용이 발생한다. 모델 CPU 워커의 자동 종료와 서로 다른 자원이다. AWS Free new experience에서 Chime SDK는 지원 대상 밖이므로 요금제 업그레이드 없이 구성했다. 기존 API VPC의 Lambda 엔드포인트 정책에는 자격 발급 함수 하나만 추가한다. GitHub 예산은 변경하지 않는다.

배포 순서는 API/마이그레이션 아티팩트 게시 → 통화 스택 생성 → CPU 스택의 선택 `CallRelayFunctionArn` 연결 → RDS 0009 적용 → API 스택의 `CallRelayFunctionArn`, `CallSignalsTableName`, `CallLambdaEndpoint` 연결이다. 자격 발급 함수의 공개 URL은 만들지 않는다. AWS 검증 전에는 로컬 검사 통과를 실제 단말 통화 성공으로 보고하지 않는다.

지원 범위는 전경 PWA다. 화면 잠금·앱 종료·백그라운드 수신 및 일반 전화번호 발신은 이 기능에 포함되지 않는다. 양쪽 마이크 권한과 실제 네트워크 간 연결은 별도 단말 확인이 필요하다.

## 릴레이 실행 버전 (#92)

Ubuntu 24.04 기본 coturn 4.6.1 대신 [공식 Docker 릴리스 4.18.0-r0](https://github.com/coturn/coturn/releases/tag/docker/4.18.0-r0)의 linux/amd64 이미지 `sha256:59af4221a2c45c1195a1a4a865542b3ea744be1dd8e228d11bde87c6631081cf`로 고정한다. 기본 패키지는 비활성화하고 `umsal-turn.service`가 고정 컨테이너를 실행한다. 비root UID·capability 제거·새 권한 금지·읽기 전용 루트·설정 파일 읽기 전용·8MiB 임시 디렉터리를 적용한다. EC2 부팅·재시작 후 systemd가 복구한다. 기존 인스턴스는 같은 UserData 셸을 SSM으로 적용한 뒤 CloudFormation 구성도 맞춘다.

자격 발급 Lambda는 SDK 초기화와 Secrets Manager 호출을 제한 시간 안에 처리하도록 512MiB를 할당한다. 실행 제한 8초·동시성 2는 유지한다.
