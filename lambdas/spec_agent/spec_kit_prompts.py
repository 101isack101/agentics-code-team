"""Spec-kit methodology prompts, ported to run headless inside Lambda.

Spec-kit's CLI (`specify`) is interactive and not designed for serverless.
We replicate its three foundational phases as a single chained prompt:

    constitution -> specify -> plan

The output is a single TechnicalSpec JSON conforming to schemas.TechnicalSpec.
"""

from __future__ import annotations


SYSTEM_PROMPT = """You are the Spec Agent of a multi-agent software factory.

Your sole responsibility is to transform a raw user requirement into a rigorous,
testable, machine-readable Technical Specification (TechnicalSpec v1.0) that
downstream agents (CodeGen, Review, Security, Scalability) can consume without
ambiguity.

You follow the Spec-kit methodology distilled into three mental phases:

1. CONSTITUTION - Establish non-negotiable principles for this project:
   stack choice, architectural style, quality bars, out-of-scope items.
2. SPECIFY - Produce the WHAT: user stories, functional requirements,
   non-functional requirements, acceptance criteria. No implementation details.
3. PLAN - Translate the WHAT into concrete contracts: API endpoints,
   data entities, constraints. Still no code, but now concrete enough that
   two engineers would build the same system.

HARD RULES:
- Output MUST be a single valid JSON object conforming to the provided schema.
- No prose outside the JSON. No markdown fences. No explanations.
- Every acceptance criterion MUST be testable (observable output given input).
- Every functional requirement MUST map to at least one acceptance criterion.
- If the user's requirement is ambiguous, make the most conservative assumption
  and record it under `constraints` with prefix "ASSUMPTION:".
- Prefer fewer, stronger requirements over many weak ones.
- `complexity` = "simple" -> lean Serverless layout; "enterprise" -> DDD layout.
"""


USER_PROMPT_TEMPLATE = """Project: {project_name}
Language: {language}
Complexity: {complexity}
Run ID: {run_id}

Raw user requirement:
---
{requirements}
---

Produce a TechnicalSpec v1.0 JSON object with EXACTLY these top-level fields:

{{
  "spec_version": "1.0",
  "run_id": "{run_id}",
  "project_name": "{project_name}",
  "complexity": "{complexity}",
  "language": "{language}",
  "summary": "<2-3 sentence elevator pitch of what will be built>",
  "user_stories": ["As a <role> I want <goal> so that <benefit>", ...],
  "functional_requirements": ["FR-001: <requirement>", ...],
  "non_functional_requirements": ["NFR-001: <requirement>", ...],
  "acceptance_criteria": [
    {{"id": "AC-001", "description": "<given/when/then>", "testable": true}},
    ...
  ],
  "api_contracts": [
    {{
      "method": "POST",
      "path": "/resource",
      "summary": "<what it does>",
      "request_schema": {{"field": "type", ...}},
      "response_schema": {{"field": "type", ...}},
      "auth_required": true
    }}
  ],
  "data_entities": [
    {{
      "name": "User",
      "fields": {{"id": "uuid", "email": "string"}},
      "relations": ["has_many:Orders"]
    }}
  ],
  "constraints": ["ASSUMPTION: ...", "MUST: ...", "MUST_NOT: ..."],
  "out_of_scope": ["<thing we explicitly will NOT build>", ...]
}}

Return ONLY the JSON object. Nothing else.
"""


def build_messages(
    run_id: str,
    project_name: str,
    requirements: str,
    language: str,
    complexity: str,
) -> list[dict]:
    return [
        {
            "role": "user",
            "content": USER_PROMPT_TEMPLATE.format(
                run_id=run_id,
                project_name=project_name,
                requirements=requirements,
                language=language,
                complexity=complexity,
            ),
        }
    ]
