stepsCompleted: [1, 2, 3]
inputDocuments:
  - _bmad-output/planning-artifacts/prd.md
  - _bmad-output/planning-artifacts/architecture.md
  - _bmad-output/planning-artifacts/product-brief-petrosa_k8s-2026-02-23.md
  - petrosa-tradeengine/tradeengine/config_manager.py (Existing Reference)
  - petrosa-tradeengine/contracts/trading_config.py (Existing Reference)
---

# petrosa_k8s - Epic Breakdown (Brownfield Adherence Phase)

## Overview

This document provides a refactored epic breakdown for the petrosa-cio service. It focuses on a "Sovereign Extraction" strategy: moving existing configuration management and validation logic from the petrosa-tradeengine into a central orchestrator.

## Requirements Inventory

### Functional Requirements

FR1: Centralized `petrosa-cio` service exposing existing bot FastAPI config routes via MCP.
FR2: LLM-driven management of Strategy-Specific and Global Risk parameters (Dynamic Risk Gating).
FR3: Pydantic Hard-Gates (Nurse Auditor) validating trades in < 50ms.
FR4: Redis-backed Asynchronous Policy Cache for < 50ms enforcement.
FR5: MongoDB Atlas Config Versioning (snapshot/rollback) for failed patches.
FR6: Triple-Redundancy Alerting (Grafana, Otel, Email) for AI and system failures.
FR7: Deterministic Heartbeat (200ms timeout on audit requests) with conservative fail-open.
FR8: Shadow ROI tracking for blocked trades post-blockage (Growth).
FR9: MFA for any modifications to core safety constants (Nurse Hard-Gates).
FR10: Standalone "Nuclear Option" script in `/canary` for bypassing cluster/NATS logic.

### NonFunctional Requirements

NFR1: sub-50ms Policy Enforcement latency (Redis lookup + Pydantic validation).
NFR2: Zero "Phantom Trades" reaching the exchange (100% verification against live JSON).
NFR3: < 60s reactivity to trigger strategy "Halt" or "Safe Mode" configuration patches.
NFR4: High-Reliability Alerting (Zero "Silent Failures").
NFR5: 100% trace-to-thought mapping stored immutable in MongoDB Atlas.
NFR6: Deployment in `petrosa-apps` K8s namespace with non-root user (1000).

### Additional Requirements
- **Codebase Adherence:** MUST reuse `contracts/trading_config.py` and `tradeengine/defaults.py` schemas.
- **Service Promotion:** Extract `TradingConfigManager` functionality from TradeEngine to CIO repo.
- **Interception Pattern:** Strategies publish `intent`, CIO validates and publishes `signals`.
- **Zero-Touch Fleet:** The existing `petrosa-tradeengine` should require minimal or zero code changes to receive approved signals.
- **Core Strategy:** Decoupled Strategist-Nurse model using NATS Request-Reply for interception.
- **Runtime:** Python 3.11+ strictly using `asyncio` for non-blocking I/O.
- **Telemetry:** Mandatory `petrosa-otel` context propagation via NATS headers (`traceparent`).
- **Structure:** Domain-driven layout: `/apps/nurse` (logic), `/apps/strategist` (brain), `/core/nats` (mesh).
- **Security:** Kubernetes Secrets for all credentials; strictly isolated from LLM reasoning context.
- **Intervention:** Manual Git PR approval with MFA required for all "Primal" safety constant updates.

### FR Coverage Map

FR1: Epic 1 - Sovereign Extraction & Migration
FR2: Epic 4 - MCP Governance (Existing Schemas)
FR3: Epic 2 - Signal Interception (The Nurse Gate)
FR4: Epic 2 - Redis-backed Policy Sync
FR5: Epic 4 - Artifact Snapshots & Rollbacks
FR6: Epic 5 - Alert Distillation & Notifications
FR7: Epic 1 - Standardized Heartbeats
FR8: Epic 6 - Shadow ROI & Vector Memory
FR9: Epic 3 - Semantic Intent Guarding
FR10: Epic 5 - Nuclear Option (Simulation Ready)

## Epic List

### Epic 1: Sovereign Extraction & Shared Base
Initialize the `petrosa-cio` repo by "carving out" the existing configuration management and contract logic from the TradeEngine.
**FRs covered:** FR1, FR7

### Epic 2: The Signal Interceptor (The Nurse)
Insert the CIO between the Strategies and the TradeEngine. strategies -> intent -> CIO -> signals -> TradeEngine.
**FRs covered:** FR3, FR4

### Epic 3: Semantic Guarding & Safety Gating
Enhance the existing Pydantic validation with "Market Regime" awareness and logical intent checks (< 50ms).
**FRs covered:** FR9

### Epic 4: Configuration Mastery (MCP Gateway)
Wrap the existing (migrated) LLM configuration routes into the MCP toolset for the Strategist.
**FRs covered:** FR2, FR5

### Epic 5: Resilient Alerts & Standalone Fail-Safes
Implement the "Nuclear Option" and Triple-Redundancy alerting with signal distillation.
**FRs covered:** FR6, FR10

### Epic 6: Reflective Auditing & Institutional Memory
Establish the Vector Store for historical reasoning and the Shadow ROI calculation engine.
**FRs covered:** FR8

## Epic 1: Sovereign Extraction & Shared Base
**Goal:** Migrate the existing "Intelligence" from petrosa-tradeengine to petrosa-cio while maintaining 100% adherence to existing models.

### Story 1.1: Shared Contracts & Scaffolding
As an Architect,
I want to initialize the `petrosa-cio` service and extract existing `contracts` from the TradeEngine,
So that the fund has a single unified definition of a `Signal` and `TradingConfig`.

**Acceptance Criteria:**
- **Given** the source repo `petrosa-tradeengine`
- **When** I initialize the new `petrosa-cio` service
- **Then** `contracts/trading_config.py` and `contracts/signal.py` are copied and updated for the new service.
- **And** the project structure follows the PetroSa2 standard (Poetry 3.11+, Ruff, Makefile).

### Story 1.2: TradingConfigManager Migration
As a Developer,
I want to port the `TradingConfigManager` and `tradeengine/defaults.py` to the CIO service,
So that the CIO becomes the authoritative owner of risk policy.

**Acceptance Criteria:**
- **Given** the existing manager logic
- **When** I migrate the `config_manager.py` to the CIO
- **Then** the CIO can successfully load, merge, and persist configurations using the existing MongoDB/Redis patterns.
- **And** all 800+ lines of LLM-friendly schemas from `defaults.py` are preserved.

## Epic 2: The Signal Interceptor (The Nurse Gate)
**Goal:** Implement the "Man-in-the-Middle" NATS pattern to move from unsupervised execution to audited intent.

### Story 2.1: Intent-to-Signal Proxy
As a Developer,
I want the CIO to subscribe to `cio.intent.>` and re-publish approved messages to `signals.trading`,
So that the existing TradeEngine requires zero changes to its consumer logic.

**Acceptance Criteria:**
- **Given** a strategy sending an intent on `cio.intent.btc`
- **When** the message is received by the CIO
- **Then** it is validated via the `config_manager`.
- **And** if approved, it is published to `signals.trading` with identical payload structure.
- **And** it includes the `_otel_trace_context` for full observability.

### Story 2.2: Deterministic Heartbeat & Service Health
As an Architect,
I want the CIO to provide a high-frequency, non-blocking heartbeat on NATS,
So that bots can fail-safely if the CIO becomes unreachable.

**Acceptance Criteria:**
- **Given** a NATS ping on `cio.heartbeat`
- **When** received
- **Then** the CIO responds within 20ms confirming `GOVERNANCE_ACTIVE`.

## Epic 3: Semantic Guarding & Safety Gating
**Goal:** Add "Logical Safety" on top of the existing Pydantic "Type Safety."

### Story 3.1: Market Regime Semantic Guard
As a PM,
I want the Nurse to veto trades that violate current market regime logic (e.g., "Don't increase size in high vol"),
So that hallucinations that are "type-valid" but "strategically insane" are blocked.

**Acceptance Criteria:**
- **Given** a valid `Signal` intent
- **When** checked by the Semantic Guard
- **Then** it cross-references the trade against the `RegimeStatus` (cached in Redis).
- **And** it blocks the trade if it violates the current drawdown or volatility phase multipliers.

## Epic 4: Configuration Mastery (MCP Gateway)
**Goal:** Expose the migrated risk configuration logic to the LLM via the MCP toolset.

### Story 4.1: MCP Discovery of Existing Schemas
As a Strategist (LLM),
I want the CIO to expose the `tradeengine/defaults.py` schemas as MCP tools,
So that I have deep context on every parameter's impact and when-to-use tips.

**Acceptance Criteria:**
- **Given** the CIO service is running
- **When** the LLM connects via MCP
- **Then** it discovers tools that mirror the `api_config_routes.py` functionality with full semantic documentation.

### Story 4.2: Audit Snapshots & Rollback Engine
As an Architect,
I want to leverage the existing MongoDB Audit trail to provide 1-click rollbacks for the LLM,
So that we have constant "Undo" capability for any configuration experiment.

**Acceptance Criteria:**
- **Given** a configuration update
- **When** the patch is applied
- **Then** the CIO snapshots the pre-patch state.
- **And** an MCP tool `rollback_to_version` is provided to the LLM.

## Epic 5: Resilient Alerts & Standalone Fail-Safes
**Goal:** Worst-case scenario handling.

### Story 5.1: Triple-Redundant Alerting & Distiller
As an Architect,
I want the CIO to dispatch high-fidelity alerts via Grafana, Otel, and Email,
So that critical breaches are never missed.

### Story 5.2: Standalone Nuclear Option (Simulation-Ready)
As an Architect,
I want a standalone `/canary` script to "Close All" that works outside the K8s/NATS cluster,
So that I can stop the fund even during a total platform failure.

## Epic 6: Reflective Auditing & Institutional Memory
**Goal:** High-level strategic learning.

### Story 6.1: Vector-Based Institutional Memory
As a Strategist (LLM),
I want a Vector Store of all past reasoning traces and failures,
So that I can perform semantic retrieval to avoid repeating historical errors.

### Story 6.2: Shadow ROI (The Safety Tax)
As an Investor,
I want the CIO to track the virtual PnL of all blocked trades,
So that I can see the quantified value of the "Nurse" safety layer.
