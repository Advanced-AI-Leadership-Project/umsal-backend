"""S3AudioStore against botocore's Stubber (no network): exact calls and error mapping."""

from __future__ import annotations

import hashlib
from urllib.parse import parse_qs, urlparse

import boto3
import pytest
from botocore.config import Config
from botocore.stub import Stubber

from umsal_backend import storage
from umsal_backend.storage import S3AudioStore, StorageUnavailable

BUCKET = "umsal-audio-test"
KEY = "recordings/rec_1/source"
SHA = hashlib.sha256(b"x").hexdigest()


@pytest.fixture
def s3(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIASYNTHETIC")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "synthetic-secret")
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    client = boto3.client(
        "s3",
        region_name="ap-southeast-2",
        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
    )
    with Stubber(client) as stubber:
        yield S3AudioStore(BUCKET, "ap-southeast-2", client=client), stubber
        stubber.assert_no_pending_responses()


def test_presigned_put_signs_size_type_and_checksum(s3):
    store, _ = s3
    signed = store.presign_put(
        KEY, content_type="audio/mp4", size=1234, sha256_hex=SHA, expires_in=900
    )
    url = urlparse(signed.url)
    query = parse_qs(url.query)
    assert url.netloc == f"{BUCKET}.s3.ap-southeast-2.amazonaws.com"  # no CORS redirect
    assert query["X-Amz-Expires"] == ["900"]
    assert query["X-Amz-SignedHeaders"] == [
        "content-length;content-type;host;x-amz-checksum-sha256;x-amz-sdk-checksum-algorithm"
    ]
    assert signed.headers == {
        "Content-Type": "audio/mp4",
        "x-amz-checksum-sha256": storage.sha256_b64(SHA),
        "x-amz-sdk-checksum-algorithm": "SHA256",
    }


def test_head_version_reads_size_and_checksum(s3):
    store, stubber = s3
    stubber.add_response(
        "head_object",
        {"ContentLength": 1234, "ChecksumSHA256": storage.sha256_b64(SHA)},
        {"Bucket": BUCKET, "Key": KEY, "VersionId": "v1", "ChecksumMode": "ENABLED"},
    )
    assert store.head_version(KEY, "v1") == storage.ObjectVersion(1234, storage.sha256_b64(SHA))


@pytest.mark.parametrize("status", [403, 404, 405])
def test_missing_version_is_none_whether_s3_says_403_or_404(s3, status):
    store, stubber = s3
    stubber.add_client_error("head_object", http_status_code=status)
    assert store.head_version(KEY, "nope") is None


def test_head_version_server_error_is_retryable(s3):
    store, stubber = s3
    stubber.add_client_error("head_object", http_status_code=500)
    with pytest.raises(StorageUnavailable):
        store.head_version(KEY, "v1")


def test_delete_versions_removes_versions_and_markers_except_the_kept_one(s3):
    store, stubber = s3
    stubber.add_response(
        "list_object_versions",
        {
            "Versions": [{"Key": KEY, "VersionId": "v1"}, {"Key": KEY, "VersionId": "v2"}],
            "DeleteMarkers": [{"Key": KEY, "VersionId": "m1"}],
        },
        {"Bucket": BUCKET, "Prefix": "recordings/rec_1/"},
    )
    for version in ("v2", "m1"):
        stubber.add_response(
            "delete_object", {}, {"Bucket": BUCKET, "Key": KEY, "VersionId": version}
        )
    assert store.delete_versions("recordings/rec_1/", keep="v1") == 2


def test_delete_and_list_failures_are_retryable(s3):
    store, stubber = s3
    stubber.add_client_error("delete_object", http_status_code=503)
    with pytest.raises(StorageUnavailable):
        store.delete_version(KEY, "v1")
    stubber.add_client_error("list_object_versions", http_status_code=500)
    with pytest.raises(StorageUnavailable):
        store.delete_versions("recordings/rec_1/")
    stubber.add_client_error("list_object_versions", http_status_code=500)
    with pytest.raises(StorageUnavailable):
        store.remaining_versions("recordings/rec_1/")


def test_remaining_versions_counts_everything_under_the_prefix(s3):
    store, stubber = s3
    stubber.add_response(
        "list_object_versions",
        {"Versions": [{"Key": KEY, "VersionId": "v1"}], "DeleteMarkers": []},
        {"Bucket": BUCKET, "Prefix": "recordings/rec_1/"},
    )
    assert store.remaining_versions("recordings/rec_1/") == 1


def test_store_from_env(monkeypatch):
    monkeypatch.delenv("AUDIO_BUCKET", raising=False)
    assert storage.audio_store_from_env() is None
    monkeypatch.setenv("AUDIO_BUCKET", BUCKET)
    monkeypatch.setenv("AWS_REGION", "ap-southeast-2")
    store = storage.audio_store_from_env()
    assert store is not None and store.bucket == BUCKET
    assert store.client.meta.region_name == "ap-southeast-2"  # created lazily
    assert (
        storage.recording_key("rec_1") == KEY
        and storage.recording_prefix("rec_1") == "recordings/rec_1/"
    )
