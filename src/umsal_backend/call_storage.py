"""Short-lived call signaling and authenticated TURN credentials, never audio."""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Protocol

from umsal_backend.errors import ApiError


class CallServices(Protocol):
    def put_signal(self, call_id: str, side: str, sdp: str) -> None: ...

    def get_signal(self, call_id: str, side: str) -> str | None: ...

    def ice_servers(self, call_id: str, side: str) -> dict: ...


class AwsCallServices:
    def __init__(self, table: str, function: str, region: str) -> None:
        self.table, self.function, self.region = table, function, region
        self._clients: dict[str, Any] = {}

    def client(self, service: str) -> Any:
        if service not in self._clients:
            import boto3
            from botocore.config import Config

            self._clients[service] = boto3.client(
                service,
                region_name=self.region,
                endpoint_url=(os.environ.get("CALL_LAMBDA_ENDPOINT") or None)
                if service == "lambda"
                else None,
                config=Config(
                    connect_timeout=2,
                    read_timeout=8 if service == "lambda" else 3,
                    retries={"total_max_attempts": 1, "mode": "standard"},
                ),
            )
        return self._clients[service]

    def invoke(self, service: str, operation: str, **kwargs: Any) -> dict:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            return getattr(self.client(service), operation)(**kwargs)
        except (BotoCoreError, ClientError) as exc:
            # Fixed metadata only; exception strings may carry URLs or credentials.
            logging.getLogger(__name__).warning(
                "Voice call dependency failed (%s/%s, %s)",
                service,
                operation,
                type(exc).__name__,
            )
            raise unavailable() from exc

    def put_signal(self, call_id: str, side: str, sdp: str) -> None:
        self.invoke(
            "dynamodb",
            "put_item",
            TableName=self.table,
            Item={
                "pk": {"S": f"CALL#{call_id}"},
                "sk": {"S": side},
                "sdp": {"S": sdp},
                "expires_at": {"N": str(int(time.time()) + 300)},
            },
        )

    def get_signal(self, call_id: str, side: str) -> str | None:
        item = self.invoke(
            "dynamodb",
            "get_item",
            TableName=self.table,
            Key={"pk": {"S": f"CALL#{call_id}"}, "sk": {"S": side}},
            ConsistentRead=True,
        ).get("Item")
        if not item or int(item["expires_at"]["N"]) <= time.time():
            return None
        return item["sdp"]["S"]

    def ice_servers(self, call_id: str, side: str) -> dict:
        response = self.invoke(
            "lambda",
            "invoke",
            FunctionName=self.function,
            InvocationType="RequestResponse",
            Payload=json.dumps({"call_id": call_id, "side": side}).encode(),
        )
        if response.get("FunctionError"):
            raise unavailable()
        try:
            body = json.loads(response["Payload"].read())
            if not isinstance(body.get("ice_servers"), list) or not body["ice_servers"]:
                raise ValueError
            return body
        except (KeyError, ValueError, TypeError) as exc:
            raise unavailable() from exc


def unavailable() -> ApiError:
    return ApiError(
        503, "CALLS_UNAVAILABLE", "Voice calls are temporarily unavailable.", retryable=True
    )


def calls_from_env() -> AwsCallServices | None:
    table = os.environ.get("CALL_SIGNALS_TABLE", "").strip()
    function = os.environ.get("CALL_RELAY_FUNCTION", "").strip()
    if not table or not function:
        return None
    return AwsCallServices(table, function, os.environ.get("AWS_REGION", ""))
