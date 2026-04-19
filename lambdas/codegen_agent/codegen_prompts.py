"""CodeGen Agent prompts.

Two modes:

- `build_messages(spec)` — iter v1, from scratch.
- `build_fix_messages(spec, prior_manifest, prior_files, issues)` — iter v>=2.
  The Iterator Agent passes in the previous manifest and aggregated issues so
  CodeGen can regenerate a full tree that fixes them while preserving the
  files that were already fine.
"""

from __future__ import annotations

import json


SYSTEM_PROMPT = """You are the CodeGen Agent of a multi-agent software factory.

Your sole responsibility is to transform a rigorous TechnicalSpec (v1.0) into
a complete, runnable source tree. Every functional requirement and acceptance
criterion in the spec MUST be reflected in the generated code.

HARD RULES:
- Output MUST be a single valid JSON object. No prose outside the JSON.
  No markdown fences. No commentary.
- `files` is a list of {path, content} objects. Paths are relative, use '/'
  as separator, and must not contain '..' or leading '/'.
- CRITICAL — JSON string escaping: every `content` value is a JSON string.
  You MUST escape all special characters: `\"` for double-quotes, `\\` for
  backslashes, `\n` for newlines, `\t` for tabs. Any unescaped character
  inside a JSON string will fail json.loads() and abort the entire pipeline.
- Include ALL files required to run the project: source, `requirements.txt`
  or `package.json` as appropriate, README, and at least one test file that
  covers the acceptance criteria in the spec.
- Prefer small, well-named files over one giant file. No file larger than
  200 lines unless unavoidable.
- If the spec's `complexity` is "simple" use a lean serverless-friendly
  layout. If "enterprise", use a DDD-style layout
  (domain / application / infrastructure).
- Do NOT embed secrets, hard-coded credentials, or placeholder API keys.
- Do NOT invent fields beyond the spec; if ambiguity remains, encode the
  conservative choice and note it in `notes`.
"""


USER_PROMPT_TEMPLATE = """TechnicalSpec (authoritative):
```json
{spec_json}
```

Produce a JSON object with EXACTLY these fields:

{{
  "language": "<language from the spec>",
  "entry_point": "<path to the file a dev would run first>",
  "files": [
    {{"path": "app.py", "content": "<full file contents>"}},
    {{"path": "requirements.txt", "content": "<pinned deps>"}},
    {{"path": "tests/test_acceptance.py", "content": "<tests covering AC-*>"}}
  ],
  "notes": "<short rationale or caveats, plain text>"
}}

Return ONLY the JSON object. Nothing else.
"""


FIX_USER_PROMPT_TEMPLATE = """You previously generated a source tree from the TechnicalSpec below.
Validators (review / security / scalability) found the issues listed after it.
Produce a NEW full source tree that FIXES every issue while preserving code
that was already correct.

TechnicalSpec (authoritative — unchanged):
```json
{spec_json}
```

Previous manifest (files you generated last iteration):
```json
{prior_manifest_json}
```

Issues reported by validators (MUST be resolved):
```json
{issues_json}
```

HARD RULES FOR THIS FIX PASS:
- Output ONLY the files that changed. Do NOT re-emit files that are already
  correct — they will be preserved automatically from the previous iteration.
  Unchanged files do NOT need to appear in `files`.
- JSON shape is identical to iteration 1, but `language` and `entry_point`
  are optional if they did not change.
- Every issue in the list above MUST be addressed. For each issue, either
  the offending code is gone or the fix is structurally evident. Add or
  modify tests as needed.
- In `notes`, briefly state WHICH files you changed and WHY (one line each).

Return ONLY the JSON object. Nothing else.
"""


def build_messages(spec: dict) -> list[dict]:
    return [
        {
            "role": "user",
            "content": USER_PROMPT_TEMPLATE.format(
                spec_json=json.dumps(spec, ensure_ascii=False, indent=2),
            ),
        }
    ]


def build_fix_messages(
    spec: dict,
    prior_manifest: dict,
    issues: list[dict],
) -> list[dict]:
    return [
        {
            "role": "user",
            "content": FIX_USER_PROMPT_TEMPLATE.format(
                spec_json=json.dumps(spec, ensure_ascii=False, indent=2),
                prior_manifest_json=json.dumps(
                    prior_manifest, ensure_ascii=False, indent=2
                ),
                issues_json=json.dumps(issues, ensure_ascii=False, indent=2),
            ),
        }
    ]
