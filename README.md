# Agentics Code Team

Fábrica multi-agente en AWS Lambda + Step Functions + Claude Opus 4.7 que
transforma un requerimiento en texto libre en un paquete de código
validado por tres jueces independientes y corregido iterativamente hasta
convergencia o rechazo determinista.

## Estado del proyecto

| Etapa | Alcance | Estado |
|---|---|---|
| 1 | Infra base: SAM, DynamoDB optimistic locking, Spec Agent | ✅ |
| 2 | CodeGen Agent, manifest S3, prompts estructurados | ✅ |
| 3 | Review ║ Security ║ Scalability + Iterator (fingerprints) | ✅ |
| 4 | Observabilidad, safeguards, simulaciones, multi-stage deploy | ✅ |

Suite actual: **55/55 tests verdes**, `sam validate --lint` OK.

## Arquitectura en 30s

```
  User (event JSON)
        │
        ▼
  ┌──────────┐   ┌──────────┐   ┌────────────────────────────┐   ┌──────────┐
  │  Spec    │──▶│ CodeGen  │──▶│  Review ║ Security ║ Scal  │──▶│ Iterator │
  └──────────┘   └──────────┘   └────────────────────────────┘   └──────────┘
        │             │                        │                       │
        └─────────────┴────── artefactos en S3 / estado en DynamoDB ───┘

                     loop hasta MAX_ITERATIONS o convergencia
```

- **Spec Agent** — convierte el requerimiento en un `TechnicalSpec v1.0`.
- **CodeGen Agent** — produce archivos de código + `manifest.json`.
- **3 Validadores paralelos** — emiten un `ValidatorReport` cada uno con
  gate determinista `score ≥ 85 ∧ sin blocker ∧ sin critical`.
- **Iterator** — agrega los 3 reportes, detecta ping-pong por huella SHA1
  `(category|file|description[:80])` y decide: `DONE`, `READY` (otra
  iteración), `EXHAUSTED` o `UNRESOLVABLE`.

## Setup local

Requisitos: Python 3.11, AWS SAM CLI, Docker (para `sam local`).

```bash
pip install -r requirements-dev.txt
pip install -r shared/layer/python/requirements.txt
```

## Tests

```bash
python -m pytest tests/ -v
```

Fixtures usan `moto` + `MagicMock` para Claude; no se llama a AWS ni
Anthropic reales durante los tests.

## Deploy

Dos entornos pre-configurados en `samconfig.toml`:

```bash
sam build
sam deploy --config-env dev          # no-prompt, confirm_changeset=false
sam deploy --config-env prod         # confirm_changeset=true, tags Environment=prod
```

Parámetros clave (sobrescribibles con `--parameter-overrides`):

| Parámetro | Default dev | Default prod | Rango |
|---|---|---|---|
| `MaxIterations` | 3 | 3 | 1–10 |
| `PipelineTimeoutSeconds` | 3600 | 7200 | 300–43200 |
| `LogRetentionDays` | 30 | 90 | cualquiera |

Post-deploy: poblar `AnthropicApiKeySecret` con la API key real y, si se
desea, suscribir email o Slack al topic SNS `AgenticsAlertsTopic`.

## Cómo invocar la Fábrica

Los outputs del stack incluyen `StateMachineArn` y
`AgenticsAlertsTopicArn`.

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

**Script incluido** (local o AWS):

```bash
python scripts/run_simulation.py --sim 01 --mode local
python scripts/run_simulation.py --sim 02 --mode aws --stage dev
python scripts/run_simulation.py --sim 03 --mode local --dry-run
```

El evento debe cumplir `SpecAgentEvent`:

```json
{
  "run_id": "^[a-zA-Z0-9_-]{1,64}$",
  "project_name": "^[A-Za-z0-9][A-Za-z0-9_\\- ]{1,63}$",
  "requirements": "20..8000 chars",
  "language": "python | typescript | go",
  "complexity": "simple | enterprise"
}
```

## Cómo leer resultados

**Layout en S3** (`ARTIFACTS_BUCKET`):

```
specs/<run_id>/technical_spec.json
artifacts/<run_id>/v<n>/manifest.json
artifacts/<run_id>/v<n>/<path>        # archivos generados
artifacts/<run_id>/v<n>/review.json
artifacts/<run_id>/v<n>/security.json
artifacts/<run_id>/v<n>/scalability.json
```

**Queries DynamoDB** (`RUNS_TABLE`):

```python
import boto3
ddb = boto3.resource("dynamodb").Table("agentics-runs")

# Toda la historia de un run
ddb.query(KeyConditionExpression="run_id = :r",
          ExpressionAttributeValues={":r": "run-abc"})

# Runs en un estado específico vía GSI
ddb.query(IndexName="GSI1-Status-UpdatedAt",
          KeyConditionExpression="#s = :s",
          ExpressionAttributeNames={"#s": "status"},
          ExpressionAttributeValues={":s": "ITERATOR_DONE"})
```

## Cómo interpretar reportes de validators

Un reporte tiene la forma:

```json
{
  "validator": "security",
  "score": 72,
  "pass": false,
  "issues": [
    {
      "id": "SEC-001",
      "severity": "blocker | critical | major | minor | info",
      "category": "owasp_a03",
      "file": "app.py",
      "line": 12,
      "description": "...",
      "remediation": "..."
    }
  ]
}
```

**Gate determinista** (`validator_runner.calculate_passed`): se descarta
el campo `pass` del LLM y se recalcula como
`score ≥ 85 ∧ 0 issues blocker ∧ 0 issues critical`. Esto evita
autocomplacencia del modelo.

**Fingerprints** (Iterator): `sha1(category|file|description[:80])[:16]`.
Un fingerprint que aparece en N−2, N−1 y N marca el issue como
`UNRESOLVABLE` — dos iteraciones no bastaron para resolverlo.

## Observabilidad

- **CloudWatch Logs** — Powertools Logger estructurado en JSON por
  Lambda; retención configurable (`LogRetentionDays`).
- **X-Ray** — activo globalmente; cada Lambda traza sus `http` / `aws`
  subsegmentos.
- **Métricas custom** (namespace `AgenticsCodeTeam`):
  - `AgentInvocations` (Count) — dims: `agent`, `stage`
  - `AgentDurationMs` (Milliseconds) — dims: `agent`, `stage`
  - `IteratorIterations` (Count) — dims: `outcome ∈ done|ready|exhausted|unresolvable`, `stage`
  - `ValidatorPassRate` (0.0 / 1.0) — dims: `validator`, `stage`
- **Alarmas** (publican a `AgenticsAlertsTopic`):
  - `AlarmPipelineFailures` — `AWS/States ExecutionsFailed ≥ 1` en 5 min
  - `AlarmPipelineExhausted` — `IteratorIterations outcome=exhausted ≥ 1` en 15 min
  - `AlarmSpecAgentErrors` / `AlarmCodeGenErrors` — `AWS/Lambda Errors ≥ 1` en 5 min

Sin suscripciones por defecto: post-deploy, suscribe email/Slack al
topic con `aws sns subscribe`.

## Safeguards de producción

- `MaxIterations` (1–10, default 3) — tope duro al loop corrector,
  propagado como env var y leído dinámicamente por el Iterator.
- `PipelineTimeoutSeconds` (300–43200, default 3600/7200) — timeout
  raíz del State Machine; si se alcanza, Step Functions aborta.
- **Validación estricta del evento Spec** — regex en `run_id` y
  `project_name` (bloquea inyección en paths S3 y PKs DynamoDB);
  `language` restringido a `python|typescript|go`; `requirements`
  limitado a 20–8000 chars.
- **Manejo uniforme de errores** — todo agente atrapa `Exception`
  raíz y retorna `status=<AGENT>_FAILED` con
  `error=unhandled_error: <TypeName>` (sin leakear stack traces al
  payload).

## Estructura del repo

```
Agentics_Code_Team/
├── template.yaml                  # SAM stack (Lambdas, SFN, DDB, S3, SNS, alarms)
├── samconfig.toml                 # default / dev / prod envs
├── statemachine/main.asl.json     # pipeline con timeout root
├── shared/layer/python/           # Shared Lambda Layer
│   ├── claude_client.py           # wrapper Anthropic + Secrets Manager
│   ├── dynamo_repo.py             # optimistic locking
│   ├── s3_repo.py                 # URIs, claves canónicas
│   ├── validator_runner.py        # skeleton común de los 3 validadores
│   ├── validator_schemas.py       # ValidatorEvent/Report/Result
│   ├── json_extractor.py          # parser defensivo de respuestas LLM
│   └── metrics.py                 # emisores + decorator instrument_agent
├── lambdas/
│   ├── spec_agent/        app.py + schemas.py + prompts
│   ├── codegen_agent/     codegen.py + schemas + prompts
│   ├── review_agent/      review.py + prompts
│   ├── security_agent/    security.py + prompts
│   ├── scalability_agent/ scalability.py + prompts
│   └── iterator_agent/    iterator.py + schemas
├── scripts/run_simulation.py      # runner sam-local / AWS SFN
└── tests/
    ├── conftest.py                # sys.path del Layer + fixtures moto
    ├── simulations/               # 3 eventos canónicos + validación
    └── unit/                      # 55 tests agregados
```

## Out of scope (explícito)

- `sam deploy` real — bloqueado hasta aprobación.
- Población de `AnthropicApiKeySecret` — paso manual post-deploy.
- Suscriptores del topic SNS — decisión de operaciones.
- Migración a Bedrock — decisión de arquitectura pospuesta.
