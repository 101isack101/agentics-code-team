"""Pydantic schemas for the Iterator Agent.

The Iterator is a pure-logic Lambda (no LLM call). It aggregates the 3
validator reports of iteration N, applies a deterministic ping-pong guard,
and emits the inputs CodeGen needs to produce iteration N+1.
"""

from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, Field


def _resolve_max_iterations() -> int:
    try:
        raw = int(os.getenv("MAX_ITERATIONS", "3"))
    except ValueError:
        return 3
    return max(1, min(10, raw))


# Absolute cap on corrective iterations; matches the Step Functions guard.
MAX_ITERATIONS: int = _resolve_max_iterations()


IteratorStatus = Literal[
    "ITERATOR_READY",       # next iteration approved, CodeGen can run
    "ITERATOR_DONE",        # all 3 validators passed — pipeline converged
    "ITERATOR_EXHAUSTED",   # MAX_ITERATIONS reached without convergence
    "ITERATOR_UNRESOLVABLE",  # same issue persisted across 3 iterations
    "ITERATOR_FAILED",      # input / state error
]


class IteratorEvent(BaseModel):
    """Input event for the Iterator Agent.

    Carries pointers to the 3 validator results from the current iteration
    plus the spec and the manifest produced by CodeGen in this iteration.
    """

    run_id: str
    spec_s3_uri: str
    gen_manifest_s3_uri: str
    gen_version: int = Field(ge=1)
    review_result: dict
    security_result: dict
    scalability_result: dict


class IssueFingerprint(BaseModel):
    """Deterministic identity of a validator issue across iterations.

    Hash key = (category, file or "", description[:80]).
    Used to detect ping-pong: the same fingerprint reappearing in consecutive
    iterations means the last fix did not resolve it.
    """

    validator: str
    category: str
    file: str | None = None
    description_head: str  # description[:80]
    severity: str
    fingerprint: str  # sha1(category|file|description_head) hex digest, 16 chars


class IteratorResult(BaseModel):
    """Lambda output — pointers + decision only, no raw reports."""

    run_id: str
    status: IteratorStatus
    gen_version: int = Field(ge=1)
    iteration: int = Field(ge=1)

    # All-passed convergence signal (short-circuits the state machine).
    all_passed: bool = False

    # Populated when status == ITERATOR_READY:
    next_iteration: int | None = None
    prior_manifest_s3_uri: str | None = None
    issues_to_fix: list[dict] = Field(default_factory=list)

    # Diagnostics — persisted in DynamoDB for auditing.
    persistent_fingerprints: list[str] = Field(default_factory=list)
    new_fingerprints: list[str] = Field(default_factory=list)

    error: str | None = None
