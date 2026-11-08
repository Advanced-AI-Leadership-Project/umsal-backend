"""Audio object storage for MVP recordings (docs/mvp-recording-design.md 8, #72).

The API never reads an audio body. It only

* signs a browser PUT whose size, type and SHA-256 are part of the signature, so S3
  itself rejects a different file (signed headers: content-length, content-type,
  x-amz-checksum-sha256, x-amz-sdk-checksum-algorithm);
* checks one version's size and checksum with HeadObject (ChecksumMode=ENABLED);
* deletes versions under a recording's own prefix.

``AudioStore`` is the seam the routes use; tests pass a fake. Presigned URLs are bearer
credentials: never log them or store them (the idempotency record omits them).
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from typing import Any, Protocol

UPLOAD_URL_SECONDS = 15 * 60
CONNECT_TIMEOUT_SECONDS = 2
READ_TIMEOUT_SECONDS = 5


def recording_key(recording_id: str) -> str:
    return f"recordings/{recording_id}/source"


def recording_prefix(recording_id: str) -> str:
    return f"recordings/{recording_id}/"


def sha256_b64(hex_digest: str) -> str:
    """The x-amz-checksum-sha256 value (base64 of the raw digest) for a hex digest."""
    return base64.b64encode(bytes.fromhex(hex_digest)).decode("ascii")


@dataclass(frozen=True)
class PresignedPut:
    url: str
    # Headers the browser must send exactly. Content-Length is signed too but set by the
    # browser from the body, so it is not listed (it cannot be set by script).
    headers: dict[str, str]


@dataclass(frozen=True)
class ObjectVersion:
    size: int
    checksum_sha256: str | None  # base64, as S3 reports it


class StorageUnavailable(Exception):
    """S3 did not answer or failed transiently; the request may be retried."""


class AudioStore(Protocol):
    def presign_put(
        self, key: str, *, content_type: str, size: int, sha256_hex: str, expires_in: int
    ) -> PresignedPut: ...

    def head_version(self, key: str, version_id: str) -> ObjectVersion | None: ...

    def delete_version(self, key: str, version_id: str) -> None: ...

    def delete_versions(self, prefix: str, *, keep: str | None = None) -> int: ...

    def remaining_versions(self, prefix: str) -> int: ...


class S3AudioStore:
    """boto3 implementation. Region-specific virtual-host URLs so CORS preflight is not
    redirected (a 307 on preflight fails in the browser)."""

    def __init__(self, bucket: str, region: str, client: Any | None = None) -> None:
        self.bucket = bucket
        self._region = region
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:  # created on first use, reused by a warm Lambda
            import boto3
            from botocore.config import Config

            self._client = boto3.client(
                "s3",
                region_name=self._region,
                config=Config(
                    signature_version="s3v4",
                    s3={"addressing_style": "virtual"},
                    connect_timeout=CONNECT_TIMEOUT_SECONDS,
                    read_timeout=READ_TIMEOUT_SECONDS,
                    retries={"total_max_attempts": 2, "mode": "standard"},
                ),
            )
        return self._client

    def presign_put(
        self, key: str, *, content_type: str, size: int, sha256_hex: str, expires_in: int
    ) -> PresignedPut:
        checksum = sha256_b64(sha256_hex)
        url = self.client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": self.bucket,
                "Key": key,
                "ContentType": content_type,
                "ContentLength": size,
                "ChecksumSHA256": checksum,
                "ChecksumAlgorithm": "SHA256",
            },
            ExpiresIn=expires_in,
        )
        return PresignedPut(
            url=url,
            headers={
                "Content-Type": content_type,
                "x-amz-checksum-sha256": checksum,
                "x-amz-sdk-checksum-algorithm": "SHA256",
            },
        )

    def head_version(self, key: str, version_id: str) -> ObjectVersion | None:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            head = self.client.head_object(
                Bucket=self.bucket, Key=key, VersionId=version_id, ChecksumMode="ENABLED"
            )
        except ClientError as error:
            status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            # Without s3:ListBucket a missing key or version answers 403, not 404, so
            # both mean "no such version" here (#71 review).
            if status in (400, 403, 404):
                return None
            raise StorageUnavailable from error
        except BotoCoreError as error:
            raise StorageUnavailable from error
        return ObjectVersion(
            size=int(head["ContentLength"]), checksum_sha256=head.get("ChecksumSHA256")
        )

    def delete_version(self, key: str, version_id: str) -> None:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            self.client.delete_object(Bucket=self.bucket, Key=key, VersionId=version_id)
        except (BotoCoreError, ClientError) as error:
            raise StorageUnavailable from error

    def _versions(self, prefix: str) -> list[tuple[str, str]]:
        found: list[tuple[str, str]] = []
        paginator = self.client.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for item in page.get("Versions", []) + page.get("DeleteMarkers", []):
                found.append((item["Key"], item["VersionId"]))
        return found

    def delete_versions(self, prefix: str, *, keep: str | None = None) -> int:
        """Delete every version and delete marker under ``prefix`` except ``keep``."""
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            targets = [(key, vid) for key, vid in self._versions(prefix) if vid != keep]
            # One DeleteObject per version: needs only s3:DeleteObjectVersion (a recording
            # has a handful of versions), and deleting a version never adds a marker.
            for key, vid in targets:
                self.client.delete_object(Bucket=self.bucket, Key=key, VersionId=vid)
            return len(targets)
        except (BotoCoreError, ClientError) as error:
            raise StorageUnavailable from error

    def remaining_versions(self, prefix: str) -> int:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            return len(self._versions(prefix))
        except (BotoCoreError, ClientError) as error:
            raise StorageUnavailable from error


def audio_store_from_env() -> S3AudioStore | None:
    bucket = os.environ.get("AUDIO_BUCKET", "").strip()
    if not bucket:
        return None
    region = (
        os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "ap-southeast-2"
    )
    return S3AudioStore(bucket, region)
