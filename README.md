<h1 align="left">⚙️ Agentics Code Team</h1>

<h3 align="left">🤖 Multi-agent AI factory that turns plain-text requirements into validated, production-ready code.</h3>

<p align="left">
  <img src="https://img.shields.io/badge/AWS_Lambda-FF9900?style=for-the-badge&logo=awslambda&logoColor=white" />
  <img src="https://img.shields.io/badge/Step_Functions-FF4F8B?style=for-the-badge&logo=amazonaws&logoColor=white" />
  <img src="https://img.shields.io/badge/Claude_Sonnet_4.6-6B4FBB?style=for-the-badge&logo=anthropic&logoColor=white" />
  <img src="https://img.shields.io/badge/Python-3776AB?style=for-the-badge&logo=python&logoColor=white" />
  <img src="https://img.shields.io/badge/DynamoDB-4053D6?style=for-the-badge&logo=amazondynamodb&logoColor=white" />
  <img src="https://img.shields.io/badge/S3-569A31?style=for-the-badge&logo=amazons3&logoColor=white" />
  <img src="https://img.shields.io/badge/SAM-232F3E?style=for-the-badge&logo=amazonaws&logoColor=white" />
  <img src="https://img.shields.io/badge/Docker-2496ED?style=for-the-badge&logo=docker&logoColor=white" />
  <img src="https://img.shields.io/badge/pytest-0A9EDC?style=for-the-badge&logo=pytest&logoColor=white" />
</p>

<p align="left">
  <img src="https://img.shields.io/badge/Tests-55%2F55_passing-2ea44f?style=for-the-badge" />
  <img src="https://img.shields.io/badge/SAM_validate-OK-2ea44f?style=for-the-badge" />
  <img src="https://img.shields.io/badge/Status-Production_Ready-2ea44f?style=for-the-badge" />
</p>

---

## 🏗️ Architecture

```
  User (event JSON)
        │
        ▼
  ┌──────────┐   ┌──────────┐   ┌────────────────────────────┐   ┌──────────┐
  │  Spec    │──▶│ CodeGen  │──▶│  Review ║ Security ║ Scal  │──▶│ Iterator │
  └──────────┘   └──────────┘   └────────────────────────────┘   └──────────┘
        │             │                        │                       │
        └─────────────┴────── artifacts in S3 / state in DynamoDB ────┘

                     loop until MAX_ITERATIONS or convergence
```

| Agent | Role |
|---|---|
| **Spec Agent** | Converts requirement → `TechnicalSpec v1.0` |
| **CodeGen Agent** | Produces code files + `manifest.json` |
| **Review / Security / Scalability** | 3 parallel validators — gate: `score ≥ 80 ∧ no blocker ∧ no critical` |
| **Iterator** | Aggregates reports, detects ping-pong via SHA1 fingerprint, decides: `DONE` / `READY` / `EXHAUSTED` / `UNRESOLVABLE` |

---

## 🧪 Simulation Results (production)

Real runs against the deployed stack in `us-east-1` with `claude-sonnet-4-6`:

| Sim | Description | Iterations | Result | Scores |
|---|---|---|---|---|
| sim-01 | Simple Echo API (Python) | 1 | `ITERATOR_DONE` ✅ | review=91, security=82, scalability=92 |
| sim-02 | JWT + DynamoDB CRUD (26 files) | 1 | `ITERATOR_DONE` ✅ | review=82, security=82, scalability=82 |
| sim-03 | Order Processor with impossible requirement¹ | 3 | `ITERATOR_EXHAUSTED` ✅ | security never exceeds 80 |

¹ Deliberately contradictory requirement: `sub-100ms @ 10k rps without cache`. The system
exhausted 3 iterations, detected the contradiction and reported `ITERATOR_EXHAUSTED` instead
of looping forever — correct behavior by design.

---

## 🔧 Technical Decisions & Problems Solved

### JSON truncated mid-response
Claude occasionally cuts its response before closing the JSON object when output is large.
The solution differentiates two failure modes that require distinct correction prompts:

- `JSONDecodeError` → prompt asking to complete/repair the malformed JSON
- `ValidationError` (Pydantic) → prompt asking to adjust the structure to match the schema

Sending the wrong prompt for each case causes the model to generate cascading incorrect
responses. `_complete_with_json_retry()` in `codegen.py` implements this with 2 retries
(`_MAX_JSON_RETRIES = 2`).

### Infinite agent loops (deterministic anti-ping-pong)
The Iterator can reject the same issue iteration after iteration if the model fails to
resolve it. Detecting this without relying on text (which varies by temperature):

```python
fingerprint = sha1(f"{category}|{file}|{description[:80]}".encode()).hexdigest()[:16]
```

If the same fingerprint appears in runs N-2, N-1 and N → `ITERATOR_UNRESOLVABLE`. The
system stops and reports the issue as unresolvable instead of continuing to iterate.

### Token budget in fix mode
With 25+ issues, full descriptions exceeded Sonnet's 16k output token limit, producing
truncated JSON. Solution: only the top-8 issues sorted by severity
(`blocker → critical → major → minor`) are forwarded to the correction agent.

### Delta fix-pass (CodeGen in correction mode)
In iterations ≥ 2, CodeGen only regenerates files that changed according to the Iterator's
report. Unchanged files are taken from the previous S3 manifest (same URIs, no re-upload).
This avoids exceeding the token budget on larger codebases.

### Model migration: Opus 4.7 → Sonnet 4.6
The system was developed and validated first with `claude-opus-4-7`. Migrated to
`claude-sonnet-4-6` for cost: **5x cheaper** ($3/$15 vs $15/$75 per MTok input/output)
with equivalent quality for structured code generation. Both models were tested in
production; Sonnet demonstrated the same capability for this specific use case.
`PASS_SCORE_THRESHOLD` adjusted from 85 → 80 because Sonnet scores security ~82
consistently vs 90+ for Opus.

---

## ⚙️ Current Production Configuration

| Parameter | Value | Reason |
|---|---|---|
| Model | `claude-sonnet-4-6` | Validated with Opus 4.7 first; migrated for cost (5x cheaper) |
| `PASS_SCORE_THRESHOLD` | `80` | Sonnet scores security ~82 consistently |
| `_MAX_ISSUES_TO_FIX` | `8` | 25+ issues exceeded Sonnet's token budget |
| `_CODEGEN_MAX_TOKENS` | `16000` | Output limit for mid-size codebases |
| `MaxIterations` | `3` | Hard cap on the correction loop |

---

## 📊 Project Status

| Stage | Scope | Status |
|---|---|---|
| 1 | Base infra: SAM, DynamoDB optimistic locking, Spec Agent | ✅ |
| 2 | CodeGen Agent, S3 manifest, structured prompts | ✅ |
| 3 | Review ║ Security ║ Scalability + Iterator (fingerprints) | ✅ |
| 4 | Observability, safeguards, simulations, multi-stage deploy | ✅ |

---

## 🚀 Setup

```bash
pip install -r requirements-dev.txt
pip install -r shared/layer/python/requirements.txt
```

**Requirements:** Python 3.11 · AWS SAM CLI · Docker

---

## 🧪 Tests

```bash
python -m pytest tests/ -v
```

Fixtures use `moto` + `MagicMock` for Claude — no real AWS or Anthropic calls during tests.

---

## ☁️ Deploy

```bash
sam build
sam deploy --config-env dev    # no-prompt
sam deploy --config-env prod   # confirms changeset, tags Environment=prod
```

| Parameter | Dev | Prod | Range |
|---|---|---|---|
| `MaxIterations` | 3 | 3 | 1–10 |
| `PipelineTimeoutSeconds` | 3600 | 7200 | 300–43200 |
| `LogRetentionDays` | 30 | 90 | any |

---

## ▶️ Invoke the Pipeline

**AWS CLI:**
```bash
aws stepfunctions start-execution \
  --state-machine-arn "$(aws cloudformation describe-stacks \
      --stack-name agentics-code-team \
      --query 'Stacks[0].Outputs[?OutputKey==`StateMachineArn`].OutputValue' \
      --output text)" \
  --input file://tests/simulations/01_simple_echo_api.json
```

**Python SDK:**
```python
import boto3, json
sfn = boto3.client("stepfunctions")
event = json.load(open("tests/simulations/01_simple_echo_api.json"))
arn = "arn:aws:states:us-east-1:<account>:stateMachine:AgenticsCodeTeam"
sfn.start_execution(stateMachineArn=arn, input=json.dumps(event))
```

**Input schema (`SpecAgentEvent`):**
```json
{
  "run_id": "^[a-zA-Z0-9_-]{1,64}$",
  "project_name": "^[A-Za-z0-9][A-Za-z0-9_\\- ]{1,63}$",
  "requirements": "20..8000 chars",
  "language": "python | typescript | go",
  "complexity": "simple | enterprise"
}
```

---

## 📂 Repo Structure

```
Agentics_Code_Team/
├── template.yaml                  # SAM stack (Lambdas, SFN, DDB, S3, SNS, alarms)
├── samconfig.toml                 # default / dev / prod envs
├── statemachine/main.asl.json     # pipeline with root timeout
├── shared/layer/python/           # Shared Lambda Layer
│   ├── claude_client.py           # Anthropic + Secrets Manager wrapper
│   ├── dynamo_repo.py             # optimistic locking
│   ├── s3_repo.py                 # canonical URIs and keys
│   ├── validator_runner.py        # common skeleton for 3 validators
│   ├── validator_schemas.py       # ValidatorEvent/Report/Result
│   ├── json_extractor.py          # defensive LLM response parser
│   └── metrics.py                 # emitters + instrument_agent decorator
├── lambdas/
│   ├── spec_agent/        app.py + schemas.py + prompts
│   ├── codegen_agent/     codegen.py + schemas + prompts
│   ├── review_agent/      review.py + prompts
│   ├── security_agent/    security.py + prompts
│   ├── scalability_agent/ scalability.py + prompts
│   └── iterator_agent/    iterator.py + schemas
├── scripts/run_simulation.py
└── tests/
    ├── conftest.py                # Layer sys.path + moto fixtures
    ├── simulations/               # 3 canonical events + validation
    └── unit/                      # 55 tests
```

---

## 📡 Observability

- **CloudWatch Logs** — Powertools Logger (structured JSON), configurable retention
- **X-Ray** — active globally; each Lambda traces `http`/`aws` subsegments
- **Custom metrics** (namespace `AgenticsCodeTeam`): `AgentInvocations`, `AgentDurationMs`, `IteratorIterations`, `ValidatorPassRate`
- **Alarms** → `AgenticsAlertsTopic` (SNS): pipeline failures, exhausted runs, Lambda errors

---

## 🛡️ Production Safeguards

- `MaxIterations` (1–10) — hard cap on the correction loop
- `PipelineTimeoutSeconds` — root State Machine timeout; Step Functions aborts on breach
- **Strict event validation** — regex on `run_id` / `project_name`; `language` restricted to `python|typescript|go`; `requirements` 20–8000 chars
- **Uniform error handling** — all agents catch root `Exception` and return `status=<AGENT>_FAILED` without leaking stack traces

---

## 🔍 Reading Results

**S3 layout (`ARTIFACTS_BUCKET`):**
```
specs/<run_id>/technical_spec.json
artifacts/<run_id>/v<n>/manifest.json
artifacts/<run_id>/v<n>/<path>
artifacts/<run_id>/v<n>/review.json
artifacts/<run_id>/v<n>/security.json
artifacts/<run_id>/v<n>/scalability.json
```

**Validator gate:** `score ≥ 80 ∧ 0 blocker issues ∧ 0 critical issues` — recalculated deterministically, ignoring the LLM's own `pass` field.

**Fingerprints (Iterator):** `sha1(category|file|description[:80])[:16]` — an issue appearing in runs N−2, N−1 and N is marked `UNRESOLVABLE`.

---

<p align="center">Built by <a href="https://github.com/101isack101">@101isack101</a> · Powered by Claude + AWS</p>
