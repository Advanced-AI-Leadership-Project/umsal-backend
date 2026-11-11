# CPU 음성·텍스트 시연

운영 중인 S3, PostgreSQL, 결과 DynamoDB를 재사용한다. 테스트 계정 두 명을 API 스택의
`CpuPilotUsers`에 지정한 뒤 양쪽이 동의한 합성 파일만 `cpu-1` 작업으로 처리한다.
입력은 두 화자를 각각 한 채널에 넣은 16 kHz, 16비트 PCM 스테레오 WAV, 최대 60초다.
단일 채널 화자 분리나 일반 통화 정확도는 이 시연의 범위 밖이다.

모델 파일은 Git에 포함하지 않는다. 모델 경로는 아래 리비전의 자산을 사용한다.

| 경로 | 모델 리비전과 파일 |
|---|---|
| `whisper/` | [Systran/faster-whisper-base](https://huggingface.co/Systran/faster-whisper-base/tree/ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66)의 config.json, tokenizer.json, vocabulary.txt, model.bin |
| `e5/` | [Xenova/multilingual-e5-small](https://huggingface.co/Xenova/multilingual-e5-small/tree/761b726dd34fb83930e26aab4e9ac3899aa1fa78)의 tokenizer.json 및 onnx/model_quantized.onnx |

`models.py`는 두 채널을 각각 Whisper CPU INT8로 전사해 후보 구간을 만든다. 확인된
화자의 전사에 E5 임베딩을 적용해 걱정·수면·식사 관련 표현을 점수화한다. 단서의 근거는
선택한 화자의 실제 전사 구간이며, 점수가 불충분하면 단서 없음으로 표시한다.

`infra/aws/cpu-analysis.yaml`은 발행 Lambda, SQS/DLQ, Worker API, 비공개 엔드포인트,
인스턴스 프로파일, Launch Template, Auto Scaling 그룹과 용량 제어 Lambda를 만든다.
DB 마이그레이션 0007과 0008을 먼저 적용한다. 워커 배포에는
잠금 파일에서 설치한 Linux 의존성, Python 3.12, 코드와 고정 모델 파일을 포함한
번들을 비공개 artifact S3 키로 지정한다. `run.py --help`에서 필요한 파라미터를 확인한다.
`WorkerAmi`에 Sydney의 Amazon Linux 2023 x86_64 AMI를,
`WorkerArtifactSha256`에 번들 SHA-256을 지정한다. Launch Template은 Lambda 엔드포인트의
전용 DNS를 `AWS_ENDPOINT_URL_LAMBDA`로 주입하고 번들의 체크섬을 확인한다.
EC2에는 공인 IP·인바운드 포트를 할당하지 않고, 프로세스 외부 제한 시간과 종료 시
인스턴스 종료를 설정한다. IAM을 통해 SQS와 전용 Lambda만 호출하며 원음은 claim이
발급한 고정 버전 URL로 받는다. DB 직접 연결은 없다.

요청 처리는 접수와 outbox를 같은 트랜잭션으로 기록한다. 발행 작업은 ID만 큐에
전달한다. Worker API는 동의 스냅샷, 현재 작업, 화자 버전, 리스를 잠금 아래 검증한다.
성공 또는 영속화된 종료 응답을 받은 뒤 큐 메시지를 지운다.

용량 제어 Lambda는 1분마다 큐의 대기·처리 중·지연 메시지를 확인한다. 작업이 있으면
Auto Scaling의 목표 용량을 1로, 비면 0으로 설정한다. 그룹의 최소 용량은 0, 최대는 1이다.
신규 워커는 시작부터 축소 보호를 받으므로 큐 집계가 일시적으로 0이어도 처리 중에
축소되지 않는다. 워커가 60초간 새 메시지를 받지 못하면 자신의 IAM 역할로 제어
Lambda에 종료를 요청한다. 제어기는 큐를 다시 확인해 작업이 있으면 계속 실행하도록
응답하고, 비면 해당 그룹에 속한 요청 인스턴스의 보호만 해제한다. 인스턴스가 종료될 때
루트 EBS도 삭제된다. 화자 확인을 기다리는 동안은 분석 대기 작업이 없으므로 종료할 수
있고, 사용자가 화자를 확정하면 텍스트 작업을 위해 자동 기동한다.

워커의 실패·종료 뒤에도 SQS의 메시지와 기존 claim·리스 검사를 통해 재처리한다.
프로세스에는 15분 제한이 있고 부팅 때 20분 종료 타이머도 설치한다. 용량 제어 Lambda는
원음·DB 접근 권한 없이 지정한 큐 집계와 지정한 그룹만 제어한다. 배포 후 `CpuGroup`의
최소 0·최대 1과 신규 인스턴스 보호 설정을 유지한다. AWS에서 상시 남는 인터페이스
엔드포인트는 시간 요금이 발생한다.
