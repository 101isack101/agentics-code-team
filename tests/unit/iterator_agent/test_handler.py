"""Unit tests for the Iterator Agent.

The Iterator is pure logic (no LLM), so every test is a deterministic
assertion over its 4 decision branches:
  - ITERATOR_DONE      (all 3 validators passed)
  - ITERATOR_READY     (some failed, under MAX_ITERATIONS, issues not persistent)
  - ITERATOR_EXHAUSTED (some failed, iteration == MAX_ITERATIONS)
  - ITERATOR_UNRESOLVABLE (same issue across 3 iterations)
"""

from __future__ import annotations

import json

import boto3
import pytest

import iterator as iterator_app
from dynamo_repo import RunsRepository
from iterator_schemas import MAX_ITERATIONS
from s3_repo import ArtifactsRepository


def _seed_report(
    bucket: str,
    run_id: str,
    gen_version: int,
    validator: str,
    score: int,
    passed: bool,
    issues: list[dict],
) -> str:
    """Write a validator report to S3 exactly as run_validator would."""
    s3 = boto3.client("s3", region_name="us-east-1")
    key = f"artifacts/{run_id}/v{gen_version}/{validator}.json"
    body = {
        "validator": validator,
        "run_id": run_id,
        "gen_version": gen_version,
        "score": score,
        "pass": passed,
        "summary": "",
        "issues": issues,
    }
    s3.put_object(Bucket=bucket, Key=key, Body=json.dumps(body).encode("utf-8"))
    return f"s3://{bucket}/{key}"


def _make_validator_result(
    run_id: str,
    gen_version: int,
    validator: str,
    score: int,
    passed: bool,
    report_uri: str,
) -> dict:
    """Mirror the ValidatorResult shape emitted by run_validator."""
    return {
        "run_id": run_id,
        "status": f"{validator.upper()}_READY",
        "gen_version": gen_version,
        "score": score,
        "passed": passed,
        "report_s3_uri": report_uri,
    }


def _build_event(
    s3_bucket: str,
    run_id: str,
    gen_version: int,
    *,
    review: dict,
    security: dict,
    scalability: dict,
) -> dict:
    return {
        "run_id": run_id,
        "spec_s3_uri": f"s3://{s3_bucket}/specs/{run_id}/technical_spec.json",
        "gen_manifest_s3_uri": (
            f"s3://{s3_bucket}/artifacts/{run_id}/v{gen_version}/manifest.json"
        ),
        "gen_version": gen_version,
        "review_result": review,
        "security_result": security,
        "scalability_result": scalability,
    }


def test_iterator_all_passed_returns_done(s3_bucket, runs_table, lambda_context):
    run_id = "run-done"
    gv = 1
    # All three reports exist and pass; no issues.
    results = {}
    for name in ("review", "security", "scalability"):
        uri = _seed_report(s3_bucket, run_id, gv, name, score=100, passed=True, issues=[])
        results[name] = _make_validator_result(run_id, gv, name, 100, True, uri)

    event = _build_event(
        s3_bucket, run_id, gv,
        review=results["review"],
        security=results["security"],
        scalability=results["scalability"],
    )
    out = iterator_app.lambda_handler(
        event,
        context=lambda_context,
        runs_repo=RunsRepository(),
        artifacts_repo=ArtifactsRepository(),
    )

    assert out["status"] == "ITERATOR_DONE"
    assert out["all_passed"] is True
    assert out["iteration"] == gv

    # DynamoDB carries the convergence record.
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    item = ddb.Table(runs_table).get_item(
        Key={"run_id": run_id, "item_type": f"ITERATION#v{gv}"}
    )["Item"]
    assert item["status"] == "ITERATOR_DONE"
    assert item["payload"]["all_passed"] is True


def test_iterator_failures_under_cap_returns_ready(s3_bucket, runs_table, lambda_context):
    run_id = "run-ready"
    gv = 1
    # Review passes, security fails with a blocker, scalability passes.
    uri_review = _seed_report(s3_bucket, run_id, gv, "review", 95, True, [])
    uri_sec = _seed_report(
        s3_bucket, run_id, gv, "security", 70, False,
        issues=[
            {
                "id": "SEC-001",
                "severity": "blocker",
                "category": "owasp_a03",
                "file": "app.py",
                "line": 12,
                "description": "Unsanitized user input concatenated into SQL.",
                "remediation": "Use parameterized queries.",
                "metadata": {},
            }
        ],
    )
    uri_scal = _seed_report(s3_bucket, run_id, gv, "scalability", 90, True, [])

    event = _build_event(
        s3_bucket, run_id, gv,
        review=_make_validator_result(run_id, gv, "review", 95, True, uri_review),
        security=_make_validator_result(run_id, gv, "security", 70, False, uri_sec),
        scalability=_make_validator_result(run_id, gv, "scalability", 90, True, uri_scal),
    )
    out = iterator_app.lambda_handler(
        event,
        context=lambda_context,
        runs_repo=RunsRepository(),
        artifacts_repo=ArtifactsRepository(),
    )

    assert out["status"] == "ITERATOR_READY"
    assert out["all_passed"] is False
    assert out["next_iteration"] == gv + 1
    assert out["prior_manifest_s3_uri"].endswith(f"v{gv}/manifest.json")
    assert len(out["issues_to_fix"]) == 1
    assert out["issues_to_fix"][0]["validator"] == "security"
    assert out["issues_to_fix"][0]["severity"] == "blocker"

    # Persisted fingerprints for the next iteration to compare against.
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    item = ddb.Table(runs_table).get_item(
        Key={"run_id": run_id, "item_type": f"ITERATION#v{gv}"}
    )["Item"]
    assert len(item["payload"]["current_fingerprints"]) == 1


def test_iterator_info_issues_are_not_forwarded_as_fixes(s3_bucket, runs_table, lambda_context):
    """`info` severity never blocks passed, so we don't waste tokens on it."""
    run_id = "run-info-only"
    gv = 1
    # Security fails (score < 85) but the only issue is `info`.
    uri_review = _seed_report(s3_bucket, run_id, gv, "review", 95, True, [])
    uri_sec = _seed_report(
        s3_bucket, run_id, gv, "security", 60, False,
        issues=[
            {
                "id": "SEC-001",
                "severity": "info",
                "category": "style",
                "file": "app.py",
                "line": 1,
                "description": "Consider adding a module docstring.",
                "remediation": "Add one-line docstring.",
                "metadata": {},
            }
        ],
    )
    uri_scal = _seed_report(s3_bucket, run_id, gv, "scalability", 90, True, [])

    event = _build_event(
        s3_bucket, run_id, gv,
        review=_make_validator_result(run_id, gv, "review", 95, True, uri_review),
        security=_make_validator_result(run_id, gv, "security", 60, False, uri_sec),
        scalability=_make_validator_result(run_id, gv, "scalability", 90, True, uri_scal),
    )
    out = iterator_app.lambda_handler(
        event,
        context=lambda_context,
        runs_repo=RunsRepository(),
        artifacts_repo=ArtifactsRepository(),
    )
    assert out["status"] == "ITERATOR_READY"
    assert out["issues_to_fix"] == []  # info is filtered out


def test_iterator_exhausted_at_max_iterations(s3_bucket, runs_table, lambda_context):
    run_id = "run-exhausted"
    gv = MAX_ITERATIONS  # we are already at the cap

    # Seed prior iteration fingerprint records so the agent doesn't fail.
    runs_repo = RunsRepository()
    for prior_v in range(1, gv):
        runs_repo.create_item(
            run_id=run_id,
            item_type=f"ITERATION#v{prior_v}",
            status="ITERATOR_RECORDED",
            payload={
                "current_fingerprints": [],
                "persistent_fingerprints": [],
            },
        )

    # A fresh issue (not persistent) but we've run out of iterations anyway.
    uri_review = _seed_report(s3_bucket, run_id, gv, "review", 95, True, [])
    uri_sec = _seed_report(
        s3_bucket, run_id, gv, "security", 60, False,
        issues=[
            {
                "id": "SEC-001",
                "severity": "critical",
                "category": "owasp_a02",
                "file": "app.py",
                "line": 9,
                "description": "Passwords logged in plaintext.",
                "remediation": "Remove logging or hash first.",
                "metadata": {},
            }
        ],
    )
    uri_scal = _seed_report(s3_bucket, run_id, gv, "scalability", 90, True, [])

    event = _build_event(
        s3_bucket, run_id, gv,
        review=_make_validator_result(run_id, gv, "review", 95, True, uri_review),
        security=_make_validator_result(run_id, gv, "security", 60, False, uri_sec),
        scalability=_make_validator_result(run_id, gv, "scalability", 90, True, uri_scal),
    )
    out = iterator_app.lambda_handler(
        event,
        context=lambda_context,
        runs_repo=runs_repo,
        artifacts_repo=ArtifactsRepository(),
    )
    assert out["status"] == "ITERATOR_EXHAUSTED"
    assert out["all_passed"] is False
    assert out["next_iteration"] is None
    assert "MAX_ITERATIONS" in out["error"]


def test_iterator_unresolvable_when_issue_persists_across_three_iterations(
    s3_bucket, runs_table, lambda_context
):
    """Same fingerprint in iters 1, 2 and 3 -> ITERATOR_UNRESOLVABLE at iter 3."""
    run_id = "run-pingpong"
    runs_repo = RunsRepository()
    artifacts_repo = ArtifactsRepository()

    # We're going to hand-craft the exact same issue in iter 2 and then run
    # the Iterator for iter 3 with the same issue still there.
    offending_issue = {
        "id": "SEC-001",
        "severity": "blocker",
        "category": "owasp_a03",
        "file": "app.py",
        "line": 12,
        "description": "Unsanitized user input concatenated into SQL.",
        "remediation": "Use parameterized queries.",
        "metadata": {},
    }

    # --- Compute the fingerprint the way iterator.py does ---
    from iterator import _fingerprint  # noqa: PLC0415 — test introspection
    fp = _fingerprint(
        offending_issue["category"],
        offending_issue["file"],
        offending_issue["description"],
    )

    # Iteration 1 recorded (issue was new then).
    runs_repo.create_item(
        run_id=run_id,
        item_type="ITERATION#v1",
        status="ITERATOR_RECORDED",
        payload={
            "current_fingerprints": [fp],
            "persistent_fingerprints": [],
        },
    )
    # Iteration 2 recorded (issue persisted from iter 1).
    runs_repo.create_item(
        run_id=run_id,
        item_type="ITERATION#v2",
        status="ITERATOR_RECORDED",
        payload={
            "current_fingerprints": [fp],
            "persistent_fingerprints": [fp],  # already persistent
        },
    )

    # Now run the Iterator for iter 3 with the same issue STILL present.
    gv = 3
    uri_review = _seed_report(s3_bucket, run_id, gv, "review", 95, True, [])
    uri_sec = _seed_report(
        s3_bucket, run_id, gv, "security", 70, False, issues=[offending_issue]
    )
    uri_scal = _seed_report(s3_bucket, run_id, gv, "scalability", 90, True, [])

    event = _build_event(
        s3_bucket, run_id, gv,
        review=_make_validator_result(run_id, gv, "review", 95, True, uri_review),
        security=_make_validator_result(run_id, gv, "security", 70, False, uri_sec),
        scalability=_make_validator_result(run_id, gv, "scalability", 90, True, uri_scal),
    )
    out = iterator_app.lambda_handler(
        event,
        context=lambda_context,
        runs_repo=runs_repo,
        artifacts_repo=artifacts_repo,
    )

    assert out["status"] == "ITERATOR_UNRESOLVABLE"
    assert fp in out["persistent_fingerprints"]
    assert "persisted" in out["error"]


def test_iterator_invalid_event_returns_failed(lambda_context):
    # Missing required fields (no review_result etc).
    out = iterator_app.lambda_handler(
        {"run_id": "nope"},
        context=lambda_context,
    )
    assert out["status"] == "ITERATOR_FAILED"
    assert "invalid_event" in out["error"]


def test_iterator_fingerprint_is_stable_for_same_issue():
    from iterator import _fingerprint  # noqa: PLC0415
    a = _fingerprint("owasp_a03", "app.py", "Unsanitized user input concatenated into SQL.")
    b = _fingerprint("owasp_a03", "app.py", "Unsanitized user input concatenated into SQL.")
    c = _fingerprint("owasp_a03", "app.py", "Totally different problem.")
    assert a == b
    assert a != c


@pytest.mark.parametrize(
    ("file_a", "file_b", "should_differ"),
    [
        ("app.py", "api.py", True),
        ("app.py", "app.py", False),
        (None, "app.py", True),
    ],
)
def test_iterator_fingerprint_is_file_sensitive(file_a, file_b, should_differ):
    from iterator import _fingerprint  # noqa: PLC0415
    a = _fingerprint("cat", file_a, "desc")
    b = _fingerprint("cat", file_b, "desc")
    assert (a != b) is should_differ


@pytest.mark.parametrize(
    ("env_value", "expected"),
    [
        ("1", 1),
        ("5", 5),
        ("99", 10),      # clamp upper
        ("0", 1),        # clamp lower
        ("not-a-number", 3),  # fallback
    ],
)
def test_max_iterations_respects_env_var(monkeypatch, env_value, expected):
    monkeypatch.setenv("MAX_ITERATIONS", env_value)
    from iterator_schemas import _resolve_max_iterations  # noqa: PLC0415
    assert _resolve_max_iterations() == expected
