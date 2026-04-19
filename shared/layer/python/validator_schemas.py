"""Shared schemas for the three parallel validators.

One report shape covers all three validators; the `validator` field identifies
which agent produced the report. Domain-specific data goes in `issues[*].category`
(free text) and `issues[*].metadata` (free-form dict) to stay KISS.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


Severity = Literal["blocker", "critical", "major", "minor", "info"]
ValidatorName = Literal["review", "security", "scalability"]


class ValidatorEvent(BaseModel):
    """Input event for review/security/scalability Lambdas."""

    run_id: str
    spec_s3_uri: str
    gen_manifest_s3_uri: str
    gen_version: int = Field(ge=1)
    iteration: int = Field(default=1, ge=1)


class ValidatorIssue(BaseModel):
    id: str = Field(..., description="Stable ID per report, e.g. 'ISSUE-001'")
    severity: Severity
    category: str = Field(..., description="Domain-specific label (e.g. 'owasp_a03', 'n_plus_1')")
    file: str | None = None
    line: int | None = None
    description: str
    remediation: str
    metadata: dict = Field(default_factory=dict)


class ValidatorReport(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    validator: ValidatorName
    run_id: str
    gen_version: int = Field(ge=1)
    score: int = Field(ge=0, le=100, description="Higher is better, 100 = flawless.")
    passed: bool = Field(alias="pass")
    summary: str
    issues: list[ValidatorIssue] = Field(default_factory=list)


class ValidatorResult(BaseModel):
    """Lambda output — only pointers, never the raw report."""

    run_id: str
    status: str
    gen_version: int | None = None
    score: int | None = None
    passed: bool | None = None
    report_s3_uri: str | None = None
    error: str | None = None
