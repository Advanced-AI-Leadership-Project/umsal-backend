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
인스턴스 프로파일을 만든다. DB 마이그레이션 0007을 먼저 적용한다. 워커를 기동할 때는
잠금 파일에서 설치한 Linux 의존성, Python 3.12, 코드와 고정 모델 파일을 포함한
번들을 비공개 artifact S3 키로 지정한다. `run.py --help`에서 필요한 파라미터를 확인한다.
EC2에는 공인 IP·인바운드 포트를 할당하지 않고, 프로세스 외부 제한 시간과 종료 시
인스턴스 종료를 설정한다. IAM을 통해 SQS와 전용 Lambda만 호출하며 원음은 claim이
발급한 고정 버전 URL로 받는다. DB 직접 연결은 없다.

요청 처리는 접수와 outbox를 같은 트랜잭션으로 기록한다. 발행 작업은 ID만 큐에
전달한다. Worker API는 동의 스냅샷, 현재 작업, 화자 버전, 리스를 잠금 아래 검증한다.
성공 또는 영속화된 종료 응답을 받은 뒤 큐 메시지를 지운다. 종료된 워커를 자동으로
다시 기동하는 기능은 별도다. AWS에서 상시 남는 인터페이스 엔드포인트는 시간 요금이
발생하므로 사용 계획에 맞춰 관리한다.
