"""Unit tests for Review / Security / Scalability validator Lambdas.

All three share the `validator_runner`. We parameterize across the three
agents to ensure each wiring is correct.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import boto3
import pytest

import review as review_app
import scalability as scalability_app
import security as security_app
from dynamo_repo import RunsRepository
from s3_repo import ArtifactsRepository
from validator_runner import calculate_passed
from validator_schemas import ValidatorIssue, ValidatorReport


SPEC_PAYLOAD = {
    "spec_version": "1.0",
    "run_id": "run-val",
    "project_name": "DemoVal",
    "complexity": "simple",
    "language": "python",
    "summary": "A tiny API with one endpoint.",
    "user_stories": ["As a user I want POST /echo"],
    "functional_requirements": ["FR-001: Echo the text field"],
    "non_functional_requirements": [],
    "acceptance_criteria": [
        {"id": "AC-001", "description": "echoes input", "testable": True}
    ],
    "api_contracts": [],
    "data_entities": [],
    "constraints": [],
    "out_of_scope": [],
}


def _seed_generated_source(bucket: str, run_id: str, version: int) -> str:
    """Seed a codegen v1 tree (manifest + a couple of files) and return manifest s3 URI."""
    s3 = boto3.client("s3", region_name="us-east-1")
    base = f"artifacts/{run_id}/v{version}"

    files = [
        ("app.py", "def handler(event, _):\n    return event\n"),
        ("requirements.txt", "boto3\n"),
    ]
    manifest_files = []
    for path, content in files:
        key = f"{base}/{path}"
        s3.put_object(Bucket=bucket, Key=key, Body=content.encode("utf-8"))
        manifest_files.append(
            {"path": path, "s3_uri": f"s3://{bucket}/{key}", "size_bytes": len(content)}
        )

    spec_key = f"specs/{run_id}/technical_spec.json"
    spec_payload = {**SPEC_PAYLOAD, "run_id": run_id}
    s3.put_object(
        Bucket=bucket, Key=spec_key, Body=json.dumps(spec_payload).encode("utf-8")
    )

    manifest = {
        "run_id": run_id,
        "gen_version": version,
        "language": "python",
        "entry_point": "app.py",
        "files": manifest_files,
        "notes": "seeded",
    }
    manifest_key = f"{base}/manifest.json"
    s3.put_object(
        Bucket=bucket, Key=manifest_key, Body=json.dumps(manifest).encode("utf-8")
    )
    return f"s3://{bucket}/{manifest_key}"


def _fake_claude(report: dict) -> MagicMock:
    fake = MagicMock()
    fake.complete.return_value = json.dumps(report)
    return fake


VALIDATOR_CASES = [
    # (app_module, validator_name, ready_status, failed_status, item_prefix, report_filename)
    (review_app, "review", "REVIEW_READY", "REVIEW_FAILED", "REVIEW", "review.json"),
    (
        security_app,
        "security",
        "SECURITY_READY",
        "SECURITY_FAILED",
        "SECURITY",
        "security.json",
    ),
    (
        scalability_app,
        "scalability",
        "SCALABILITY_READY",
        "SCALABILITY_FAILED",
        "SCALABILITY",
        "scalability.json",
    ),
]


@pytest.mark.parametrize(
    ("app_module", "validator_name", "ready_status", "failed_status", "item_prefix", "report_filename"),
    VALIDATOR_CASES,
)
def test_validator_happy_path(
    s3_bucket,
    runs_table,
    lambda_context,
    app_module,
    validator_name,
    ready_status,
    failed_status,
    item_prefix,
    report_filename,
):
    run_id = f"run-{validator_name}"
    manifest_uri = _seed_generated_source(s3_bucket, run_id, version=1)
    spec_uri = f"s3://{s3_bucket}/specs/{run_id}/technical_spec.json"

    report_payload = {
        "validator": validator_name,
        "run_id": run_id,
        "gen_version": 1,
        "score": 88,
        "pass": True,
        "summary": "Looks clean; minor nitpicks only.",
        "issues": [
            {
                "id": "ISSUE-001",
                "severity": "minor",
                "category": "naming",
                "file": "app.py",
                "line": 1,
                "description": "Use a more descriptive function name.",
                "remediation": "Rename `handler` to `echo_handler`.",
                "metadata": {},
            }
        ],
    }

    event = {
        "run_id": run_id,
        "spec_s3_uri": spec_uri,
        "gen_manifest_s3_uri": manifest_uri,
        "gen_version": 1,
        "iteration": 1,
    }

    result = app_module.lambda_handler(
        event,
        context=lambda_context,
        claude=_fake_claude(report_payload),
        runs_repo=RunsRepository(),
        artifacts_repo=ArtifactsRepository(),
    )

    assert result["status"] == ready_status
    assert result["score"] == 88
    assert result["passed"] is True
    assert result["report_s3_uri"] == (
        f"s3://{s3_bucket}/artifacts/{run_id}/v1/{report_filename}"
    )

    s3 = boto3.client("s3", region_name="us-east-1")
    report = json.loads(
        s3.get_object(
            Bucket=s3_bucket, Key=f"artifacts/{run_id}/v1/{report_filename}"
        )["Body"].read()
    )
    assert report["validator"] == validator_name
    assert report["pass"] is True
    assert len(report["issues"]) == 1

    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    item = ddb.Table(runs_table).get_item(
        Key={"run_id": run_id, "item_type": f"{item_prefix}#v1"}
    )["Item"]
    assert item["status"] == ready_status
    assert item["payload"]["score"] == 88
    assert item["payload"]["passed"] is True


@pytest.mark.parametrize(
    ("app_module", "validator_name", "ready_status", "failed_status", "item_prefix", "report_filename"),
    VALIDATOR_CASES,
)
def test_validator_invalid_event_returns_failed(
    s3_bucket,
    runs_table,
    lambda_context,
    app_module,
    validator_name,
    ready_status,
    failed_status,
    item_prefix,
    report_filename,
):
    # gen_manifest_s3_uri missing
    event = {"run_id": "r", "spec_s3_uri": "s3://x/y", "gen_version": 1}
    result = app_module.lambda_handler(
        event,
        context=lambda_context,
        claude=_fake_claude({}),
        runs_repo=RunsRepository(),
        artifacts_repo=ArtifactsRepository(),
    )
    assert result["status"] == failed_status
    assert "invalid_event" in result["error"]


def test_validator_malformed_report_returns_failed(s3_bucket, runs_table, lambda_context):
    run_id = "run-review-bad"
    manifest_uri = _seed_generated_source(s3_bucket, run_id, version=1)
    spec_uri = f"s3://{s3_bucket}/specs/{run_id}/technical_spec.json"

    # Claude returns JSON but missing required fields (no `score`, no `pass`).
    fake = MagicMock()
    fake.complete.return_value = json.dumps({"validator": "review", "issues": []})

    event = {
        "run_id": run_id,
        "spec_s3_uri": spec_uri,
        "gen_manifest_s3_uri": manifest_uri,
        "gen_version": 1,
    }
    result = review_app.lambda_handler(
        event,
        context=lambda_context,
        claude=fake,
        runs_repo=RunsRepository(),
        artifacts_repo=ArtifactsRepository(),
    )
    assert result["status"] == "REVIEW_FAILED"
    assert "report_validation_failed" in result["error"]


def _make_report(score: int, severities: list[str]) -> ValidatorReport:
    return ValidatorReport(
        validator="review",
        run_id="run-x",
        gen_version=1,
        score=score,
        **{"pass": True},  # LLM-provided value — will be ignored by the gate.
        summary="",
        issues=[
            ValidatorIssue(
                id=f"ISSUE-{i:03d}",
                severity=sev,
                category="test",
                description="x",
                remediation="y",
            )
            for i, sev in enumerate(severities, start=1)
        ],
    )


@pytest.mark.parametrize(
    ("score", "severities", "expected"),
    [
        (100, [],                         True),   # perfect
        (85,  ["minor", "info"],          True),   # threshold edge, no blockers
        (84,  [],                         False),  # below threshold
        (95,  ["blocker"],                False),  # any blocker fails
        (95,  ["critical"],               False),  # any critical fails
        (90,  ["major", "major", "info"], True),   # majors are OK
        (85,  ["critical"],               False),  # threshold met, critical present
    ],
)
def test_calculate_passed_is_deterministic(score, severities, expected):
    report = _make_report(score, severities)
    assert calculate_passed(report) is expected


def test_llm_pass_true_is_overridden_when_blocker_present(
    s3_bucket, runs_table, lambda_context
):
    """The LLM may lie about `pass`. Our deterministic gate must win."""
    run_id = "run-override"
    manifest_uri = _seed_generated_source(s3_bucket, run_id, version=1)
    spec_uri = f"s3://{s3_bucket}/specs/{run_id}/technical_spec.json"

    lying_report = {
        "validator": "review",
        "run_id": run_id,
        "gen_version": 1,
        "score": 95,
        "pass": True,  # <-- lie
        "summary": "Looks fine.",
        "issues": [
            {
                "id": "ISSUE-001",
                "severity": "blocker",  # <-- contradicts the self-grade
                "category": "missing_requirement",
                "file": "app.py",
                "line": 1,
                "description": "FR-001 not implemented.",
                "remediation": "Implement the echo handler.",
                "metadata": {},
            }
        ],
    }
    event = {
        "run_id": run_id,
        "spec_s3_uri": spec_uri,
        "gen_manifest_s3_uri": manifest_uri,
        "gen_version": 1,
    }
    result = review_app.lambda_handler(
        event,
        context=lambda_context,
        claude=_fake_claude(lying_report),
        runs_repo=RunsRepository(),
        artifacts_repo=ArtifactsRepository(),
    )

    assert result["status"] == "REVIEW_READY"
    assert result["passed"] is False, "Deterministic gate must override LLM self-grade"

    s3 = boto3.client("s3", region_name="us-east-1")
    saved = json.loads(
        s3.get_object(
            Bucket=s3_bucket, Key=f"artifacts/{run_id}/v1/review.json"
        )["Body"].read()
    )
    assert saved["pass"] is False, "Persisted report must carry the deterministic value"

    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    item = ddb.Table(runs_table).get_item(
        Key={"run_id": run_id, "item_type": "REVIEW#v1"}
    )["Item"]
    assert item["payload"]["passed"] is False


def test_validator_fenced_json_is_parsed(s3_bucket, runs_table, lambda_context):
    run_id = "run-security-fenced"
    manifest_uri = _seed_generated_source(s3_bucket, run_id, version=1)
    spec_uri = f"s3://{s3_bucket}/specs/{run_id}/technical_spec.json"

    report_payload = {
        "validator": "security",
        "run_id": run_id,
        "gen_version": 1,
        "score": 100,
        "pass": True,
        "summary": "No issues found.",
        "issues": [],
    }
    fenced = "```json\n" + json.dumps(report_payload) + "\n```"
    fake = MagicMock()
    fake.complete.return_value = fenced

    event = {
        "run_id": run_id,
        "spec_s3_uri": spec_uri,
        "gen_manifest_s3_uri": manifest_uri,
        "gen_version": 1,
    }
    result = security_app.lambda_handler(
        event,
        context=lambda_context,
        claude=fake,
        runs_repo=RunsRepository(),
        artifacts_repo=ArtifactsRepository(),
    )
    assert result["status"] == "SECURITY_READY"
    assert result["score"] == 100
    assert result["passed"] is True
