# Multi-Agent Code System — AWS Lambda + AWS Step Functions
> Plan de arquitectura avanzado y optimizado. Listo para implementar con Claude Code.

---

## Stack Tecnológico

| Capa | Tecnología | Rol |
|---|---|---|
| Orquestación Core | AWS Step Functions | Supervisor principal, retry nativo, timeouts extendidos, manejo de estados paralelos |
| Especificación | Spec-kit (Anthropic) | Definición rigurosa de requerimientos antes de generar código |
| Razonamiento Interno | LangChain / LangGraph | Tool calling y flujos cíclicos internos dentro de Lambdas específicas |
| LLM | Claude 4.7 Opus (API) | Backbone de inteligencia de todos los agentes |
| Compute | AWS Lambda | Un agente por función serverless (escalado independiente) |
| Estado del Workflow | SFN State + DynamoDB | State handling de Step Functions + persistencia con Optimistic Locking |
| Almacenamiento | S3 | Código fuente (código base), versiones (diffs) y outputs finales |
| Entry Point | API Gateway | REST endpoint de entrada para disparar statemachines |
| Observabilidad | CloudWatch + X-Ray | Trazas, logs estructurados JSON, alertas de fallos |

---

## Flujo de Trabajo y los 7 Agentes del Sistema

### 1. Spec Agent (Nuevo) — `spec-lambda` 
- Utiliza **Spec-kit** para transformar los requerimientos crudos del usuario en una Especificación Técnica estructurada, exhaustiva y testeable.
- Define contratos de API, esquemas de base de datos y criterios de aceptación.

### 2. Supervisor (Orquestador) — `AWS Step Functions`
- Reemplaza al Supervisor Lambda para evitar el límite de 15 minutos.
- Coordina el flujo completo, manejando los estados de *Map/Parallel* y reintentos automáticos.
- Mantiene el contador de iteraciones y decide si enrutar a Output o al Iterator.

### 3. CodeGen Agent — `codegen-lambda` (Flujo Multi-Paso)
- Lee la especificación del Spec Agent.
- Para evitar límites de tokens y mantener código limpio, genera en fases:
  1. **Estructura:** Crea carpetas y archivos base vacíos.
  2. **Interfaces/Domain:** Genera los contratos.
  3. **Implementación:** Rellena la lógica.
  4. **Tests:** Escribe pruebas unitarias.
- Adaptable: Usa arquitectura plana Serverless para requerimientos simples, o DDD para alta complejidad.

### 4. Code Review Agent — `review-lambda`
- Evalúa calidad, legibilidad, performance, edge cases y principios SOLID.
- Retorna score 0–100 + lista de issues.

### 5. Security Agent — `security-lambda`
- Checks OWASP Top 10 (Injection, IAM Overpermission, Hardcoded Secrets).
- Retorna issues con severidad CRITICAL, HIGH, MEDIUM. Un CRITICAL fuerza iteración o fallo.

### 6. Scalability Agent — `scalability-lambda`
- Verifica latencia de base de datos (N+1 queries), bloqueos, uso de memoria y cloud-native patterns.

### 7. Consolidator & Iterator Agent — `iterator-lambda`
- **Fase Consolidación:** Step Functions une los outputs de Review, Security y Scalability. Si hay feedback contradictorio, este agente lo resuelve antes de codificar.
- **Fase Iteración:** Genera parches (diffs) sobre el código en S3 en lugar de reescribir todo desde cero (ahorro masivo de costos).
- Límite de 3 intentos. Posee memoria de iteraciones pasadas para evitar bucles de errores (Anti Ping-Pong).

---

## Arquitectura de Ejecución (Visualización Step Functions)

```text
Input: { requirements, language, complexity }
  │
  ▼
[Task: Spec Agent (Spec-kit)] → Genera Technical Spec.json
  │
  ▼
[Task: CodeGen Agent (Multi-step)] → Sube v1 del Código a S3
  │
  ├─────────────────────┬─────────────────────┐
  ▼                     ▼                     ▼
[Code Review]      [Security]          [Scalability]   ← PARALELO (SFN Parallel State)
score + issues     vulnerabilities     arquitectura
  │                     │                     │
  └─────────────────────┴─────────────────────┘
                         │
        [SFN ResultAggregation] JSON Combinado
                         │
  ¿Score >= 85 AND Critical == 0 AND Scalability == Pass?
            ┌────────────┴────────────┐
            ▼                         ▼
         ✅ SÍ                      ❌ NO
    [Output Task]             ¿Iteraciones < 3?
    Notifica SN / S3             │
                                 ├── Sí: [Task: Iterator Agent (Aplica parche)] → Vuelve a Eval Parallela
                                 └── No: [Task: Fail State] → Alerta a humano
```

---

## Estructura Adaptativa del Proyecto Generado

**Nivel Complexity: Enterprise (DDD)**
```
project-name/
├── specs/                  # Generado por Spec-kit
├── src/
│   ├── domain/             # Entities, interfaces
│   ├── application/        # Use cases
│   ├── infrastructure/     # DB, APIs externas
│   └── interfaces/         # Handlers HTTP/Lambda
├── tests/
```

**Nivel Complexity: Serverless Simple**
```
project-name/
├── specs/
├── src/
│   ├── handlers/           # Entrypoints Lambda
│   ├── services/           # Lógica de negocio core
│   └── database/           # Consultas simples
├── tests/
```

**Principios del CodeGen Agent:**
- Configuración vía variables de entorno (12-Factor App).
- Inyección de dependencias (para facilitar testing Mocks).
- Gestión de dependencias en Lambda Layers si se requiere.
- Logs estructurados nativos con `aws-lambda-powertools`.

---

## Plan de Ejecución (4 Etapas)

### Etapa 1 — Especificación e Infraestructura
- [ ] Inicializar workspace local usando `Claude Code`.
- [ ] Configurar proyecto IaC (Terraform o AWS SAM) para las Lambdas de manera automatizada.
- [ ] Construir y probar el **Spec Agent** integrando Anthropic `Spec-kit`.
- [ ] Setup del bucket S3 para manejo de zip/artefactos entre Lambdas.

### Etapa 2 — Generación y Review en Paralelo
- [ ] Implementar **CodeGen Agent** con lógica de multi-paso (Architecture -> Domain -> Impl).
- [ ] Desarrollar los 3 agentes validadores (Review, Security, Scalability).
- [ ] Utilizar prompts con contextos estrictos de Claude 4.7 Opus.

### Etapa 3 — Orquestación con Step Functions
- [ ] Escribir la definición `state-machine.asl.json` para AWS Step Functions.
- [ ] Implementar el **Iterator Agent** capaz de resolver conflictos de los 3 agentes y generar diffs.
- [ ] Conectar todo el flujo, asegurar correcto manejo del SFN Payload size (pasando punteros de archivos de S3, no código crudo en JSON).

### Etapa 4 — Ecosistema y Despliegue Producción
- [ ] Test Integrales e-2-e corriendo ejemplos reales de prompts.
- [ ] Monitoreo y dashboards con AWS X-Ray.
- [ ] Crear documentación y configuración específica de `Claude Code` (creando un script de herramientas custom/MCP para auditar la DB o el S3).

---

## Flujo de inicialización de Trabajo de Claude Code

Cuando abras la terminal e inicies **Claude Code** por primera vez, entrégale el siguiente prompt:

> "Inicia la implementación de la Etapa 1 del proyecto según el documento `ARCHITECTURE_MULTI_AGENT_LAMBDA.md`. Tu primera tarea es estructurar el repositorio en forma de monorepo con AWS SAM (o Terraform) para gestionar las Lambdas, e implementar el Spec Agent utilizando principios de Spec-kit. Recuerda que no vamos a usar LangGraph para orquestación global, usaremos AWS Step Functions. Ve paso por paso y confirma conmigo cada estructura que generes antes de commitear."
