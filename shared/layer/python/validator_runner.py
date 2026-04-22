"""Orchestrates the common skeleton of the three parallel validators.

Each validator Lambda differs only in:
  - its system prompt
  - its user-prompt builder
  - the ValidatorName literal
  - the DynamoDB item_type prefix and ready/failed statuses
  - the report filename in S3

Everything else (reading spec + manifest + files from S3, calling Claude,
parsing JSON, validating the report schema, persisting artifacts, writing
DynamoDB item) is identical and lives here.

Input event shape is `ValidatorEvent` (see validator_schemas).

Output is a JSON dict conforming to `ValidatorResult` — only S3/DynamoDB
pointers, never the raw report, so Step Functions payloads stay under 256KB.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from pydantic import ValidationError

from claude_client import ClaudeClient
from dynamo_repo import RunsRepository
from json_extractor import extract_json
from metrics import emit_validator_outcome
from s3_repo import ArtifactsRepository
from validator_schemas import (
    ValidatorEvent,
    ValidatorName,
    ValidatorReport,
    ValidatorResult,
)


# Map of file path -> content, passed to each validator's user-prompt builder.
UserPromptBuilder = Callable[[dict, list[dict]], str]


# Severity levels that disqualify a report from passing, regardless of score.
_BLOCKING_SEVERITIES: frozenset[str] = frozenset({"blocker", "critical"})

# Minimum score required to pass when no blocking-severity issues are present.
PASS_SCORE_THRESHOLD: int = 80


def calculate_passed(report: ValidatorReport) -> bool:
    """Deterministic pass gate — independent of the LLM's subjective judgment.

    A report passes iff:
      - score >= PASS_SCORE_THRESHOLD (85), AND
      - it has zero blocker-severity issues, AND
      - it has zero critical-severity issues.

    We trust the LLM to enumerate issues with concrete evidence (file + line),
    not to self-grade. The `pass` field Claude returns is discarded and
    overwritten with the value this function computes.
    """
    if report.score < PASS_SCORE_THRESHOLD:
        return False
    return all(issue.severity not in _BLOCKING_SEVERITIES for issue in report.issues)


def _load_files_from_manifest(
    manifest: dict,
    artifacts_repo: ArtifactsRepository,
) -> list[dict]:
    files: list[dict] = []
    for file_entry in manifest.get("files", []):
        _, key = ArtifactsRepository.parse_s3_uri(file_entry["s3_uri"])
        files.append(
            {
                "path": file_entry["path"],
                "content": artifacts_repo.get_text(key),
            }
        )
    return files


def run_validator(
    *,
    event: dict[str, Any],
    validator_name: ValidatorName,
    item_type_prefix: str,
    ready_status: str,
    failed_status: str,
    report_filename: str,
    system_prompt: str,
    build_user_prompt: UserPromptBuilder,
    logger,
    claude: ClaudeClient | None = None,
    runs_repo: RunsRepository | None = None,
    artifacts_repo: ArtifactsRepository | None = None,
) -> dict[str, Any]:
    try:
        parsed = ValidatorEvent.model_validate(event)
    except ValidationError as exc:
        logger.exception("invalid_validator_event")
        return ValidatorResult(
            run_id=event.get("run_id", "unknown"),
            status=failed_status,
            error=f"invalid_event: {exc.errors()}",
        ).model_dump()

    claude = claude or ClaudeClient()
    runs_repo = runs_repo or RunsRepository()
    artifacts_repo = artifacts_repo or ArtifactsRepository()

    logger.info(
        f"{validator_name}_start",
        extra={"run_id": parsed.run_id, "gen_version": parsed.gen_version},
    )

    _, spec_key = ArtifactsRepository.parse_s3_uri(parsed.spec_s3_uri)
    _, manifest_key = ArtifactsRepository.parse_s3_uri(parsed.gen_manifest_s3_uri)

    try:
        spec = artifacts_repo.get_json(spec_key)
        manifest = artifacts_repo.get_json(manifest_key)
        files = _load_files_from_manifest(manifest, artifacts_repo)
    except Exception as exc:
        logger.exception("validator_input_load_failed")
        return ValidatorResult(
            run_id=parsed.run_id,
            status=failed_status,
            error=f"input_load_failed: {exc}",
        ).model_dump()

    user_prompt = build_user_prompt(spec, files)
    try:
        raw = claude.complete(
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
            agent_name=validator_name,
        )
        data = extract_json(raw)
        data.setdefault("run_id", parsed.run_id)
        data.setdefault("gen_version", parsed.gen_version)
        data.setdefault("validator", validator_name)
        report = ValidatorReport.model_validate(data)
    except (ValidationError, json.JSONDecodeError) as exc:
        logger.exception(f"{validator_name}_report_invalid")
        return ValidatorResult(
            run_id=parsed.run_id,
            status=failed_status,
            error=f"report_validation_failed: {exc}",
        ).model_dump()

    # Deterministic pass gate overrides whatever the LLM wrote in `pass`.
    report.passed = calculate_passed(report)
    emit_validator_outcome(validator_name, report.passed)

    report_key = artifacts_repo.report_key(
        parsed.run_id, parsed.gen_version, report_filename
    )
    report_s3_uri = artifacts_repo.put_json(
        report_key, report.model_dump(by_alias=True)
    )

    item_type = f"{item_type_prefix}#v{parsed.gen_version}"
    runs_repo.create_item(
        run_id=parsed.run_id,
        item_type=item_type,
        status=ready_status,
        payload={
            "validator": validator_name,
            "report_s3_uri": report_s3_uri,
            "score": report.score,
            "passed": report.passed,
            "issues_count": len(report.issues),
        },
    )

    logger.info(
        f"{validator_name}_done",
        extra={
            "run_id": parsed.run_id,
            "gen_version": parsed.gen_version,
            "score": report.score,
            "passed": report.passed,
            "issues": len(report.issues),
        },
    )

    return ValidatorResult(
        run_id=parsed.run_id,
        status=ready_status,
        gen_version=parsed.gen_version,
        score=report.score,
        passed=report.passed,
        report_s3_uri=report_s3_uri,
    ).model_dump()
