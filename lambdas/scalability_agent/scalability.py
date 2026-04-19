"""Scalability Agent Lambda handler — thin wrapper over validator_runner."""

from __future__ import annotations

from typing import Any

from aws_lambda_powertools import Logger, Tracer

from claude_client import ClaudeClient
from dynamo_repo import RunsRepository
from metrics import instrument_agent
from s3_repo import ArtifactsRepository
from validator_runner import run_validator

from scalability_prompts import SYSTEM_PROMPT, build_user_prompt


logger = Logger()
tracer = Tracer()


@logger.inject_lambda_context(log_event=True)
@tracer.capture_lambda_handler
@instrument_agent("scalability_agent")
def lambda_handler(
    event: dict[str, Any],
    context: Any,
    claude: ClaudeClient | None = None,
    runs_repo: RunsRepository | None = None,
    artifacts_repo: ArtifactsRepository | None = None,
) -> dict[str, Any]:
    try:
        return run_validator(
            event=event,
            validator_name="scalability",
            item_type_prefix="SCALABILITY",
            ready_status="SCALABILITY_READY",
            failed_status="SCALABILITY_FAILED",
            report_filename="scalability.json",
            system_prompt=SYSTEM_PROMPT,
            build_user_prompt=build_user_prompt,
            logger=logger,
            claude=claude,
            runs_repo=runs_repo,
            artifacts_repo=artifacts_repo,
        )
    except Exception as exc:
        logger.exception("scalability_unhandled_error")
        return {
            "run_id": event.get("run_id", "unknown") if isinstance(event, dict) else "unknown",
            "status": "SCALABILITY_FAILED",
            "error": f"unhandled_error: {type(exc).__name__}",
        }
