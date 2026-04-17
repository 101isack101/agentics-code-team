# Agentics Code Team — Multi-Agent Code System

Sistema de generación automática de software usando 7 agentes especializados
orquestados por AWS Step Functions, con Claude 4.7 Opus como LLM backbone.

Ver [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) para el plan completo.

## Estado

**Etapa 1 — Especificación e Infraestructura** (en curso)

- [x] Monorepo + IaC (AWS SAM)
- [x] S3 bucket + DynamoDB con Optimistic Locking
- [x] Spec Agent (`spec-lambda`) con Spec-kit prompts portados
- [ ] Deploy a AWS (pendiente aprobación explícita)

## Stack

- **Runtime Lambda**: Python 3.12
- **IaC**: AWS SAM (`template.yaml`)
- **LLM**: Claude 4.7 Opus via Anthropic API (no Bedrock)
- **Estado**: DynamoDB (PK=`run_id`, SK=`item_type`) con versión para locking optimista
- **Artefactos**: S3 con versioning
- **Orquestación**: AWS Step Functions (Etapa 3)

## Setup local

```bash
python -m venv .venv
source .venv/Scripts/activate      # git-bash Windows
pip install -r requirements-dev.txt
```

## Validar IaC

```bash
sam validate --lint
sam build
```

> SAM CLI: instalar desde https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html

## Tests

```bash
pytest tests/unit -v
```

Los tests usan `moto` para mockear S3/DynamoDB y un mock del cliente Anthropic.
**No requieren credenciales AWS ni API key real.**

## Estructura

```
Agentics_Code_Team/
├── template.yaml              # SAM: S3, DynamoDB, Secret, Layer, Lambdas
├── samconfig.toml             # Config de deploy (dev)
├── requirements-dev.txt
├── docs/
│   └── ARCHITECTURE.md        # Fuente de verdad — plan completo
├── statemachine/
│   └── main.asl.json          # Placeholder (Etapa 3)
├── lambdas/
│   └── spec_agent/            # Etapa 1 — Spec Agent
├── shared/
│   └── layer/                 # DynamoDB + S3 repos compartidos
└── tests/
    └── unit/
```

## Despliegue (pendiente aprobación)

```bash
# 1. Configurar API key real en Secrets Manager (post-deploy)
aws secretsmanager put-secret-value \
    --secret-id agentics-code-team/dev/anthropic-api-key \
    --secret-string '{"ANTHROPIC_API_KEY":"sk-ant-..."}'

# 2. Build + deploy
sam build
sam deploy --guided      # primera vez
sam deploy               # subsiguientes
```
