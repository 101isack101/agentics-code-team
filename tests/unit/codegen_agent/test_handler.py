"""Unit tests for the CodeGen Agent Lambda handler.

No real AWS (moto), no real Anthropic call (injected MagicMock).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import boto3
import pytest

import codegen as codegen_app
from dynamo_repo import RunsRepository
from s3_repo import ArtifactsRepository


SPEC_PAYLOAD = {
    "spec_version": "1.0",
    "run_id": "run-gen",
    "project_name": "DemoEcho",
    "complexity": "simple",
    "language": "python",
    "summary": "Tiny echo service.",
    "user_stories": ["As a user I want POST /echo"],
    "functional_requirements": ["FR-001: Echo the text field"],
    "non_functional_requirements": [],
    "acceptance_criteria": [
        {"id": "AC-001", "description": "echoes", "testable": True}
    ],
    "api_contracts": [],
    "data_entities": [],
    "constraints": [],
    "out_of_scope": [],
}

VALID_CODEGEN_OUTPUT = {
    "language": "python",
    "entry_point": "app.py",
    "files": [
        {"path": "app.py", "content": "def handler(event, _):\n    return event\n"},
        {"path": "requirements.txt", "content": "boto3\n"},
        {
            "path": "tests/test_acceptance.py",
            "content": "def test_echo():\n    assert {'x': 1} == {'x': 1}\n",
        },
    ],
    "notes": "Minimal echo handler per AC-001.",
}


def _seed_spec(bucket: str, run_id: str, spec: dict) -> str:
    s3 = boto3.client("s3", region_name="us-east-1")
    key = f"specs/{run_id}/technical_spec.json"
    s3.put_object(Bucket=bucket, Key=key, Body=json.dumps(spec).encode("utf-8"))
    return f"s3://{bucket}/{key}"


def _fake_claude(output: dict) -> MagicMock:
    fake = MagicMock()
    fake.complete.return_value = json.dumps(output)
    return fake


def test_happy_path_generates_files_and_manifest(s3_bucket, runs_table, lambda_context):
    spec_uri = _seed_spec(s3_bucket, "run-gen", SPEC_PAYLOAD)
    event = {"run_id": "run-gen", "spec_s3_uri": spec_uri, "iteration": 1}

    result = codegen_app.lambda_handler(
        event,
        context=lambda_context,
        claude=_fake_claude(VALID_CODEGEN_OUTPUT),
        runs_repo=RunsRepository(),
        artifacts_repo=ArtifactsRepository(),
    )

    assert result["status"] == "GEN_READY"
    assert result["gen_version"] == 1
    assert result["files_count"] == 3
    assert result["gen_manifest_s3_uri"].startswith(
        f"s3://{s3_bucket}/artifacts/run-gen/v1/"
    )

    s3 = boto3.client("s3", region_name="us-east-1")
    manifest = json.loads(
        s3.get_object(Bucket=s3_bucket, Key="artifacts/run-gen/v1/manifest.json")[
            "Body"
        ].read()
    )
    assert manifest["language"] == "python"
    assert manifest["entry_point"] == "app.py"
    assert {f["path"] for f in manifest["files"]} == {
        "app.py",
        "requirements.txt",
        "tests/test_acceptance.py",
    }
    # Files themselves exist at their declared S3 URIs.
    app_obj = s3.get_object(Bucket=s3_bucket, Key="artifacts/run-gen/v1/app.py")
    assert b"def handler" in app_obj["Body"].read()

    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    item = ddb.Table(runs_table).get_item(
        Key={"run_id": "run-gen", "item_type": "GEN#v1"}
    )["Item"]
    assert item["status"] == "GEN_READY"
    assert item["version"] == 1
    assert item["payload"]["files_count"] == 3


def test_invalid_event_returns_failed(s3_bucket, runs_table, lambda_context):
    result = codegen_app.lambda_handler(
        {"run_id": "run-x"},  # missing spec_s3_uri
        context=lambda_context,
        claude=_fake_claude(VALID_CODEGEN_OUTPUT),
        runs_repo=RunsRepository(),
        artifacts_repo=ArtifactsRepository(),
    )
    assert result["status"] == "GEN_FAILED"
    assert "invalid_event" in result["error"]


def test_path_traversal_is_rejected(s3_bucket, runs_table, lambda_context):
    spec_uri = _seed_spec(s3_bucket, "run-evil", SPEC_PAYLOAD)
    malicious = {
        **VALID_CODEGEN_OUTPUT,
        "files": [{"path": "../../etc/passwd", "content": "hacked"}],
    }

    result = codegen_app.lambda_handler(
        {"run_id": "run-evil", "spec_s3_uri": spec_uri, "iteration": 1},
        context=lambda_context,
        claude=_fake_claude(malicious),
        runs_repo=RunsRepository(),
        artifacts_repo=ArtifactsRepository(),
    )

    assert result["status"] == "GEN_FAILED"
    assert "codegen_output_invalid" in result["error"]
    # Nothing leaked to S3 under the run prefix.
    s3 = boto3.client("s3", region_name="us-east-1")
    listing = s3.list_objects_v2(Bucket=s3_bucket, Prefix="artifacts/run-evil/")
    assert "Contents" not in listing


def test_malformed_llm_output_returns_failed(s3_bucket, runs_table, lambda_context):
    spec_uri = _seed_spec(s3_bucket, "run-bad", SPEC_PAYLOAD)
    fake = MagicMock()
    fake.complete.return_value = "not json at all {{{"

    result = codegen_app.lambda_handler(
        {"run_id": "run-bad", "spec_s3_uri": spec_uri, "iteration": 1},
        context=lambda_context,
        claude=fake,
        runs_repo=RunsRepository(),
        artifacts_repo=ArtifactsRepository(),
    )
    assert result["status"] == "GEN_FAILED"
    assert "codegen_output_invalid" in result["error"]


def test_fenced_json_still_parsed(s3_bucket, runs_table, lambda_context):
    spec_uri = _seed_spec(s3_bucket, "run-fenced", SPEC_PAYLOAD)
    fenced = "```json\n" + json.dumps(VALID_CODEGEN_OUTPUT) + "\n```"
    fake = MagicMock()
    fake.complete.return_value = fenced

    result = codegen_app.lambda_handler(
        {"run_id": "run-fenced", "spec_s3_uri": spec_uri, "iteration": 1},
        context=lambda_context,
        claude=fake,
        runs_repo=RunsRepository(),
        artifacts_repo=ArtifactsRepository(),
    )
    assert result["status"] == "GEN_READY"
    assert result["files_count"] == 3
