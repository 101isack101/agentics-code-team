"""Unit tests for the Spec Agent lambda handler.

No real AWS, no real Anthropic call. We inject a fake ClaudeClient.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import boto3
import pytest

import app as spec_agent_app
from dynamo_repo import RunsRepository
from s3_repo import ArtifactsRepository


VALID_SPEC_JSON = {
    "spec_version": "1.0",
    "run_id": "run-abc",
    "project_name": "DemoApp",
    "complexity": "simple",
    "language": "python",
    "summary": "A tiny demo API that echoes input.",
    "user_stories": [
        "As a user I want to POST text so that I receive an echo response."
    ],
    "functional_requirements": ["FR-001: POST /echo accepts a string and returns it"],
    "non_functional_requirements": [],
    "acceptance_criteria": [
        {
            "id": "AC-001",
            "description": "Given payload 'hi' When POST /echo Then response == 'hi'",
            "testable": True,
        }
    ],
    "api_contracts": [
        {
            "method": "POST",
            "path": "/echo",
            "summary": "Echo input back.",
            "request_schema": {"text": "string"},
            "response_schema": {"text": "string"},
            "auth_required": False,
        }
    ],
    "data_entities": [],
    "constraints": ["ASSUMPTION: stateless service"],
    "out_of_scope": ["persistence layer"],
}


def _make_fake_claude(payload_json: dict) -> MagicMock:
    fake = MagicMock()
    fake.complete.return_value = json.dumps(payload_json)
    return fake


def test_happy_path_produces_spec_ready(s3_bucket, runs_table, lambda_context):
    event = {
        "run_id": "run-abc",
        "project_name": "DemoApp",
        "requirements": "Build a tiny echo API with one POST endpoint.",
        "language": "python",
        "complexity": "simple",
    }
    fake_claude = _make_fake_claude(VALID_SPEC_JSON)

    result = spec_agent_app.lambda_handler(
        event,
        context=lambda_context,
        claude=fake_claude,
        runs_repo=RunsRepository(),
        artifacts_repo=ArtifactsRepository(),
    )

    assert result["status"] == "SPEC_READY"
    assert result["run_id"] == "run-abc"
    assert result["spec_s3_uri"].startswith(f"s3://{s3_bucket}/specs/run-abc/")
    assert result["spec_version"] == 1

    s3 = boto3.client("s3", region_name="us-east-1")
    obj = s3.get_object(Bucket=s3_bucket, Key="specs/run-abc/technical_spec.json")
    saved = json.loads(obj["Body"].read())
    assert saved["functional_requirements"] == VALID_SPEC_JSON["functional_requirements"]

    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    item = ddb.Table(runs_table).get_item(
        Key={"run_id": "run-abc", "item_type": "SPEC"}
    )["Item"]
    assert item["version"] == 1
    assert item["status"] == "SPEC_READY"


def test_invalid_event_returns_failed(s3_bucket, runs_table, lambda_context):
    bad_event = {"run_id": "r", "project_name": "p"}  # missing requirements
    fake_claude = _make_fake_claude(VALID_SPEC_JSON)

    result = spec_agent_app.lambda_handler(
        bad_event,
        context=lambda_context,
        claude=fake_claude,
        runs_repo=RunsRepository(),
        artifacts_repo=ArtifactsRepository(),
    )
    assert result["status"] == "SPEC_FAILED"
    assert "invalid_event" in result["error"]
    fake_claude.complete.assert_not_called()


def test_claude_returns_fenced_json_is_still_parsed(s3_bucket, runs_table, lambda_context):
    fenced = "```json\n" + json.dumps(VALID_SPEC_JSON) + "\n```"
    fake_claude = MagicMock()
    fake_claude.complete.return_value = fenced

    event = {
        "run_id": "run-fenced",
        "project_name": "DemoApp",
        "requirements": "Build something simple please.",
        "language": "python",
        "complexity": "simple",
    }
    # run_id in the fake spec must match to pass validation
    VALID_SPEC_JSON["run_id"] = "run-fenced"
    fake_claude.complete.return_value = "```json\n" + json.dumps(VALID_SPEC_JSON) + "\n```"

    result = spec_agent_app.lambda_handler(
        event,
        context=lambda_context,
        claude=fake_claude,
        runs_repo=RunsRepository(),
        artifacts_repo=ArtifactsRepository(),
    )
    VALID_SPEC_JSON["run_id"] = "run-abc"  # restore for other tests
    assert result["status"] == "SPEC_READY"


def test_project_name_with_invalid_chars_rejected(s3_bucket, runs_table, lambda_context):
    fake_claude = _make_fake_claude(VALID_SPEC_JSON)
    event = {
        "run_id": "run-abc",
        "project_name": "Demo;DROP TABLE",
        "requirements": "Build a tiny echo API with one POST endpoint.",
        "language": "python",
        "complexity": "simple",
    }
    result = spec_agent_app.lambda_handler(
        event,
        context=lambda_context,
        claude=fake_claude,
        runs_repo=RunsRepository(),
        artifacts_repo=ArtifactsRepository(),
    )
    assert result["status"] == "SPEC_FAILED"
    assert "invalid_event" in result["error"]
    fake_claude.complete.assert_not_called()


def test_run_id_path_traversal_rejected(s3_bucket, runs_table, lambda_context):
    fake_claude = _make_fake_claude(VALID_SPEC_JSON)
    event = {
        "run_id": "../../etc/passwd",
        "project_name": "DemoApp",
        "requirements": "Build a tiny echo API with one POST endpoint.",
        "language": "python",
        "complexity": "simple",
    }
    result = spec_agent_app.lambda_handler(
        event,
        context=lambda_context,
        claude=fake_claude,
        runs_repo=RunsRepository(),
        artifacts_repo=ArtifactsRepository(),
    )
    assert result["status"] == "SPEC_FAILED"
    assert "invalid_event" in result["error"]
    fake_claude.complete.assert_not_called()


def test_unsupported_language_rejected(s3_bucket, runs_table, lambda_context):
    fake_claude = _make_fake_claude(VALID_SPEC_JSON)
    event = {
        "run_id": "run-abc",
        "project_name": "DemoApp",
        "requirements": "Build a tiny echo API with one POST endpoint.",
        "language": "cobol",
        "complexity": "simple",
    }
    result = spec_agent_app.lambda_handler(
        event,
        context=lambda_context,
        claude=fake_claude,
        runs_repo=RunsRepository(),
        artifacts_repo=ArtifactsRepository(),
    )
    assert result["status"] == "SPEC_FAILED"
    assert "invalid_event" in result["error"]
    fake_claude.complete.assert_not_called()


def test_malformed_spec_returns_failed(s3_bucket, runs_table, lambda_context):
    fake_claude = MagicMock()
    fake_claude.complete.return_value = '{"this": "is not a valid spec"}'

    event = {
        "run_id": "run-bad",
        "project_name": "DemoApp",
        "requirements": "Build something simple please.",
        "language": "python",
        "complexity": "simple",
    }
    result = spec_agent_app.lambda_handler(
        event,
        context=lambda_context,
        claude=fake_claude,
        runs_repo=RunsRepository(),
        artifacts_repo=ArtifactsRepository(),
    )
    assert result["status"] == "SPEC_FAILED"
    assert "spec_validation_failed" in result["error"]
