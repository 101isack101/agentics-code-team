"""Pydantic schemas for the TechnicalSpec v1.0 contract.

This schema is the output of the Spec Agent and the input of the CodeGen Agent.
Changing fields here = breaking change for downstream agents.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


ComplexityLevel = Literal["simple", "enterprise"]
HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]


class AcceptanceCriterion(BaseModel):
    id: str = Field(..., description="Stable ID like AC-001")
    description: str
    testable: bool = True


class ApiEndpoint(BaseModel):
    method: HttpMethod
    path: str
    summary: str
    request_schema: dict = Field(default_factory=dict)
    response_schema: dict = Field(default_factory=dict)
    auth_required: bool = True


class DataEntity(BaseModel):
    name: str
    fields: dict[str, str] = Field(
        default_factory=dict,
        description="Field name -> primitive type (e.g. 'email': 'string')",
    )
    relations: list[str] = Field(default_factory=list)


class TechnicalSpec(BaseModel):
    spec_version: Literal["1.0"] = "1.0"
    run_id: str
    project_name: str
    complexity: ComplexityLevel
    language: str
    summary: str
    user_stories: list[str] = Field(min_length=1)
    functional_requirements: list[str] = Field(min_length=1)
    non_functional_requirements: list[str] = Field(default_factory=list)
    acceptance_criteria: list[AcceptanceCriterion] = Field(min_length=1)
    api_contracts: list[ApiEndpoint] = Field(default_factory=list)
    data_entities: list[DataEntity] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)


class SpecAgentEvent(BaseModel):
    """Expected input event for the Spec Agent Lambda."""

    run_id: str
    project_name: str
    requirements: str = Field(min_length=10)
    language: str = "python"
    complexity: ComplexityLevel = "simple"


class SpecAgentResult(BaseModel):
    """Lambda output - just pointers, never raw payload (see ARCHITECTURE.md L141)."""

    run_id: str
    status: Literal["SPEC_READY", "SPEC_FAILED"]
    spec_s3_uri: str | None = None
    spec_version: int | None = None
    error: str | None = None
