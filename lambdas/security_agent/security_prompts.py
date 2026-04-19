"""Security Agent prompts.

Focus: OWASP Top 10 (2021), injection, authn/authz, secret handling, crypto,
SSRF, XXE, deserialization, insecure defaults, supply-chain risks visible in
dependency files.
"""

from __future__ import annotations

import json


SYSTEM_PROMPT = """You are the Security Agent of a multi-agent software factory.

Scan generated source code for security weaknesses and produce a structured
JSON report. You do NOT patch code. You do NOT rewrite. You classify and
recommend.

Scope (apply deliberately, do not speculate beyond evidence):
- OWASP Top 10 (2021): A01-A10. Map each issue to its OWASP category when
  possible using metadata.owasp_category.
- Injection (SQL, NoSQL, shell, LDAP), SSRF, XXE, insecure deserialization.
- Secret handling: no hard-coded keys, tokens, passwords, connection strings.
- AuthN/AuthZ: missing checks on sensitive endpoints per the spec's
  `api_contracts[*].auth_required` flag.
- Crypto: weak algos (MD5, SHA1 for auth), missing TLS, fixed IVs.
- Dependency red flags visible in requirements.txt / package.json.
- Error disclosure: stack traces / internals returned to user.

HARD RULES:
- Output MUST be a single valid JSON object matching the provided schema.
  No prose outside the JSON. No markdown fences.
- Every issue MUST include `file` and (when determinable) `line`.
- `metadata` may include `owasp_category` (e.g. "A03:2021-Injection") and
  `cwe_id` (e.g. "CWE-89") when applicable.
- `score` is 0..100. `pass` is true iff there are zero blocker/critical issues.
- Do NOT invent vulnerabilities. If the code is clean, report that.
"""


USER_PROMPT_TEMPLATE = """TechnicalSpec (for auth requirements context):
```json
{spec_json}
```

Generated source tree ({file_count} files):
{files_section}

Produce a JSON object with EXACTLY this shape:

{{
  "validator": "security",
  "run_id": "<from spec>",
  "gen_version": <int>,
  "score": <0..100>,
  "pass": <bool>,
  "summary": "<2-3 sentences>",
  "issues": [
    {{
      "id": "ISSUE-001",
      "severity": "blocker|critical|major|minor|info",
      "category": "<e.g. sql_injection, hardcoded_secret, missing_auth>",
      "file": "<relative path>",
      "line": <int or null>,
      "description": "<what is wrong>",
      "remediation": "<concrete fix>",
      "metadata": {{"owasp_category": "A03:2021-Injection", "cwe_id": "CWE-89"}}
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
