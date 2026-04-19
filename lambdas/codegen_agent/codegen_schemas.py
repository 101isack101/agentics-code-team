"""Pydantic schemas for the CodeGen Agent.

Input event: CodeGenEvent (run_id + S3 pointer to TechnicalSpec).
LLM output:  CodeGenOutput (structured list of generated files).
Lambda output: CodeGenResult (only pointers to S3 + DynamoDB, never raw code).
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator


# Guard against path traversal and absolute paths in model-generated filenames.
_SAFE_PATH_RE = re.compile(r"^(?!/)(?!.*\.\.)[A-Za-z0-9._\-/]+$")


class GeneratedFile(BaseModel):
    path: str = Field(..., min_length=1, max_length=256)
    content: str

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        if not _SAFE_PATH_RE.match(value):
            raise ValueError(
                f"Unsafe file path: {value!r}. "
                "Must be relative, ASCII alphanumerics + . _ - /, no '..' segments."
            )
        return value


class CodeGenOutput(BaseModel):
    """What we expect Claude to return inside the JSON body (initial generation)."""

    language: str
    entry_point: str
    files: list[GeneratedFile] = Field(min_length=1, max_length=80)
    notes: str = ""


class CodeGenFixOutput(BaseModel):
    """Claude response for fix passes: only changed files, metadata optional.

    Unchanged files are merged from the prior manifest by the application layer
    to keep LLM output small and avoid token-limit truncation on large codebases.
    """

    language: str | None = None
    entry_point: str | None = None
    files: list[GeneratedFile] = Field(min_length=1, max_length=80)
    notes: str = ""


class FixIssue(BaseModel):
    """Subset of ValidatorIssue carried into a fix iteration prompt.

    We intentionally keep only fields useful to guide a rewrite (no IDs or
    metadata) to reduce prompt noise and token cost.
    """

    validator: str
    severity: str
    category: str
    file: str | None = None
    line: int | None = None
    description: str
    remediation: str


class CodeGenEvent(BaseModel):
    run_id: str
    spec_s3_uri: str
    iteration: int = Field(default=1, ge=1, le=10)
    # Optional fix-mode inputs. Populated by the Iterator Agent from iter >= 2.
    prior_manifest_s3_uri: str | None = None
    issues_to_fix: list[FixIssue] = Field(default_factory=list)


class ManifestFileEntry(BaseModel):
    path: str
    s3_uri: str
    size_bytes: int


class CodeGenManifest(BaseModel):
    """Stored at artifacts/<run_id>/v<n>/manifest.json.

    Downstream validators load this file to learn which files exist and
    where to fetch them from.
    """

    run_id: str
    gen_version: int = Field(ge=1)
    language: str
    entry_point: str
    files: list[ManifestFileEntry]
    notes: str = ""


class CodeGenResult(BaseModel):
    """Lambda output — pointers only."""

    run_id: str
    status: Literal["GEN_READY", "GEN_FAILED"]
    gen_version: int | None = None
    gen_manifest_s3_uri: str | None = None
    files_count: int | None = None
    error: str | None = None
