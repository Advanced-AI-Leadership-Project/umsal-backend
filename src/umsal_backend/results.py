"""Result bodies in DynamoDB (docs/mvp-recording-design.md 5, 6; MVP stage 4, #75).

One item per recording and speaker-assignment version: ``pk = REC#<recording_id>``,
``sk = RESULT#<version>``, the body as a JSON string. The API role may touch only
``REC#*`` partition keys (api.yaml ``RecordingStorage``).

The table never decides what may be shown: PostgreSQL holds consent, deletion and the
current assignment version, and ``getResult`` filters every read by them. An item
written for an older version, or left behind by a failed commit, is never read.
"""

from __future__ import annotations

import json
import os
from typing import Any, Protocol

from umsal_backend.storage import StorageUnavailable

CONNECT_TIMEOUT_SECONDS = 2
READ_TIMEOUT_SECONDS = 3


def result_key(recording_id: str, version: int) -> dict[str, dict[str, str]]:
    return {"pk": {"S": f"REC#{recording_id}"}, "sk": {"S": f"RESULT#{version}"}}


class ResultStore(Protocol):
    def put(self, recording_id: str, version: int, body: dict[str, Any]) -> None: ...

    def get(self, recording_id: str, version: int) -> dict[str, Any] | None: ...

    def delete(self, recording_id: str, version: int) -> None: ...


class DynamoResultStore:
    """Result items through the DynamoDB gateway endpoint; one attempt, short timeouts."""

    def __init__(self, table: str, region: str, client: Any | None = None) -> None:
        self.table = table
        self.region = region
        self._client = client

    def client(self) -> Any:
        if self._client is None:  # created on first use and reused by a warm Lambda
            import boto3  # deferred: only needed on AWS
            from botocore.config import Config

            self._client = boto3.client(
                "dynamodb",
                region_name=self.region,
                config=Config(
                    connect_timeout=CONNECT_TIMEOUT_SECONDS,
                    read_timeout=READ_TIMEOUT_SECONDS,
                    retries={"total_max_attempts": 1, "mode": "standard"},
                ),
            )
        return self._client

    def _call(self, name: str, **kwargs: Any) -> dict[str, Any]:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            return getattr(self.client(), name)(TableName=self.table, **kwargs)
        except (BotoCoreError, ClientError) as error:
            # Throttling, a timeout, a missing permission: the caller decides between a
            # 503 (request path) and a re-queue (analysis drain).
            raise StorageUnavailable from error

    def put(self, recording_id: str, version: int, body: dict[str, Any]) -> None:
        item = {**result_key(recording_id, version), "body": {"S": json.dumps(body)}}
        self._call("put_item", Item=item)

    def get(self, recording_id: str, version: int) -> dict[str, Any] | None:
        found = self._call(
            "get_item", Key=result_key(recording_id, version), ConsistentRead=True
        ).get("Item")
        if found is None:
            return None
        return json.loads(found["body"]["S"])

    def delete(self, recording_id: str, version: int) -> None:
        self._call("delete_item", Key=result_key(recording_id, version))


def result_store_from_env() -> DynamoResultStore | None:
    table = os.environ.get("DYNAMODB_TABLE", "").strip()
    if not table:
        return None
    # Same requirement as the readiness probe (dynamo.py), which fails at startup first.
    return DynamoResultStore(table, os.environ.get("AWS_REGION", "").strip())
