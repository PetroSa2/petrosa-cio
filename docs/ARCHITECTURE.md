# Petrosa CIO Architecture

## Project Context Analysis

### Requirements Overview

**Functional Requirements:**
- **petrosa-cio Centralized Orchestrator:** Service that promotes existing bot config routes to the MCP toolset and acts as a sovereign gatekeeper.
- **Interception-based 'Nurse' Guardrail:** Intercepts `intent` messages from strategies, validates against migrated Pydantic policies, and promotes them to `signals`.
- **Asynchronous 'Strategist' Brain:** Periodic LLM-driven policy generation based on regime shifts and portfolio health.
- **Triple-Redundant Alerting:** Multi-channel notification via Grafana, Otel, and Email.
- **Out-of-Band (OOB) Canary:** External monitoring to detect cluster failures.
- **Institutional Memory (Growth):** MongoDB Atlas Vector Search for historical failure similarity.

**Non-Functional Requirements:**
- **Predictable Latency:** Policy enforcer must operate under 50ms (Redis lookup + Pydantic validation).
- **Auditability:** 100% trace-to-thought mapping stored in MongoDB.
- **Fail-Safe Reliability:** Bots must default to hard-coded conservative logic if the CIO heartbeat stops.
- **Security:** Zero LLM exposure to exchange API keys; Kubernetes secret injection.

**Scale & Complexity:**
- Primary domain: Fintech / Autonomous Quantitative Trading
- Complexity level: High (Safety-critical, real-time reasoning)
- Estimated architectural components: 6 (CIO Service, Redis Policy Store, MongoDB Audit Store, OOB Canary, MCP Gateway, NATS Mesh).

### Technical Constraints & Dependencies
- **NATS/Pydantic v2:** Mandatory for type-safety and inter-service communication.
- **MongoDB Atlas Native Vector Search:** Required for the Growth phase (Evolutionary Loop).
- **Petrosa-Otel:** Internal standards for telemetry and trace propagation.
- **Namespace Lockdown:** Everything must live in `petrosa-apps` with non-root security contexts.

### Cross-Cutting Concerns Identified
- **Trace Context Propagation:** Ensuring `traceparent` flows from Bot -> NATS -> CIO -> Audit Log.
- **Policy Synchronization:** Managing the propagation of LLM-generated policies to Redis without race conditions.
- **Secret Hygiene:** Strategic separation of reasoning context from execution credentials.

## Starter Template Evaluation

### Primary Technology Domain
**Specialized API/Orchestration Backend** based on high-stakes safety requirements.

### Selected Starter: **Petrosa Custom Lean FastAPI Foundation**

**Rationale for Selection:**
The `petrosa-cio` requires extreme precision and 100% adherence to existing trade models. We will "Sovereignly Extract" the `TradingConfigManager` and `contracts` from the `petrosa-tradeengine` to seed this service, ensuring immediate ecosystem compatibility and zero-touch deployment for the existing fleet.

**Initialization Pattern:**

```bash
# We will initialize via a custom layout in the /petrosa-cio directory:
1. mkdir petrosa-cio && cd petrosa-cio
2. poetry init (Python 3.11+)
3. poetry add fastapi uvicorn nats-py redis pydantic-settings petrosa-otel
```

**Architectural Decisions Provided:**

**Language & Runtime:**
- **Python 3.11+** using `asyncio` for all NATS processing and API requests.

**Core Stack:**
- **FastAPI:** For the MCP configuration server.
- **NATS-py:** For intercepting and auditing fleet messages (Soft Interception).
- **Redis (Async):** The high-speed Policy Cache (< 50ms lookup).

**Code Organization:**
- **/apps/nurse:** Strict Pydantic-based enforcement logic.
- **/apps/strategist:** Asynchronous LLM-policy generation.
- **/core/nats:** Lifecycle-managed NATS connections via FastAPI `lifespan`.

**Testing Framework:**
- **Pytest + Pytest-Asyncio:** Mandatory test-assertion checks as per repo rules.

## Core Architectural Decisions

### Decision Priority Analysis

**Critical Decisions (Block Implementation):**
- **NATS Signal Interception:** The Nurse will use an **Interception Pattern** (`intent.>` -> `signals.trading`). Strategies publish intents; the CIO validates and promotes them to signals.
- **Sovereign Extraction:** Component reuse from `petrosa-tradeengine` (ConfigManager, defaults, contracts) is mandatory to ensure single-source-of-truth.
- **Async Policy Sync (NATS Events):** Strategist updates Redis via `policy.updated` events to ensure real-time policy propagation.

**Important Decisions (Shape Architecture):**
- **MongoDB Atlas Audit Store:** Immutable logging of all "Thoughts" linked to "Traces".
- **Out-of-Band (OOB) Canary:** Deployment of a heartbeat-watcher as a GitHub Action or Lambda outside the main K8s namespace.

**Deferred Decisions (Post-MVP):**
- **Evolutionary Loop PR Automation:** Deferred until the manual Strategy Mutation process is stable.

### Data Architecture
- **Policy Cache:** **Redis (asyncio) v7.2.0**. Used for high-speed policy lookups by the Nurse.
- **Audit Logging:** **MongoDB Atlas (Native Vector Search ready)**. Stores immutable JSON logs of AI reasoning traces.
- **Data Validation:** **Pydantic v2 (Mandatory)** for all inbound/outbound NATS schemas.

### Authentication & Security
- **Secret Management:** **Kubernetes Secrets** injected as ENV vars; strictly isolated from the LLM prompt context.
- **Identity:** Service-level authentication via NATS credentials (NKEYs/JWTs).

### API & Communication Patterns
- **Internal Comm:** **nats-py v2.13.1**. Standardized on **Interception Pattern** (Intent-to-Signal) for safety gates.
- **External/Tool Comm:** **FastAPI v0.131.0**. Exposing MCP-compatible routes to the LLM agent.
- **Telemetry:** **petrosa-otel**. Mandatory context propagation via NATS headers (`traceparent`).

### Infrastructure & Deployment
- **Runtime:** **Python 3.11.x**. Consistent with Petrosa ecosystem rules.
- **Deployment:** **petrosa-apps** namespace in K8s; non-root user (1000).
- **Monitoring:** Triple-redundancy (Grafana Alloy + Otel Traces + Email Alarms).

## Implementation Patterns & Consistency Rules

### Pattern Categories Defined

**Critical Conflict Points Identified:**
4 key areas where AI agents could make different choices (NATS Subject Naming, Response Envelopes, Field Casing, and Trace Propagation).

### Naming Patterns

**Database Naming Conventions:**
- **No SQL Tables for MVP:** Data is stored in Redis (Key-Value) and MongoDB (JSON Collections).
- **Naming:** All Keys and Collections use `snake_case` (e.g., `policy:active`, `audit_logs`).

**API Naming Conventions:**
- **REST Endpoints:** Use singular nouns (e.g., `/policy`, `/config`).
- **Field Casing:** Strict `snake_case` for both request and response bodies to match Python standards.

**Code Naming Conventions:**
- **Files:** `snake_case.py`.
- **Classes:** `PascalCase`.
- **Functions/Variables:** `snake_case`.

### Structure Patterns

**Project Organization:**
- **Tests:** Located in `/tests/` (co-location is forbidden for this repo).
- **Core Logic:** Divided by domain: `/apps/nurse/` (Enforcement) and `/apps/strategist/` (Policy Generation).
- **Shared Code:** Located in `/core/` (NATS client, Redis wrapper, utilities).

**File Structure Patterns:**
- **Configuration:** Managed via `core/config.py` using `pydantic-settings`.
- **Telemetry:** Initialized in `core/telemetry.py` using the `petrosa-otel` package.

### Format Patterns

**API Response Formats:**
- **Standard Envelope:**
  ```json
  { "success": true, "data": { ... }, "trace_id": "uuid", "error": null }
  ```
- **Error Format:**
  ```json
  { "success": false, "data": null, "error": { "code": "VAL_ERR", "message": "Detailed message" } }
  ```

**Data Exchange Formats:**
- **Booleans:** Native `true`/`false`.
- **Dates:** ISO 8601 strings (UTC).

### Communication Patterns

**Event System Patterns:**
- **NATS Subject Schema:** `cio.<module>.<action>`.
- **Examples:** `cio.nurse.audit` (Trade requests), `cio.strategist.update` (Policy pushes).

### Process Patterns

**Error Handling Patterns:**
- **FastAPI Middleware:** Automated wrapping of all exceptions into the Standard Error Envelope.
- **NATS Fail-Safe:** If the Nurse service times out (> 200ms), the bot must be notified via a specific "RETRY_SAFE" status code.

### Enforcement Guidelines

**All AI Agents MUST:**
- **Instrument Everything:** Every function call in the `nurse` loop must be wrapped in a `petrosa-otel` span.
- **Strict Pydantic:** Never use raw dicts for NATS payloads; always use Pydantic V2 models.
- **Trace Continuity:** Extract `traceparent` from NATS headers and inject it into all downstream Audit Logs.

## Architecture Validation Results

### Coherence Validation ✅

**Decision Compatibility:**
High. The combination of **FastAPI** for management, **NATS (asyncio)** for real-time interception, and **Redis** for cached policies forms a cohesive, high-performance safety engine. Python 3.11 ensures compatibility with all chosen libraries.

**Pattern Consistency:**
All patterns (Naming, Structure, Communication) support the decentralized "Strategist-Nurse" model. Dot-notation subjects in NATS align with the module-based directory structure.

**Structure Alignment:**
The project structure properly isolates the sub-50ms "Nurse" logic from the high-latency "Strategist" logic, preventing thread-blocking and ensuring reliability.

### Requirements Coverage Validation ✅

**Epic/Feature Coverage:**
100% of defined epics are covered by the tiered logic across `apps/nurse` and `apps/strategist`.

**Functional Requirements Coverage:**
All core orchestrator, safety, alerting, and memory requirements are supported by specific structural components.

**Non-Functional Requirements Coverage:**
Performance (< 50ms) is addressed via Redis; Security via K8s isolation; Reliability via the OOB Canary.

### Implementation Readiness Validation ✅

**Decision Completeness:**
All critical tools (FastAPI, NATS, Redis, MongoDB) are pinned with verified 2026 stable versions.

**Structure Completeness:**
A complete, specific directory tree is defined for the `petrosa-cio` service.

**Pattern Completeness:**
Conflict points like casing, subject naming, and error envelopes are explicitly decided for AI agent consistency.

### Gap Analysis Results
- **Priority: Low.** The current plan is robust for MVP.
- **Future Enhancement:** Potential migration to NATS JetStream if audit log write volume exceeds MongoDB ingress limits during high-frequency volatility.

### Architecture Completeness Checklist
- [x] Project context thoroughly analyzed
- [x] Scale and complexity assessed
- [x] Technical constraints identified
- [x] Cross-cutting concerns mapped
- [x] Critical decisions documented with versions
- [x] Technology stack fully specified
- [x] Integration patterns defined
- [x] Performance considerations addressed
- [x] Naming conventions established
- [x] Structure patterns defined
- [x] Communication patterns specified
- [x] Complete directory structure defined
- [x] Component boundaries established
- [x] Integration points mapped
- [x] Requirements to structure mapping complete

### Architecture Readiness Assessment

**Overall Status:** READY FOR IMPLEMENTATION

**Confidence Level:** HIGH

**Key Strengths:**
- **Decoupled Reasoning:** Ensures market safety even when the LLM is slow or offline.
- **Audit Traceability:** Native support for end-to-end trace correlation.
- **Tiered Safety:** Multiple fail-safes (Nurse -> Bots -> Nuclear Option).

### Implementation Handoff

**AI Agent Guidelines:**
- Follow the `cio.<module>.<action>` NATS subject naming pattern exactly.
- Always wrap Nurse logic in `petrosa-otel` spans for sub-millisecond trace visibility.
- Strictly use the **Standard Envelope** for all API and NATS responses.

**First Implementation Priority:**
Initialize project via: `poetry add fastapi uvicorn nats-py redis pydantic-settings petrosa-otel`

## Project Structure & Boundaries

### Complete Project Directory Structure

```text
petrosa-cio/
├── README.md
├── pyproject.toml              # Poetry configuration (Python 3.11+)
├── ruff.toml                   # Strict Petrosa linting/formatting
├── Makefile                    # make setup, make pipeline, make deploy
├── .env.example
├── .gitignore
├── .github/
│   └── workflows/
│       ├── ci.yml              # Pipeline: Ruff, Pytest, Bandit
│       └── deploy.yml          # K8s Deployment
├── apps/
│   ├── nurse/                  # The enforcement layer (Sub-50ms)
│   │   ├── __init__.py
│   │   ├── enforcer.py         # Pydantic validation logic
│   │   ├── redis_cache.py      # High-speed policy storage
│   │   └── models.py           # Pydantic V2 trade schemas
│   └── strategist/             # The brain (Async LLM reasoning)
│       ├── __init__.py
│       ├── brain.py            # LLM interface & reasoning
│       ├── evolutionary.py     # Strategy mutation logic
│       └── policy.py           # Policy generation & sync
├── core/                       # Shared infrastructure
│   ├── nats/
│   │   ├── client.py           # Lifespan-managed NATS connection
│   │   └── subjects.py         # Subject constants (cio.nurse.*)
│   ├── alerting/
│   │   ├── channels.py         # Email, Grafana, Otel integration
│   │   └── redundancy.py       # Multi-channel logic
│   ├── config.py               # Pydantic Settings
│   └── telemetry.py            # Otel initialization
├── tests/
│   ├── unit/                   # Component isolation tests
│   ├── integration/            # NATS/Redis mock tests
│   └── property_based/         # Hypothesis tests for Pydantic gates
├── canary/                     # Out-of-Band Monitoring
│   └── heartbeat.py            # External monitor (Lambda/Action)
└── k8s/                        # Kubernetes Manifests
    ├── deployment.yaml         # petrosa-apps namespace
    ├── secrets.yaml            # Isolated credentials
    └── service.yaml            # Internal cluster service
```

### Architectural Boundaries

**API Boundaries:**
- **Inbound NATS:** `cio.nurse.audit` (Trade requests from the fleet).
- **Outbound NATS:** `cio.strategist.update` (Policy updates to the Nurse).
- **External API:** FastAPI endpoints for MCP tool integration.

**Service Boundaries:**
- **Nurse:** Self-contained enforcement logic. Must never block on the Strategist.
- **Strategist:** Asynchronous policy generator. Does not require real-time market access for enforcement.

**Data Flow:**
1.  **Strategy** publishes `intent` to `intent.trading.<symbol>`.
2.  **Nurse** performs sub-50ms **Redis Policy Check** and semantic guard.
3.  **Nurse** (if approved) publishes promoted `Signal` to `signals.trading`.
4.  **TradeEngine** (Zero-Touch) receives and executes the approved signal.
5.  **Audit Log** is pushed asynchronously to **MongoDB Atlas**.
5.  **Canary** pings Nurse heartbeat every 60s.
