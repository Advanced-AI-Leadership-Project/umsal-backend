"""IaC for MVP recordings (#70): CORS and least-privilege storage permissions."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1] / "infra" / "aws"


class _CfnLoader(yaml.SafeLoader):
    pass


def _tag(loader: yaml.SafeLoader, suffix: str, node: yaml.Node):
    if isinstance(node, yaml.ScalarNode):
        return {suffix: loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {suffix: loader.construct_sequence(node, deep=True)}
    return {suffix: loader.construct_mapping(node, deep=True)}


_CfnLoader.add_multi_constructor("!", _tag)


def _load(name: str) -> dict:
    return yaml.load((ROOT / name).read_text(encoding="utf-8"), Loader=_CfnLoader)  # noqa: S506


def _policy(role: dict, name: str) -> list[dict]:
    for item in role["Properties"]["Policies"]:
        if isinstance(item, dict) and "If" in item:
            condition, policy, _ = item["If"]
            if policy["PolicyName"] == name:
                return policy["PolicyDocument"]["Statement"]
    raise AssertionError(f"policy {name} not found")


def test_audio_bucket_allows_only_presigned_browser_puts():
    bucket = _load("storage.yaml")["Resources"]["AudioBucket"]["Properties"]
    (rule,) = bucket["CorsConfiguration"]["CorsRules"]
    assert rule["AllowedMethods"] == ["PUT"]
    assert rule["AllowedOrigins"] == {"Ref": "UploadOrigins"}
    assert "*" not in str(rule["AllowedOrigins"])
    assert set(rule["AllowedHeaders"]) == {
        "content-type",
        "x-amz-checksum-sha256",
        "x-amz-sdk-checksum-algorithm",
    }
    assert "x-amz-version-id" in rule["ExposedHeaders"]
    origins = _load("storage.yaml")["Parameters"]["UploadOrigins"]["Default"]
    assert "*" not in origins


@pytest.mark.parametrize(
    ("template", "role", "policy", "actions"),
    [
        (
            "api.yaml",
            "FunctionRole",
            "RecordingStorage",
            {
                "s3:PutObject",
                "s3:GetObject",
                "s3:GetObjectVersion",
                "s3:DeleteObjectVersion",
                "s3:ListBucketVersions",
                "dynamodb:GetItem",
                "dynamodb:PutItem",
                "dynamodb:DeleteItem",
                "dynamodb:Query",
            },
        ),
        (
            "jobs.yaml",
            "MaintenanceRole",
            "RecordingSweep",
            {
                "s3:DeleteObjectVersion",
                "s3:ListBucketVersions",
                "dynamodb:Query",
                "dynamodb:DeleteItem",
            },
        ),
    ],
)
def test_storage_permissions_are_scoped(template, role, policy, actions):
    statements = _policy(_load(template)["Resources"][role], policy)
    granted = set()
    for statement in statements:
        action = statement["Action"]
        names = {action} if isinstance(action, str) else set(action)
        granted |= names
        resource = statement["Resource"]["Sub"]
        assert "*" not in resource.replace("/recordings/*", "")
        if any(name.startswith("s3:") and name != "s3:ListBucketVersions" for name in names):
            assert resource.endswith("/recordings/*")
        if "s3:ListBucketVersions" in names:
            assert statement["Condition"] == {"StringLike": {"s3:prefix": "recordings/*"}}
        if any(name.startswith("dynamodb:") for name in names):
            assert statement["Condition"] == {
                "ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["REC#*"]}
            }
    assert granted == actions


def test_recording_sweep_runs_every_15_minutes_only_with_recordings():
    schedule = _load("jobs.yaml")["Resources"]["RecordingSweep"]
    assert schedule["Condition"] == "UseRecordings"
    assert schedule["Properties"]["ScheduleExpression"] == "rate(15 minutes)"
    assert schedule["Properties"]["Target"]["Input"] == '{"task": "recordings"}'
