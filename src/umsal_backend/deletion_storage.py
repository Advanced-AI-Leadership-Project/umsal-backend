"""Physical recording cleanup, called only by scheduled maintenance.

PostgreSQL must already hide the recording and fence its jobs. This adapter only
confirms that the recording's live storage is empty now; the caller owns retries
until outstanding upload URLs expire, and must track backup retention separately.
No audio, result body, source key or SDK error is logged here.
"""

from __future__ import annotations

from typing import Any

from umsal_backend.results import DynamoResultStore, result_store_from_env
from umsal_backend.storage import (
    S3AudioStore,
    StorageUnavailable,
    audio_store_from_env,
    recording_key,
    source_prefix,
)


def _checked_prefix(recording_id: str, s3_key: str) -> str:
    recording_key(recording_id)  # Validate the identifier before making any AWS call.
    prefix = source_prefix(s3_key)
    # "users" is reserved for the user-scoped layout, never a legacy recording.
    if prefix == "recordings/users/" or prefix.rstrip("/").rsplit("/", 1)[-1] != recording_id:
        raise StorageUnavailable
    return prefix


def _s3_targets(store: S3AudioStore, prefix: str, operation: str) -> list[tuple[str, str]]:
    targets = []
    for page in store.client.get_paginator(operation).paginate(Bucket=store.bucket, Prefix=prefix):
        if operation == "list_object_versions":
            items = page.get("Versions", []) + page.get("DeleteMarkers", [])
            identifier = "VersionId"
        else:
            items = page.get("Uploads", [])
            identifier = "UploadId"
        for item in items:
            key, value = item["Key"], item[identifier]
            # Never trust a returned object outside the exact recording namespace.
            if not isinstance(key, str) or not key.startswith(prefix) or not value:
                raise StorageUnavailable
            targets.append((key, value))
    return targets


def _result_keys(store: DynamoResultStore, recording_id: str) -> list[dict[str, Any]]:
    pk = {"S": f"REC#{recording_id}"}
    keys = []
    for page in (
        store.client()
        .get_paginator("query")
        .paginate(
            TableName=store.table,
            KeyConditionExpression="#pk = :pk",
            ExpressionAttributeNames={"#pk": "pk", "#sk": "sk"},
            ExpressionAttributeValues={":pk": pk},
            ProjectionExpression="#pk, #sk",
            ConsistentRead=True,
        )
    ):
        for item in page.get("Items", []):
            if item["pk"] != pk or not item["sk"].get("S"):
                raise StorageUnavailable
            # Delete the complete partition, including old and orphan result versions.
            keys.append({"pk": item["pk"], "sk": item["sk"]})
    return keys


def _abort_upload(store: S3AudioStore, key: str, upload_id: str) -> None:
    params = {"Bucket": store.bucket, "Key": key, "UploadId": upload_id}
    try:
        store.client.abort_multipart_upload(**params)
        # An in-flight part may finish during abort. Do not report a partial purge
        # as complete; maintenance will retry. A removed upload answers NoSuchUpload.
        for page in store.client.get_paginator("list_parts").paginate(**params):
            if page.get("Parts"):
                raise StorageUnavailable
    except store.client.exceptions.NoSuchUpload:
        pass


def purge_recording_storage(recording_id: str, s3_key: str) -> None:
    """Idempotently purge this recording's versions, multipart uploads and results.

    Missing configuration, an unsafe persisted key, dependency failures and any
    remaining live item all raise StorageUnavailable for the maintenance retry path.
    The function never scans a bucket/table or expands a prefix from client input.
    """
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        prefix = _checked_prefix(recording_id, s3_key)
        audio = audio_store_from_env()
        results = result_store_from_env()
        if audio is None or results is None or not results.region:
            raise StorageUnavailable

        # Collect each listing before mutating it, so deletion cannot move its cursor.
        uploads = _s3_targets(audio, prefix, "list_multipart_uploads")
        for key, upload_id in uploads:
            _abort_upload(audio, key, upload_id)
        versions = _s3_targets(audio, prefix, "list_object_versions")
        for key, version_id in versions:
            audio.delete_version(key, version_id)
        for key in _result_keys(results, recording_id):
            results._call("delete_item", Key=key)

        # All three checks must succeed, including every page. A new late writer
        # or any partial deletion keeps the durable deletion job retryable.
        if _s3_targets(audio, prefix, "list_multipart_uploads"):
            raise StorageUnavailable
        if _s3_targets(audio, prefix, "list_object_versions"):
            raise StorageUnavailable
        if _result_keys(results, recording_id):
            raise StorageUnavailable
    except (BotoCoreError, ClientError, KeyError, TypeError, ValueError) as error:
        # This adapter is the SDK boundary; callers need one safe, retryable error.
        raise StorageUnavailable from error
