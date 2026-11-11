# CPU 음성·텍스트 분석

운영 중인 S3, PostgreSQL, 결과 DynamoDB를 재사용한다. API 스택의
`CpuRealAudioEnabled=true`는 양쪽 계정의 현재 분석 동의를 확인한 녹음을
`cpu-2` 작업으로 접수한다. WAV·MP3·M4A를 지원하며 파일은 최대 50 MiB,
실제 디코딩한 길이는 최대 10분이다. CPU에서 16 kHz 모노로 변환한 뒤
화자를 구분한다. 사용자가 로컬 원본을 듣고 자기 목소리를 확인한 뒤에만
그 화자의 구간을 전사·텍스트 분석한다. 겹쳐 말한 구간은 근거에서 제외한다.
실제 파일과 합성 파일의 `demo` 출처를 결과까지 보존한다.

기존 `cpu-1`의 지정된 합성 계정·60초 이하 16 kHz 스테레오 WAV 작업과
결과도 처리한다. 실제 녹음은 기능이 꺼져 있으면 접수를 거절한다.

모델 파일은 Git에 포함하지 않는다. 모델 경로는 아래 리비전의 자산을 사용한다.

| 경로 | 모델 리비전과 파일 |
|---|---|
| `whisper/` | [Systran/faster-whisper-base](https://huggingface.co/Systran/faster-whisper-base/tree/ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66)의 config.json, tokenizer.json, vocabulary.txt, model.bin |
| `e5/` | [Xenova/multilingual-e5-small](https://huggingface.co/Xenova/multilingual-e5-small/tree/761b726dd34fb83930e26aab4e9ac3899aa1fa78)의 tokenizer.json 및 onnx/model_quantized.onnx |

`diarization/segmentation.onnx`는 sherpa-onnx의
[pyannote segmentation 3.0 변환 자산](https://github.com/k2-fsa/sherpa-onnx/releases/tag/speaker-segmentation-models)이며,
`diarization/embedding.onnx`는
[3D-Speaker ERes2Net Base](https://github.com/k2-fsa/sherpa-onnx/releases/tag/speaker-recongition-models)의
`3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx`다.
배포 자산의 SHA-256과 모델 라이선스를 번들에 함께 보관한다. sherpa-onnx 2.0.1을 사용한다.

| 파일 | SHA-256 |
|---|---|
| segmentation.onnx | 220ad67ca923bef2fa91f2390c786097bf305bceb5e261d4af67b38e938e1079 |
| embedding.onnx | 1a331345f04805badbb495c775a6ddffcdd1a732567d5ec8b3d5749e3c7a5e4b |

`models.py`는 음성 활동 분할·화자 임베딩·군집화로 익명 화자 구간을 만든다.
인물의 신원과 화자 번호는 사용자가 원본을 듣고 확인한다. 텍스트 단계는 오디오
단계에서 확정한 구간을 Worker API에서 받아 다시 화자를 나누는 과정 없이 사용한다.
Whisper Base CPU INT8로 해당 구간을 전사하고 E5 임베딩으로 걱정·수면·식사
관련 단서를 점수화한다. 점수가 불충분하면 단서 없음으로 표시한다.
전사·화자 분리와 분류에 오류가 있을 수 있으므로 원음 확인과 실제 자료 평가가 필요하다.

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
