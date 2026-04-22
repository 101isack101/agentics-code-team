"""CodeGen Agent Lambda handler.

Flow:
    1. Validate event (CodeGenEvent).
    2. Load TechnicalSpec from S3.
    3. Ask Claude for structured CodeGenOutput JSON.
    4. Upload each generated file to artifacts/<run_id>/v<iteration>/<path>.
    5. Upload manifest.json listing all files with their S3 URIs.
    6. Write DynamoDB item PK=run_id SK=GEN#v<iteration> (optimistic-locking v1).
    7. Return only pointers (manifest S3 URI, gen_version, files count).
"""

from __future__ import annotations

import json
from typing import Any, TypeVar

from pydantic import BaseModel

_T = TypeVar("_T", bound=BaseModel)

from aws_lambda_powertools import Logger, Tracer
from pydantic import ValidationError

from claude_client import ClaudeClient
from dynamo_repo import RunsRepository
from json_extractor import extract_json
from metrics import instrument_agent
from s3_repo import ArtifactsRepository

from codegen_prompts import SYSTEM_PROMPT, build_fix_messages, build_messages
from codegen_schemas import (
    CodeGenEvent,
    CodeGenFixOutput,
    CodeGenManifest,
    CodeGenOutput,
    CodeGenResult,
    ManifestFileEntry,
)


logger = Logger()
tracer = Tracer()


def _guess_content_type(path: str) -> str:
    if path.endswith(".json"):
        return "application/json; charset=utf-8"
    if path.endswith(".md"):
        return "text/markdown; charset=utf-8"
    if path.endswith((".yaml", ".yml")):
        return "application/x-yaml; charset=utf-8"
    return "text/plain; charset=utf-8"


def _persist_files(
    run_id: str,
    gen_version: int,
    output: CodeGenOutput | CodeGenFixOutput,
    artifacts_repo: ArtifactsRepository,
) -> list[ManifestFileEntry]:
    entries: list[ManifestFileEntry] = []
    for generated in output.files:
        key = artifacts_repo.artifact_key(run_id, gen_version, generated.path)
        uri = artifacts_repo.put_text(
            key,
            generated.content,
            content_type=_guess_content_type(generated.path),
        )
        entries.append(
            ManifestFileEntry(
                path=generated.path,
                s3_uri=uri,
                size_bytes=len(generated.content.encode("utf-8")),
            )
        )
    return entries


_JSON_PARSE_FIX_PROMPT = (
    "Your previous response contained invalid JSON (parse error: {error}). "
    "The most common cause is an unescaped character inside a 'content' string — "
    "a bare double-quote instead of \\\" or a bare backslash instead of \\\\. "
    "Regenerate the COMPLETE JSON object from scratch with all special characters "
    "correctly escaped. Return ONLY the JSON — no prose, no fences."
)
_JSON_SCHEMA_FIX_PROMPT = (
    "Your previous response was valid JSON but failed schema validation: {error}. "
    "The output MUST be a JSON object with EXACTLY these top-level fields: "
    "\"language\" (string), \"entry_point\" (string), \"files\" (array of {{path, content}}), "
    "\"notes\" (string). Regenerate the complete object with all required fields present. "
    "Return ONLY the JSON — no prose, no fences."
)
_MAX_JSON_RETRIES = 2
_CODEGEN_MAX_TOKENS = 16000


def _complete_with_json_retry(
    claude: ClaudeClient,
    messages: list[dict],
    run_id: str,
    schema_cls: type[_T] = CodeGenOutput,  # type: ignore[assignment]
) -> _T:
    raw = claude.complete(
        system=SYSTEM_PROMPT,
        messages=messages,
        max_tokens=_CODEGEN_MAX_TOKENS,
        agent_name="codegen_agent",
    )
    last_exc: Exception | None = None
    for attempt in range(_MAX_JSON_RETRIES + 1):
        try:
            return schema_cls.model_validate(extract_json(raw))  # type: ignore[return-value]
        except (ValidationError, json.JSONDecodeError) as exc:
            last_exc = exc
            if attempt < _MAX_JSON_RETRIES:
                logger.warning(
                    "codegen_json_retry",
                    extra={"run_id": run_id, "attempt": attempt + 1, "error": str(exc)},
                )
                template = (
                    _JSON_SCHEMA_FIX_PROMPT
                    if isinstance(exc, ValidationError)
                    else _JSON_PARSE_FIX_PROMPT
                )
                fix_messages = messages + [
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content": template.format(error=str(exc)[:200])},
                ]
                raw = claude.complete(
                    system=SYSTEM_PROMPT,
                    messages=fix_messages,
                    max_tokens=_CODEGEN_MAX_TOKENS,
                    agent_name="codegen_agent",
                )
    raise last_exc  # type: ignore[misc]


@logger.inject_lambda_context(log_event=True)
@tracer.capture_lambda_handler
@instrument_agent("codegen_agent")
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
        logger.exception("codegen_unhandled_error")
        return CodeGenResult(
            run_id=event.get("run_id", "unknown") if isinstance(event, dict) else "unknown",
            status="GEN_FAILED",
            error=f"unhandled_error: {type(exc).__name__}",
        ).model_dump()


def _run(
    event: dict[str, Any],
    claude: ClaudeClient | None,
    runs_repo: RunsRepository | None,
    artifacts_repo: ArtifactsRepository | None,
) -> dict[str, Any]:
    try:
        parsed = CodeGenEvent.model_validate(event)
    except ValidationError as exc:
        logger.exception("Invalid codegen event")
        return CodeGenResult(
            run_id=event.get("run_id", "unknown"),
            status="GEN_FAILED",
            error=f"invalid_event: {exc.errors()}",
        ).model_dump()

    claude = claude or ClaudeClient()
    runs_repo = runs_repo or RunsRepository()
    artifacts_repo = artifacts_repo or ArtifactsRepository()

    is_fix_pass = bool(parsed.prior_manifest_s3_uri and parsed.issues_to_fix)
    logger.info(
        "codegen_start",
        extra={
            "run_id": parsed.run_id,
            "iteration": parsed.iteration,
            "mode": "fix" if is_fix_pass else "initial",
            "issues_to_fix": len(parsed.issues_to_fix),
        },
    )

    _, spec_key = ArtifactsRepository.parse_s3_uri(parsed.spec_s3_uri)
    try:
        spec = artifacts_repo.get_json(spec_key)
        prior_manifest: dict | None = None
        if is_fix_pass:
            _, prior_key = ArtifactsRepository.parse_s3_uri(
                parsed.prior_manifest_s3_uri  # type: ignore[arg-type]
            )
            prior_manifest = artifacts_repo.get_json(prior_key)
    except Exception as exc:
        logger.exception("codegen_spec_load_failed")
        return CodeGenResult(
            run_id=parsed.run_id,
            status="GEN_FAILED",
            error=f"spec_load_failed: {exc}",
        ).model_dump()

    if is_fix_pass:
        messages = build_fix_messages(
            spec=spec,
            prior_manifest=prior_manifest or {},
            issues=[issue.model_dump() for issue in parsed.issues_to_fix],
        )
    else:
        messages = build_messages(spec)

    try:
        if is_fix_pass:
            fix_output = _complete_with_json_retry(
                claude, messages, parsed.run_id, schema_cls=CodeGenFixOutput
            )
        else:
            fix_output = None
            output = _complete_with_json_retry(claude, messages, parsed.run_id)
    except (ValidationError, json.JSONDecodeError) as exc:
        logger.exception("codegen_output_invalid")
        return CodeGenResult(
            run_id=parsed.run_id,
            status="GEN_FAILED",
            error=f"codegen_output_invalid: {exc}",
        ).model_dump()

    if is_fix_pass and fix_output is not None:
        # Persist only the changed files Claude returned.
        changed_entries = _persist_files(parsed.run_id, parsed.iteration, fix_output, artifacts_repo)
        changed_paths = {e.path for e in changed_entries}
        # Carry over unchanged files from the previous manifest (same S3 URI, no re-upload).
        prior_files = (prior_manifest or {}).get("files", [])
        unchanged_entries = [
            ManifestFileEntry(path=f["path"], s3_uri=f["s3_uri"], size_bytes=f["size_bytes"])
            for f in prior_files
            if f["path"] not in changed_paths
        ]
        entries = changed_entries + unchanged_entries
        language = fix_output.language or (prior_manifest or {}).get("language", "unknown")
        entry_point = fix_output.entry_point or (prior_manifest or {}).get("entry_point", "")
        notes = fix_output.notes
    else:
        entries = _persist_files(parsed.run_id, parsed.iteration, output, artifacts_repo)  # type: ignore[arg-type]
        language = output.language  # type: ignore[union-attr]
        entry_point = output.entry_point  # type: ignore[union-attr]
        notes = output.notes  # type: ignore[union-attr]

    manifest = CodeGenManifest(
        run_id=parsed.run_id,
        gen_version=parsed.iteration,
        language=language,
        entry_point=entry_point,
        files=entries,
        notes=notes,
    )
    manifest_key = artifacts_repo.manifest_key(parsed.run_id, parsed.iteration)
    manifest_s3_uri = artifacts_repo.put_json(manifest_key, manifest.model_dump())

    runs_repo.create_item(
        run_id=parsed.run_id,
        item_type=f"GEN#v{parsed.iteration}",
        status="GEN_READY",
        payload={
            "gen_manifest_s3_uri": manifest_s3_uri,
            "language": language,
            "entry_point": entry_point,
            "files_count": len(entries),
        },
    )

    logger.info(
        "codegen_done",
        extra={
            "run_id": parsed.run_id,
            "gen_version": parsed.iteration,
            "files_count": len(entries),
        },
    )

    return CodeGenResult(
        run_id=parsed.run_id,
        status="GEN_READY",
        gen_version=parsed.iteration,
        gen_manifest_s3_uri=manifest_s3_uri,
        files_count=len(entries),
    ).model_dump()
