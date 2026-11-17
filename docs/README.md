# 백엔드 문서 색인

| 확인할 내용 | 기준 문서 |
|---|---|
| 실행·DB·검사 | [README](../README.md) |
| 현재 구현·저장 계층·후속 기능 | [SPEC](../SPEC.md) |
| 작업 규칙·PR·자동 병합 | [AGENTS](../AGENTS.md), [공동 개발](ai-collaboration.md), [검증 하네스](verification-harness.md) |
| 제품 계약·공개 HTTP와 IAM 내부 경로 | [원본 OpenAPI](../contracts/openapi.yaml), [API 대조/결정 기록](api-contract.md) |
| 실제 CPU 분석·모노 화자 확인·자동 기동 | [CPU 워커](../workers/cpu/README.md) |
| 전경 1:1 통화·제약 | [통화](voice-calls.md) |
| 배포 명령·과거 확인 증거 | [AWS 배포](aws-deployment.md), [복원 당시 소스/설정](deployed-mobile-restoration.md) |
| 초기 단계별 설계 | [2단계](aws-stage2-design.md), [3단계](aws-stage3-design.md), [MVP 접수](mvp-recording-design.md) |
| 구현 과정 학습 | [STUDY](../STUDY.MD) |

날짜·commit이 고정된 설계/배포 기록은 당시 증거다. 현재 기능은 SPEC·계약 대조와 실제 main 소스를 확인하고, 현재 AWS 설정·DB 적용 버전·모델 품질·실기기 상태는 각각 검증한다. 오래된 보고서의 `stub-1` 설명으로 현재 CPU 워커를 미구현으로 해석하지 않는다.
