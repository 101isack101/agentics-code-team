"""Review Agent prompts.

Focus: code quality, readability, structure, naming, idiomatic usage,
and — critically — alignment with the TechnicalSpec's acceptance criteria.
"""

from __future__ import annotations

import json


SYSTEM_PROMPT = """You are the Review Agent of a multi-agent software factory.

Your job is to review the generated source code against the TechnicalSpec and
return a structured JSON report. You do NOT modify code. You do NOT rewrite.
You critique and score.

Checklist (apply rigorously):
- Does every functional requirement (FR-*) map to code? If missing, file an
  issue with severity `blocker`.
- Does every acceptance criterion (AC-*) have a test covering it? If not,
  severity `critical`.
- Naming, readability, SRP, dead code, magic numbers, docstrings, typing.
- Error handling completeness at system boundaries only; no over-engineering.
- File layout matches the declared complexity (simple vs enterprise).

HARD RULES:
- Output MUST be a single valid JSON object matching the provided schema.
  No prose outside the JSON. No markdown fences.
- Every issue MUST include `file` and (when meaningful) `line`.
- `score` is 0..100; be strict but fair. Perfect score requires zero issues.
- `pass` is true iff there are zero blocker/critical issues.
"""


USER_PROMPT_TEMPLATE = """TechnicalSpec:
```json
{spec_json}
```

Generated source tree ({file_count} files):
{files_section}

Produce a JSON object with EXACTLY this shape:

{{
  "validator": "review",
  "run_id": "<from spec>",
  "gen_version": <int>,
  "score": <0..100>,
  "pass": <bool>,
  "summary": "<2-3 sentences>",
  "issues": [
    {{
      "id": "ISSUE-001",
      "severity": "blocker|critical|major|minor|info",
      "category": "<e.g. missing_requirement, missing_test, naming, dead_code>",
      "file": "<relative path>",
      "line": <int or null>,
      "description": "<what is wrong>",
      "remediation": "<concrete fix>",
      "metadata": {{}}
    }}
  ]
}}

Return ONLY the JSON object.
"""


def _format_files(files: list[dict]) -> str:
    blocks = []
    for f in files:
        blocks.append(f"=== FILE: {f['path']} ===\n{f['content']}")
    return "\n\n".join(blocks) if blocks else "(no files)"


def build_user_prompt(spec: dict, files: list[dict]) -> str:
    return USER_PROMPT_TEMPLATE.format(
        spec_json=json.dumps(spec, ensure_ascii=False, indent=2),
        file_count=len(files),
        files_section=_format_files(files),
    )
