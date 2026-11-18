"""Recording purge SDK contract tests; all AWS responses are synthetic Stubber data."""

from __future__ import annotations

import boto3
import pytest
from botocore.exceptions import EndpointConnectionError
from botocore.stub import Stubber

from umsal_backend import deletion_storage as cleanup
from umsal_backend.results import DynamoResultStore
from umsal_backend.storage import S3AudioStore, StorageUnavailable

BUCKET = "umsal-audio-test"
TABLE = "umsal-results-test"
REGION = "ap-southeast-2"
RECORDING = "rec_1"
KEY = "recordings/users/usr_1/rec_1/source"
PREFIX = KEY.removesuffix("source")


def result_key(version="1", recording=RECORDING):
    return {"pk": {"S": f"REC#{recording}"}, "sk": {"S": f"RESULT#{version}"}}


def query_params():
    return {
        "TableName": TABLE,
        "KeyConditionExpression": "#pk = :pk",
        "ExpressionAttributeNames": {"#pk": "pk", "#sk": "sk"},
        "ExpressionAttributeValues": {":pk": {"S": f"REC#{RECORDING}"}},
        "ProjectionExpression": "#pk, #sk",
        "ConsistentRead": True,
    }


@pytest.fixture
def stores(monkeypatch):
    # Explicit fake credentials prevent any credential/profile/instance metadata lookup.
    kwargs = {
        "region_name": REGION,
        "aws_access_key_id": "synthetic-access",
        "aws_secret_access_key": "synthetic-secret",
    }
    s3 = boto3.client("s3", **kwargs)
    dynamo = boto3.client("dynamodb", **kwargs)
    audio = S3AudioStore(BUCKET, REGION, client=s3)
    results = DynamoResultStore(TABLE, REGION, client=dynamo)
    monkeypatch.setattr(cleanup, "audio_store_from_env", lambda: audio)
    monkeypatch.setattr(cleanup, "result_store_from_env", lambda: results)
    with Stubber(s3) as a, Stubber(dynamo) as d:
        yield a, d, s3
        a.assert_no_pending_responses()
        d.assert_no_pending_responses()


def empty_s3(stub, prefix=PREFIX):
    for operation in ("list_multipart_uploads", "list_object_versions"):
        stub.add_response(operation, {}, {"Bucket": BUCKET, "Prefix": prefix})


def test_paginated_purge_removes_versions_markers_artifacts_multipart_and_all_results(stores):
    a, d, _ = stores
    params = {"Bucket": BUCKET, "Prefix": PREFIX}
    artifact = PREFIX + "jobs/job_1/prepared.json"
    a.add_response(
        "list_multipart_uploads",
        {"IsTruncated": True, "NextKeyMarker": artifact, "NextUploadIdMarker": "u1"},
        params,
    )
    a.add_response(
        "list_multipart_uploads",
        {"Uploads": [{"Key": artifact, "UploadId": "u2"}]},
        {**params, "KeyMarker": artifact, "UploadIdMarker": "u1"},
    )
    upload = {"Bucket": BUCKET, "Key": artifact, "UploadId": "u2"}
    a.add_response("abort_multipart_upload", {}, upload)
    a.add_client_error("list_parts", "NoSuchUpload", http_status_code=404, expected_params=upload)
    a.add_response(
        "list_object_versions",
        {
            "Versions": [{"Key": KEY, "VersionId": "v1"}],
            "IsTruncated": True,
            "NextKeyMarker": KEY,
            "NextVersionIdMarker": "v1",
        },
        params,
    )
    a.add_response(
        "list_object_versions",
        {
            "Versions": [{"Key": artifact, "VersionId": "null"}],
            "DeleteMarkers": [{"Key": KEY, "VersionId": "marker"}],
        },
        {**params, "KeyMarker": KEY, "VersionIdMarker": "v1"},
    )
    for key, version in [(KEY, "v1"), (artifact, "null"), (KEY, "marker")]:
        a.add_response("delete_object", {}, {"Bucket": BUCKET, "Key": key, "VersionId": version})
    q = query_params()
    first, orphan = result_key(), result_key("orphan")
    d.add_response("query", {"Items": [first], "LastEvaluatedKey": first}, q)
    d.add_response("query", {"Items": [orphan]}, {**q, "ExclusiveStartKey": first})
    for key in (first, orphan):
        d.add_response("delete_item", {}, {"TableName": TABLE, "Key": key})
    empty_s3(a)
    d.add_response("query", {"Items": []}, q)
    cleanup.purge_recording_storage(RECORDING, KEY)


@pytest.mark.parametrize("key", [KEY, "recordings/rec_1/source"])
def test_empty_recording_is_idempotent_for_both_persisted_key_layouts(stores, key):
    a, d, _ = stores
    for _ in range(2):
        empty_s3(a, key.removesuffix("source"))
        d.add_response("query", {}, query_params())
        empty_s3(a, key.removesuffix("source"))
        d.add_response("query", {}, query_params())
        cleanup.purge_recording_storage(RECORDING, key)


@pytest.mark.parametrize(
    "recording,key",
    [
        ("rec_2", KEY),
        ("users", "recordings/users/source"),
        ("../rec_1", KEY),
        (RECORDING, "recordings/rec_1"),
        (RECORDING, "recordings/rec_1/source/child"),
        (RECORDING, "recordings/users/usr_1/rec_10/source"),
        (RECORDING, "recordings/users/../rec_1/source"),
    ],
)
def test_invalid_or_mismatched_persisted_key_makes_no_aws_call(stores, recording, key):
    with pytest.raises(StorageUnavailable):
        cleanup.purge_recording_storage(recording, key)


@pytest.mark.parametrize("operation", ["list_multipart_uploads", "list_object_versions"])
def test_foreign_prefix_from_listing_is_never_deleted(stores, operation):
    a, _, _ = stores
    if operation == "list_object_versions":
        a.add_response("list_multipart_uploads", {}, {"Bucket": BUCKET, "Prefix": PREFIX})
    name, identifier = (
        ("Uploads", "UploadId")
        if operation == "list_multipart_uploads"
        else ("Versions", "VersionId")
    )
    a.add_response(
        operation,
        {name: [{"Key": "recordings/users/usr_1/rec_10/source", identifier: "v1"}]},
        {"Bucket": BUCKET, "Prefix": PREFIX},
    )
    with pytest.raises(StorageUnavailable):
        cleanup.purge_recording_storage(RECORDING, KEY)


def test_foreign_dynamo_partition_is_never_deleted(stores):
    a, d, _ = stores
    empty_s3(a)
    d.add_response("query", {"Items": [result_key(recording="rec_2")]}, query_params())
    with pytest.raises(StorageUnavailable):
        cleanup.purge_recording_storage(RECORDING, KEY)


@pytest.mark.parametrize("operation", ["list_multipart_uploads", "list_object_versions", "query"])
def test_dependency_errors_are_retryable(stores, operation):
    a, d, _ = stores
    if operation == "list_object_versions":
        a.add_response("list_multipart_uploads", {}, {"Bucket": BUCKET, "Prefix": PREFIX})
    if operation == "query":
        empty_s3(a)
    (d if operation == "query" else a).add_client_error(operation, http_status_code=503)
    with pytest.raises(StorageUnavailable):
        cleanup.purge_recording_storage(RECORDING, KEY)


def test_partial_result_delete_failure_remains_retryable(stores):
    a, d, _ = stores
    empty_s3(a)
    d.add_response("query", {"Items": [result_key(), result_key("2")]}, query_params())
    d.add_response("delete_item", {}, {"TableName": TABLE, "Key": result_key()})
    d.add_client_error("delete_item", http_status_code=503)
    with pytest.raises(StorageUnavailable):
        cleanup.purge_recording_storage(RECORDING, KEY)


def test_partial_s3_delete_failure_remains_retryable(stores):
    a, _, _ = stores
    a.add_response("list_multipart_uploads", {}, {"Bucket": BUCKET, "Prefix": PREFIX})
    a.add_response(
        "list_object_versions",
        {"Versions": [{"Key": KEY, "VersionId": "v1"}, {"Key": KEY, "VersionId": "v2"}]},
        {"Bucket": BUCKET, "Prefix": PREFIX},
    )
    a.add_response("delete_object", {}, {"Bucket": BUCKET, "Key": KEY, "VersionId": "v1"})
    a.add_client_error("delete_object", http_status_code=503)
    with pytest.raises(StorageUnavailable):
        cleanup.purge_recording_storage(RECORDING, KEY)


@pytest.mark.parametrize("already_removed", [False, True])
def test_abort_and_parts_empty_or_already_removed_are_idempotent(stores, already_removed):
    a, d, _ = stores
    a.add_response(
        "list_multipart_uploads",
        {"Uploads": [{"Key": KEY, "UploadId": "u1"}]},
        {"Bucket": BUCKET, "Prefix": PREFIX},
    )
    params = {"Bucket": BUCKET, "Key": KEY, "UploadId": "u1"}
    if already_removed:
        a.add_client_error(
            "abort_multipart_upload", "NoSuchUpload", http_status_code=404, expected_params=params
        )
    else:
        a.add_response("abort_multipart_upload", {}, params)
        a.add_response("list_parts", {"Parts": []}, params)
    a.add_response("list_object_versions", {}, {"Bucket": BUCKET, "Prefix": PREFIX})
    d.add_response("query", {}, query_params())
    empty_s3(a)
    d.add_response("query", {}, query_params())
    cleanup.purge_recording_storage(RECORDING, KEY)


def test_abort_with_remaining_parts_is_not_complete(stores):
    a, _, _ = stores
    a.add_response(
        "list_multipart_uploads",
        {"Uploads": [{"Key": KEY, "UploadId": "u1"}]},
        {"Bucket": BUCKET, "Prefix": PREFIX},
    )
    params = {"Bucket": BUCKET, "Key": KEY, "UploadId": "u1"}
    a.add_response("abort_multipart_upload", {}, params)
    a.add_response("list_parts", {"Parts": [{"PartNumber": 1}]}, params)
    with pytest.raises(StorageUnavailable):
        cleanup.purge_recording_storage(RECORDING, KEY)


@pytest.mark.parametrize("remaining", ["multipart", "version", "result"])
def test_late_items_discovered_by_final_verification_are_retryable(stores, remaining):
    a, d, _ = stores
    empty_s3(a)
    d.add_response("query", {}, query_params())
    a.add_response(
        "list_multipart_uploads",
        {"Uploads": [{"Key": KEY, "UploadId": "late"}]} if remaining == "multipart" else {},
        {"Bucket": BUCKET, "Prefix": PREFIX},
    )
    if remaining != "multipart":
        a.add_response(
            "list_object_versions",
            {"Versions": [{"Key": KEY, "VersionId": "late"}]} if remaining == "version" else {},
            {"Bucket": BUCKET, "Prefix": PREFIX},
        )
    if remaining == "result":
        d.add_response("query", {"Items": [result_key()]}, query_params())
    with pytest.raises(StorageUnavailable):
        cleanup.purge_recording_storage(RECORDING, KEY)


def test_transport_error_is_mapped_without_exposing_sdk_details(stores, monkeypatch):
    _, _, s3 = stores

    def fail(**_kwargs):
        raise EndpointConnectionError(endpoint_url="https://synthetic.invalid")

    monkeypatch.setattr(s3, "list_multipart_uploads", fail)
    with pytest.raises(StorageUnavailable):
        cleanup.purge_recording_storage(RECORDING, KEY)


@pytest.mark.parametrize("missing", ["audio", "results", "region"])
def test_missing_configuration_never_succeeds(stores, monkeypatch, missing):
    if missing == "audio":
        monkeypatch.setattr(cleanup, "audio_store_from_env", lambda: None)
    else:
        store = None if missing == "results" else DynamoResultStore(TABLE, "")
        monkeypatch.setattr(cleanup, "result_store_from_env", lambda: store)
    with pytest.raises(StorageUnavailable):
        cleanup.purge_recording_storage(RECORDING, KEY)
