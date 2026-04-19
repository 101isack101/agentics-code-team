"""Iterator Agent Lambda handler.

Deterministic, pure-logic Lambda (no LLM call). Responsibilities:

    1. Read the 3 validator reports referenced in the event.
    2. If all three passed → emit status=ITERATOR_DONE (all_passed=True).
    3. Else compute issue fingerprints = sha1(category|file|description[:80]).
       - Compare with fingerprints persisted from iteration N-1 in DynamoDB.
       - A fingerprint that appears in N-1 and N is "persistent".
       - If any fingerprint has been persistent for 2 consecutive iterations
         (i.e. appeared in N-2, N-1, and N) → ITERATOR_UNRESOLVABLE.
    4. If current iteration >= MAX_ITERATIONS → ITERATOR_EXHAUSTED.
    5. Otherwise → ITERATOR_READY with the CodeGen fix-mode inputs.

    6. Persist ITERATION#v{n} in DynamoDB with fingerprints for the next run.

Input event shape: `IteratorEvent` (see iterator_schemas).
"""

from __future__ import annotations

import hashlib
from typing import Any

from aws_lambda_powertools import Logger, Tracer
from pydantic import ValidationError

from dynamo_repo import RunsRepository
from metrics import emit_iterator_outcome, instrument_agent
from s3_repo import ArtifactsRepository

from iterator_schemas import (
    IssueFingerprint,
    IteratorEvent,
    IteratorResult,
    _resolve_max_iterations,
)


logger = Logger()
tracer = Tracer()


_DESCRIPTION_HEAD_LEN = 80
_FINGERPRINT_LEN = 16


def _fingerprint(category: str, file: str | None, description: str) -> str:
    head = (description or "")[:_DESCRIPTION_HEAD_LEN]
    material = f"{category}|{file or ''}|{head}".encode("utf-8")
    return hashlib.sha1(material).hexdigest()[:_FINGERPRINT_LEN]


def _load_report(result: dict, artifacts_repo: ArtifactsRepository) -> dict:
    uri = result.get("report_s3_uri")
    if not uri:
        raise ValueError(f"validator result missing report_s3_uri: {result}")
    _, key = ArtifactsRepository.parse_s3_uri(uri)
    return artifacts_repo.get_json(key)


def _collect_fingerprints(
    validator_name: str, report: dict
) -> list[IssueFingerprint]:
    fingerprints: list[IssueFingerprint] = []
    for issue in report.get("issues", []):
        desc = issue.get("description", "")
        category = issue.get("category", "uncategorized")
        file = issue.get("file")
        fingerprints.append(
            IssueFingerprint(
                validator=validator_name,
                category=category,
                file=file,
                description_head=desc[:_DESCRIPTION_HEAD_LEN],
                severity=issue.get("severity", "info"),
                fingerprint=_fingerprint(category, file, desc),
            )
        )
    return fingerprints


def _prior_iteration_record(
    run_id: str, current_iteration: int, runs_repo: RunsRepository
) -> dict | None:
    if current_iteration <= 1:
        return None
    item = runs_repo.get_item(run_id, f"ITERATION#v{current_iteration - 1}")
    return item.get("payload") if item else None


_FIX_SEVERITY_PRIORITY = {"blocker": 0, "critical": 1, "major": 2, "minor": 3}
_MAX_ISSUES_TO_FIX = 8


def _issues_for_fix(reports: dict[str, dict]) -> list[dict]:
    """Flatten the 3 reports into a prioritised list CodeGen can consume.

    Only blocker/critical/major/minor severities are forwarded (info is noise).
    We cap at _MAX_ISSUES_TO_FIX sorted by severity to keep the fix-pass prompt
    small enough for Sonnet to generate valid JSON within the token budget.
    """
    payload: list[dict] = []
    for validator_name, report in reports.items():
        for issue in report.get("issues", []):
            sev = issue.get("severity")
            if sev not in _FIX_SEVERITY_PRIORITY:
                continue
            payload.append(
                {
                    "validator": validator_name,
                    "severity": sev,
                    "category": issue.get("category"),
                    "file": issue.get("file"),
                    "line": issue.get("line"),
                    "description": issue.get("description"),
                    "remediation": issue.get("remediation"),
                }
            )
    payload.sort(key=lambda x: _FIX_SEVERITY_PRIORITY.get(x["severity"], 99))
    return payload[:_MAX_ISSUES_TO_FIX]


@logger.inject_lambda_context(log_event=True)
@tracer.capture_lambda_handler
@instrument_agent("iterator_agent")
def lambda_handler(
    event: dict[str, Any],
    context: Any,
    runs_repo: RunsRepository | None = None,
    artifacts_repo: ArtifactsRepository | None = None,
) -> dict[str, Any]:
    try:
        return _run(event, runs_repo, artifacts_repo)
    except Exception as exc:
        logger.exception("iterator_unhandled_error")
        return IteratorResult(
            run_id=event.get("run_id", "unknown") if isinstance(event, dict) else "unknown",
            status="ITERATOR_FAILED",
            gen_version=int((event or {}).get("gen_version", 1) or 1) if isinstance(event, dict) else 1,
            iteration=int((event or {}).get("gen_version", 1) or 1) if isinstance(event, dict) else 1,
            error=f"unhandled_error: {type(exc).__name__}",
        ).model_dump()


def _run(
    event: dict[str, Any],
    runs_repo: RunsRepository | None,
    artifacts_repo: ArtifactsRepository | None,
) -> dict[str, Any]:
    max_iterations = _resolve_max_iterations()
    try:
        parsed = IteratorEvent.model_validate(event)
    except ValidationError as exc:
        logger.exception("invalid_iterator_event")
        return IteratorResult(
            run_id=event.get("run_id", "unknown"),
            status="ITERATOR_FAILED",
            gen_version=int(event.get("gen_version", 1) or 1),
            iteration=int(event.get("gen_version", 1) or 1),
            error=f"invalid_event: {exc.errors()}",
        ).model_dump()

    runs_repo = runs_repo or RunsRepository()
    artifacts_repo = artifacts_repo or ArtifactsRepository()

    iteration = parsed.gen_version
    logger.info(
        "iterator_start",
        extra={"run_id": parsed.run_id, "iteration": iteration},
    )

    results = {
        "review": parsed.review_result,
        "security": parsed.security_result,
        "scalability": parsed.scalability_result,
    }
    all_passed = all(bool(r.get("passed")) for r in results.values())

    if all_passed:
        result = IteratorResult(
            run_id=parsed.run_id,
            status="ITERATOR_DONE",
            gen_version=iteration,
            iteration=iteration,
            all_passed=True,
        )
        runs_repo.create_item(
            run_id=parsed.run_id,
            item_type=f"ITERATION#v{iteration}",
            status="ITERATOR_DONE",
            payload={
                "all_passed": True,
                "persistent_fingerprints": [],
                "new_fingerprints": [],
            },
        )
        logger.info("iterator_done_converged", extra={"run_id": parsed.run_id})
        emit_iterator_outcome("done")
        return result.model_dump()

    # Build fingerprints for the current iteration.
    try:
        reports = {
            name: _load_report(res, artifacts_repo) for name, res in results.items()
        }
    except Exception as exc:
        logger.exception("iterator_report_load_failed")
        return IteratorResult(
            run_id=parsed.run_id,
            status="ITERATOR_FAILED",
            gen_version=iteration,
            iteration=iteration,
            error=f"report_load_failed: {exc}",
        ).model_dump()

    current_fps: list[IssueFingerprint] = []
    for name, report in reports.items():
        current_fps.extend(_collect_fingerprints(name, report))
    current_set = {fp.fingerprint for fp in current_fps}

    # Ping-pong detection against iteration N-1 and N-2.
    prior_payload = _prior_iteration_record(parsed.run_id, iteration, runs_repo)
    prior_set = set(prior_payload.get("persistent_fingerprints", [])) if prior_payload else set()
    prior_current_set = set(prior_payload.get("current_fingerprints", [])) if prior_payload else set()

    # Persistent = appeared in last iteration AND still here now.
    persistent = sorted(current_set & prior_current_set)
    # Unresolvable = already persistent last iteration AND still here.
    unresolvable = sorted(current_set & prior_set)

    # Persist THIS iteration's fingerprints for the next run.
    iteration_payload = {
        "current_fingerprints": sorted(current_set),
        "persistent_fingerprints": persistent,
        "fingerprint_details": [fp.model_dump() for fp in current_fps],
        "validator_scores": {
            name: results[name].get("score") for name in results
        },
    }
    runs_repo.create_item(
        run_id=parsed.run_id,
        item_type=f"ITERATION#v{iteration}",
        status="ITERATOR_RECORDED",
        payload=iteration_payload,
    )

    if unresolvable:
        logger.warning(
            "iterator_unresolvable",
            extra={
                "run_id": parsed.run_id,
                "iteration": iteration,
                "unresolvable_count": len(unresolvable),
            },
        )
        emit_iterator_outcome("unresolvable")
        return IteratorResult(
            run_id=parsed.run_id,
            status="ITERATOR_UNRESOLVABLE",
            gen_version=iteration,
            iteration=iteration,
            all_passed=False,
            persistent_fingerprints=unresolvable,
            new_fingerprints=sorted(current_set - prior_current_set),
            error=(
                f"{len(unresolvable)} issue(s) persisted across 3 iterations"
            ),
        ).model_dump()

    if iteration >= max_iterations:
        logger.warning(
            "iterator_exhausted",
            extra={"run_id": parsed.run_id, "iteration": iteration},
        )
        emit_iterator_outcome("exhausted")
        return IteratorResult(
            run_id=parsed.run_id,
            status="ITERATOR_EXHAUSTED",
            gen_version=iteration,
            iteration=iteration,
            all_passed=False,
            persistent_fingerprints=persistent,
            new_fingerprints=sorted(current_set - prior_current_set),
            error=f"MAX_ITERATIONS={max_iterations} reached without convergence",
        ).model_dump()

    # Approve next iteration.
    next_iteration = iteration + 1
    issues_to_fix = _issues_for_fix(reports)
    logger.info(
        "iterator_approve_next",
        extra={
            "run_id": parsed.run_id,
            "iteration": iteration,
            "next_iteration": next_iteration,
            "issues_to_fix": len(issues_to_fix),
            "persistent_count": len(persistent),
        },
    )
    emit_iterator_outcome("ready")
    return IteratorResult(
        run_id=parsed.run_id,
        status="ITERATOR_READY",
        gen_version=iteration,
        iteration=iteration,
        all_passed=False,
        next_iteration=next_iteration,
        prior_manifest_s3_uri=parsed.gen_manifest_s3_uri,
        issues_to_fix=issues_to_fix,
        persistent_fingerprints=persistent,
        new_fingerprints=sorted(current_set - prior_current_set),
    ).model_dump()
