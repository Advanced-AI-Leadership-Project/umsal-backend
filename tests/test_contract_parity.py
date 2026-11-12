"""The running app's /openapi.json must agree with contracts/openapi.yaml (api-contract 6절 4번).

For every operation the app implements under /api:
* the operationId exists in the contract,
* the method and path match (contract paths are relative to ``servers: /api``; path
  parameter names may differ, e.g. ``{id}`` vs ``{connection_id}``),
* the success status code matches,
* a required ``Idempotency-Key`` header in the contract is required by the app too.

Contract operations that are not implemented yet are allowed (most are, until D8);
the test reports them so progress is visible.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from umsal_backend.app import create_app
from umsal_backend.recordings import FinalizeRequest, UploadRequest

CONTRACT = Path(__file__).resolve().parents[1] / "contracts" / "openapi.yaml"
METHODS = {"get", "post", "put", "patch", "delete"}


def _normalize(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{}", path)


def _operations(document: dict, prefix: str = "") -> dict[str, dict]:
    found = {}
    for path, item in document["paths"].items():
        parameters = item.get("parameters", [])
        for method, operation in item.items():
            if method not in METHODS:
                continue
            found[operation["operationId"]] = {
                "method": method,
                "path": _normalize(prefix + path),
                "success": sorted(code for code in operation["responses"] if code.startswith("2")),
                "idempotency_required": any(
                    p.get("name") == "Idempotency-Key" and p.get("required")
                    for p in [*parameters, *operation.get("parameters", [])]
                ),
            }
    return found


@pytest.fixture(scope="module")
def contract_ops() -> dict[str, dict]:
    document = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    (server,) = document["servers"]
    return _operations(document, prefix=server["url"])


@pytest.fixture(scope="module")
def app_ops() -> dict[str, dict]:
    schema = create_app(token_verifier=None, database=None).openapi()
    schema["paths"] = {p: i for p, i in schema["paths"].items() if p.startswith("/api/")}
    return _operations(schema)


def test_every_implemented_api_operation_is_in_the_contract(app_ops, contract_ops):
    unknown = sorted(set(app_ops) - set(contract_ops))
    assert not unknown, f"Implemented but not in contracts/openapi.yaml: {unknown}"


def test_implemented_operations_match_contract_shape(app_ops, contract_ops):
    mismatches = {
        operation_id: {"app": app_ops[operation_id], "contract": contract_ops[operation_id]}
        for operation_id in app_ops
        if app_ops[operation_id] != contract_ops.get(operation_id)
    }
    assert not mismatches, mismatches


def test_real_recording_examples_match_running_request_models():
    import json

    examples = CONTRACT.parent / "examples"
    upload = UploadRequest.model_validate_json((examples / "upload-recording.json").read_text())
    assert upload.demo is False
    # Omitting the optional flag has the same real-recording meaning at the gate.
    omitted = json.loads((examples / "upload-recording.json").read_text())
    del omitted["demo"]
    assert UploadRequest.model_validate(omitted).demo is None
    FinalizeRequest.model_validate_json((examples / "upload-finalize-recording.json").read_text())


def test_implemented_set_is_tracked(app_ops, contract_ops):
    # Update this list when an operation is implemented, so the progress is explicit.
    assert sorted(app_ops) == sorted(
        [
            "createCall",
            "getCurrentCall",
            "getCall",
            "endCall",
            "acceptCall",
            "heartbeatCall",
            "putCallSignal",
            "getCallSignal",
            "getCallIceServers",
            "acceptInvitation",
            "assignSpeaker",
            "createInvitation",
            "createUpload",
            "disconnect",
            "finalizeUpload",
            "getConnection",
            "getConsents",
            "getHealthLive",
            "getHealthReady",
            "getJob",
            "getMe",
            "getRecording",
            "getResult",
            "listConnections",
            "listRecordings",
            "setConnectionLabel",
            "updateConsents",
        ]
    )
    assert len(contract_ops) == 41


def test_call_examples_match_running_models():
    from umsal_backend.calls import CreateCall, VoiceCall

    examples = CONTRACT.parent / "examples"
    CreateCall.model_validate_json((examples / "call-create.json").read_text())
    VoiceCall.model_validate_json((examples / "call-view.json").read_text())
