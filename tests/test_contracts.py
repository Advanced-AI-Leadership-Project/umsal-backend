"""Offline static checks for contracts/ (ported from umsal-pwa tools/validate_contracts.py).

These check the planned contract itself, not the running API, except
``test_implemented_health_responses_match_contract`` which validates the responses
of endpoints that already exist against their contract schemas.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from urllib.parse import unquote, urlsplit

import jsonschema
import pytest
import yaml
from fastapi.testclient import TestClient
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from umsal_backend.app import create_app

CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"
OPENAPI = CONTRACTS / "openapi.yaml"
ANALYSIS = CONTRACTS / "analysis-result.schema.json"
EXAMPLES = CONTRACTS / "examples"
METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
PUBLIC_PATHS = {"/health/live", "/health/ready"}
EXAMPLE_SCHEMAS = {
    "audio-prep-ready.json": "AudioPrepArtifact",
    "worker-complete-request.json": "CompleteRequest",
    "worker-claim-request.json": "ClaimRequest",
    "queue-audio-prep.json": "QueueMessage",
    "consent-update.json": "ConsentUpdate",
    "speaker-confirmation.json": "SpeakerAssignment",
    "worker-terminal-disposition.json": "TerminalDisposition",
    "upload-recording.json": "UploadRequest",
    "upload-finalize-recording.json": "FinalizeRequest",
    "recording-analysis-unavailable.json": "Error",
}


def _read(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    return yaml.safe_load(text) if path.suffix in {".yaml", ".yml"} else json.loads(text)


def _resolve_pointer(document: dict, fragment: str) -> None:
    if not fragment:
        return
    assert fragment.startswith("/"), "Only JSON Pointer fragments are supported"
    node = document
    for token in unquote(fragment[1:]).split("/"):
        key = token.replace("~1", "/").replace("~0", "~")
        node = node[int(key)] if isinstance(node, list) else node[key]


def _collect_documents() -> tuple[dict[Path, dict], int]:
    documents: dict[Path, dict] = {}
    ref_count = 0

    def visit(path: Path) -> None:
        if path not in documents:
            documents[path] = _read(path)
            walk(documents[path], path)

    def walk(node: object, source: Path) -> None:
        nonlocal ref_count
        if isinstance(node, dict):
            for key, value in node.items():
                if key != "$ref":
                    walk(value, source)
                    continue
                ref_count += 1
                parts = urlsplit(value)
                assert not parts.scheme and not parts.netloc, "Remote $ref is forbidden"
                assert not parts.query, "$ref query strings are forbidden"
                target = (source.parent / unquote(parts.path)).resolve() if parts.path else source
                assert target.is_relative_to(CONTRACTS), "$ref escaped contracts/"
                assert target.is_file(), f"Missing local $ref: {target.name}"
                visit(target)
                _resolve_pointer(documents[target], parts.fragment)
        elif isinstance(node, list):
            for item in node:
                walk(item, source)

    visit(OPENAPI)
    visit(ANALYSIS)
    return documents, ref_count


@pytest.fixture(scope="module")
def contract():
    documents, ref_count = _collect_documents()
    resources = []
    for path, document in documents.items():
        resource = Resource.from_contents(document, default_specification=DRAFT202012)
        resources.append((path.as_uri(), resource))
        if "$id" in document:
            resources.append((document["$id"], resource))
    # An explicit Registry has no HTTP/file retrieval fallback: no network is used.
    registry = Registry().with_resources(resources)

    def validator(name: str) -> jsonschema.Draft202012Validator:
        return jsonschema.Draft202012Validator(
            {"$ref": f"{OPENAPI.as_uri()}#/components/schemas/{name}"},
            registry=registry,
            format_checker=jsonschema.FormatChecker(),
        )

    analysis = documents[ANALYSIS]
    analysis_validator = jsonschema.Draft202012Validator(
        analysis, registry=registry, format_checker=jsonschema.FormatChecker()
    )
    examples = {path.name: _read(path) for path in sorted(EXAMPLES.glob("*.json"))}
    return {
        "spec": documents[OPENAPI],
        "analysis": analysis,
        "ref_count": ref_count,
        "validator": validator,
        "analysis_validator": analysis_validator,
        "examples": examples,
    }


def _operations(spec: dict):
    for path, item in spec["paths"].items():
        for method, operation in item.items():
            if method in METHODS:
                yield path, method, operation


def test_local_refs_resolve(contract):
    assert contract["ref_count"] > 0


def test_version_and_server_prefix(contract):
    spec = contract["spec"]
    assert spec["info"]["version"] == "0.4.0"
    assert spec["servers"] == [{"url": "/api", "description": spec["servers"][0]["description"]}]


def test_recording_gate_is_documented_for_ticket_and_finalize(contract):
    for path in ("/uploads", "/uploads/{id}/finalize"):
        operation = contract["spec"]["paths"][path]["post"]
        assert "CpuRealAudioEnabled=true" in operation["description"]
        assert "503 TEMPORARILY_UNAVAILABLE" in operation["description"]
        assert "503" in operation["responses"]
    assert contract["examples"]["upload-recording.json"]["demo"] is False


def test_operations_have_expected_auth_and_unique_ids(contract):
    operations = list(_operations(contract["spec"]))
    for path, method, operation in operations:
        if path in PUBLIC_PATHS:
            expected = []
        elif path.startswith("/internal/worker/"):
            expected = [{"AwsSigV4": []}]
        else:
            expected = [{"CognitoBearer": []}]
        assert operation["security"] == expected, f"Wrong auth: {method} {path}"
    ids = [operation["operationId"] for _path, _method, operation in operations]
    assert len(ids) == len(set(ids)), "Duplicate operationId"
    # 28 operations from umsal-pwa v0.2.0 + health live/ready (D2) + listConnections (D7)
    # + setConnectionLabel (D10).
    assert len(ids) == 32
    assert {"getHealthLive", "getHealthReady", "listConnections"} <= set(ids)


def test_schemas_are_valid_json_schema(contract):
    for schema in contract["spec"]["components"]["schemas"].values():
        jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator.check_schema(contract["analysis"])


def test_examples_validate(contract):
    examples = contract["examples"]
    assert len(examples) == 14, "Expected the 14 reviewed contract examples"
    for filename, example in examples.items():
        if filename not in {
            "upload-recording.json",
            "upload-finalize-recording.json",
            "recording-analysis-unavailable.json",
        }:
            assert example.get("demo") is True, f"Missing demo flag: {filename}"
        if filename.startswith("analysis-"):
            contract["analysis_validator"].validate(example)
        else:
            contract["validator"](EXAMPLE_SCHEMAS[filename]).validate(example)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["observations"][0].update(evidence=[]),
        lambda d: d["coverage"].update(analyzed_utterances=0),
        lambda d: d.update(status="FAILED", failure=None),
        lambda d: d.update(analysis_grant_versions={"usr_demo_a": 1}),
        lambda d: d.update(clinical_risk=99),
    ],
    ids=[
        "observed-without-evidence",
        "observed-with-zero-analyzed",
        "failed-without-detail",
        "missing-grant",
        "clinical-score",
    ],
)
def test_invalid_analysis_results_are_rejected(contract, mutate):
    invalid = copy.deepcopy(contract["examples"]["analysis-observed.json"])
    mutate(invalid)
    assert not contract["analysis_validator"].is_valid(invalid)


def test_lease_stage_and_speaker_rules(contract):
    examples = contract["examples"]
    complete = examples["worker-complete-request.json"]
    lease = copy.deepcopy(examples["queue-audio-prep.json"])
    del lease["pipeline_version"]
    lease.update(
        lease_token=complete["lease_token"],
        lease_expires_at="2026-10-30T10:00:00Z",
        snapshot=copy.deepcopy(complete["snapshot"]),
        speaker_assignment=None,
        input_artifacts=[{**complete["artifact"], "purpose": "RAW_AUDIO"}],
        allowed_output_bucket="umsal-demo-artifacts",
        allowed_output_prefix="jobs/job_demo_audio_001/",
        attempt=1,
    )
    lease["snapshot"]["speaker_assignment_version"] = 0
    lease_validator = contract["validator"]("Lease")
    lease_validator.validate(lease)
    text_lease = copy.deepcopy(lease)
    assignment = {"target_subject_id": "usr_demo_a", "speaker_id": "spk_demo_a", "version": 1}
    text_lease.update(stage="TEXT_ANALYSIS", speaker_assignment=assignment)
    text_lease["snapshot"]["speaker_assignment_version"] = 1
    text_lease["input_artifacts"][0]["purpose"] = "AUDIO_PREP_RESULT"
    lease_validator.validate(text_lease)
    bad = [copy.deepcopy(lease)] + [copy.deepcopy(text_lease) for _ in range(3)]
    bad[0]["speaker_assignment"] = text_lease["speaker_assignment"]
    bad[1]["speaker_assignment"] = None
    bad[2]["speaker_assignment"]["version"] = 0
    del bad[3]["speaker_assignment"]
    for invalid in bad:
        assert not lease_validator.is_valid(invalid), "Accepted invalid stage/speaker lease"


def test_connection_page_shape(contract):
    page = contract["validator"]("ConnectionPage")
    actor = {"user_id": "usr_demo_a", "display_name": "합성 사용자"}
    connection = {
        "connection_id": "con_demo_001",
        "participants": [actor, {**actor, "user_id": "usr_demo_b"}],
        "status": "ACTIVE",
        "version": 1,
    }
    page.validate({"items": [connection], "next_cursor": None})
    assert not page.is_valid({"items": [connection]})


def test_implemented_health_responses_match_contract(contract):
    """Implemented endpoints must answer in their contract shape (paths under /api)."""
    client = TestClient(create_app())
    live = client.get("/api/health/live")
    assert live.status_code == 200
    contract["validator"]("LiveResponse").validate(live.json())
    ready = client.get("/api/health/ready")
    assert ready.status_code in (200, 503)
    contract["validator"]("ReadyResponse").validate(ready.json())
