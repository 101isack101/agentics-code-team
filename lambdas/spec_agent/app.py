"""Spec Agent Lambda handler.

Input event (see schemas.SpecAgentEvent):
    {
      "run_id": "...",
      "project_name": "...",
      "requirements": "raw user requirement text",
      "language": "python",
      "complexity": "simple" | "enterprise"
    }

Output (see schemas.SpecAgentResult) - only pointers, never raw payload:
    {
      "run_id": "...",
      "status": "SPEC_READY" | "SPEC_FAILED",
      "spec_s3_uri": "s3://bucket/specs/<run_id>/technical_spec.json",
      "spec_version": 1
    }
"""

from __future__ import annotations

import json
from typing import Any

from aws_lambda_powertools import Logger, Tracer
from pydantic import ValidationError

from claude_client import ClaudeClient
from dynamo_repo import RunsRepository
from json_extractor import extract_json
from metrics import instrument_agent
from s3_repo import ArtifactsRepository

from schemas import (
    SpecAgentEvent,
    SpecAgentResult,
    TechnicalSpec,
)
from spec_kit_prompts import SYSTEM_PROMPT, build_messages


logger = Logger()
tracer = Tracer()


def _build_spec(
    event: SpecAgentEvent,
    claude: ClaudeClient,
) -> TechnicalSpec:
    messages = build_messages(
        run_id=event.run_id,
        project_name=event.project_name,
        requirements=event.requirements,
        language=event.language,
        complexity=event.complexity,
    )
    raw = claude.complete(system=SYSTEM_PROMPT, messages=messages)
    data = extract_json(raw)
    data.setdefault("run_id", event.run_id)
    data.setdefault("project_name", event.project_name)
    data.setdefault("language", event.language)
    data.setdefault("complexity", event.complexity)
    return TechnicalSpec.model_validate(data)


@logger.inject_lambda_context(log_event=True)
@tracer.capture_lambda_handler
@instrument_agent("spec_agent")
def lambda_handler(
    event: dict[str, Any],
    context: Any,
    claude: ClaudeClient | None = None,
    runs_repo: RunsRepository | None = None,
    artifacts_repo: ArtifactsRepository | None = None,
) -> dict[str, Any]:
    try:
        return _run(event, claude, runs_repo, artifacts_repo)
    except Exception as exc:
        logger.exception("spec_agent_unhandled_error")
        return SpecAgentResult(
            run_id=event.get("run_id", "unknown") if isinstance(event, dict) else "unknown",
            status="SPEC_FAILED",
            error=f"unhandled_error: {type(exc).__name__}",
        ).model_dump()


def _run(
    event: dict[str, Any],
    claude: ClaudeClient | None,
    runs_repo: RunsRepository | None,
    artifacts_repo: ArtifactsRepository | None,
) -> dict[str, Any]:
    try:
        parsed_event = SpecAgentEvent.model_validate(event)
    except ValidationError as exc:
        logger.exception("Invalid event payload")
        return SpecAgentResult(
            run_id=event.get("run_id", "unknown"),
            status="SPEC_FAILED",
            error=f"invalid_event: {exc.errors()}",
        ).model_dump()

    claude = claude or ClaudeClient()
    runs_repo = runs_repo or RunsRepository()
    artifacts_repo = artifacts_repo or ArtifactsRepository()

    logger.info("spec_generation_start", extra={"run_id": parsed_event.run_id})

    try:
        spec = _build_spec(parsed_event, claude)
    except (ValidationError, json.JSONDecodeError) as exc:
        logger.exception("spec_validation_failed")
        return SpecAgentResult(
            run_id=parsed_event.run_id,
            status="SPEC_FAILED",
            error=f"spec_validation_failed: {exc}",
        ).model_dump()

    spec_key = artifacts_repo.spec_key(parsed_event.run_id)
    spec_s3_uri = artifacts_repo.put_json(spec_key, spec.model_dump())

    runs_repo.create_item(
        run_id=parsed_event.run_id,
        item_type="SPEC",
        status="SPEC_READY",
        payload={
            "spec_s3_uri": spec_s3_uri,
            "project_name": parsed_event.project_name,
            "complexity": parsed_event.complexity,
            "language": parsed_event.language,
        },
    )

    logger.info(
        "spec_generation_done",
        extra={"run_id": parsed_event.run_id, "spec_s3_uri": spec_s3_uri},
    )

    return SpecAgentResult(
        run_id=parsed_event.run_id,
        status="SPEC_READY",
        spec_s3_uri=spec_s3_uri,
        spec_version=1,
    ).model_dump()
