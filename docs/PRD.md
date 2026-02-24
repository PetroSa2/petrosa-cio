---
stepsCompleted: [step-01-init, step-02-discovery, step-02b-vision, step-02c-executive-summary, step-03-success, step-04-journeys, step-05-domain, step-06-innovation, party-mode-refinement]
inputDocuments:
  - petrosa_k8s/_bmad-output/planning-artifacts/product-brief-petrosa_k8s-2026-02-23.md
  - petrosa_k8s/_bmad-output/planning-artifacts/research/technical-sanity-hallucination-firewall-research-2026-02-23.md
  - petrosa_k8s/_bmad-output/brainstorming/brainstorming-session-2026-02-23.md
  - petrosa_k8s/_bmad-output/project-context.md
  - petrosa_k8s/docs/ARCHITECTURE.md
  - petrosa_k8s/docs/NATS_TOPICS.md
  - petrosa_k8s/docs/PETROSA_OTEL_DEVELOPMENT.md
documentCounts:
  briefCount: 1
  researchCount: 1
  brainstormingCount: 1
  projectDocsCount: 4
classification:
  projectType: 'governance_platform'
  domain: 'fintech_agentic'
  complexity: 'high'
  projectContext: 'brownfield_orchestration'
workflowType: 'prd'
project_name: 'petrosa_k8s'
user_name: 'Yurisa2'
date: '2026-02-23'
---

# Product Requirements Document: petrosa_k8s

**Date:** 2026-02-23
**Author:** Yurisa2
**Status:** Phase 1 Final - Architecture Ready

---

## Executive Summary

**petrosa_k8s** is a high-stakes **Sovereign CIO Governance Platform** designed to provide the individual investor ("The Architect") with machine-speed oversight and "peace of mind." By orchestrating a fleet of independent trading bots through an LLM-driven reasoning layer, the system solves the bottleneck of manual 24/7 monitoring. It leverages a non-negotiable **"Nurse" safety layer** to ground AI reasoning in real-time Pydantic contracts and an **"Evolutionary Loop"** to transform historical failures into future profitability. The platform enables the transition from "Dumb Bot Execution" to "Holistic Intelligence," allowing for safe capital scaling and autonomous business financing.

### What Makes This Special

Unlike standard algorithmic trading tools that focus on narrow technical variables (lags, delays), **petrosa_k8s** provides **Bounded Autonomy** through an integrated **MCP Sidecar** and **NATS Parallel Governor**. Its core uniqueness lies in the **Strategist-Auditor** pattern, where every "intelligent" thought is verified by a deterministic logic gate. This approach ensures that the system is reactive enough to "blow the whistle" on rogue behavior within 60 seconds while being resilient enough to "fail-safe" if the AI brain encounters an error or token depletion.

## Project Classification

- **Project Type:** Governance Platform (Specialized API Backend)
- **Domain:** Fintech / Autonomous Quantitative Trading
- **Complexity:** High (High-stakes real-time reasoning + Institutional Memory)
- **Project Context:** Brownfield Orchestration (Integrating with existing NATS/K8s cluster)

---

## Success Criteria

### User Success
- **Reclaiming Monitoring Time:** > 90% reduction in daily manual dashboard oversight for the investor.
- **Decision Confidence:** The Architect achieves 100% "peace of mind" knowing that all autonomous actions are grounded in Pydantic-validated market data.
- **Operational Sovereignty:** Successful deployment of a "Digital Proxy" that autonomously finances company operations without 24/7 human guidance.

### Business Success
- **Risk-Adjusted Yield:** Maintaining a trailing 30-day Portfolio Sharpe Ratio of > 2.0.
- **Alpha Protection:** Minimizing slippage to < 5% of gross profit lost to market impact (ensuring execution quality).
- **Capital Preservation:** Zero breaches of the hard 15% Maximum Drawdown (MDD) ceiling.

### Technical Success
- **Grounding Confidence:** 100% verification of trade orders against live exchange API JSON; zero "Phantom Trades" reaching the exchange.
- **System Reactivity:** < 60 seconds to trigger a full strategy "Halt" or "Safe Mode" configuration patch upon detection of a regime shift or "Red Line" breach.
- **High-Reliability Alerting:** Zero "Silent Failures"; 100% of AI errors, rate limits, or token depletion events triggered via Triple-Redundancy Alarms (Grafana, Otel, Email).
- **Safety Tax Transparency:** Implementation of a "Shadow ROI" metric to quantify opportunity cost (missed alpha) from blocked trades.

---

## Product Scope

### MVP - Minimum Viable Product
- **MCP Configuration Server:** A central `petrosa-cio` service exposing existing bot FastAPI configuration routes to the LLM.
- **Dynamic Risk Gating:** LLM-driven management of both Strategy-Specific (stops, sizes) and Global Risk (total exposure) parameters.
- **Pydantic Hard-Gates (Nurse Auditor):** Direct enforcement of existing `TradeOrder` and `StrategyConfig` models as non-negotiable logic boundaries. **Must block trades in < 50ms.**
- **Asynchronous Policy Cache:** A Redis-backed policy store that mirrors LLM-generated governance rules for < 50ms enforcement.
- **Config Versioning:** Automated "Last Known Good State" snapshots in MongoDB Atlas for instant rollback of failed config patches.
- **Triple-Redundancy Alerting:** Automated emergency alarms via Grafana (Visual), OpenTelemetry (Trace Errors), and Email (Direct).
- **Deterministic Heartbeat:** Bots MUST include a 200ms hard timeout on CIO audit requests. If reached, they fail-open (conservative) and trigger a RED alert.

### Growth Features (Post-MVP)
- **Evolutionary Loop:** Automated strategy post-mortems and Pydantic-validated code mutations proposed via Pull Request.
- **Institutional Memory:** Active **MongoDB Atlas native Vector Search** for historical failure similarity checks during trade proposals.
- **Shadow ROI (Opportunity Cost):** The CIO Audit Log tracks 'Blocked Trade UUIDs' and calculates theoretical ROI for 48h to quantify the "Safety Tax."
- **Commander Dashboard:** High-signal visual overlays including the "Reasoning-to-Reality" Radar and interactive reasoning playback.

### Vision (Future)
- **Self-Improving Sovereign Fund:** A fully autonomous ecosystem that researches, writes, and tests its own alpha based on institutional memory.
- **Somatic Haptic Awareness:** Using mobile haptics to "feel" the heartbeat and confidence levels of the portfolio in real-time.

---

## User Journeys

### 1. The Architect: Deployment and "Aha!" Moment
- **Opening Scene:** Yurisa2 ("The Architect") is in the terminal, looking at a cluster of five independent trading bots. They are working, but the management overhead is constant. The Architect deploys the `petrosa-cio` service as a central orchestrator.
- **Rising Action:** The Architect defines the "Red Lines" in the unified Pydantic config. They watch the NATS log as the CIO Agent starts "listening" to the signals. The Architect then goes offline to focus on their primary company's engineering tasks.
- **Climax:** A sudden 5% market dip occurs. The CIO Agent detects a "Regime Shift" and a 1.5x backtest variance breach. It immediately calls the bot fleet's FastAPI config routes to slash position sizes and sends a `bot.halt` signal to the most aggressive strategy.
- **Resolution:** The Architect returns later to an automated report. The system protected the company runway while they were gone. The "aha!" moment: realization that the "Nurse" works, providing true peace of mind.

### 2. The Investor: High-Stakes Recovery (Edge Case)
- **Opening Scene:** "The Investor" persona is monitoring the portfolio's Sharpe ratio. Suddenly, the CIO Agent encounters a 500 error from the LLM provider—the "Brain" has gone dark.
- **Rising Action:** The **Triple-Redundancy Alert** triggers. The Investor receives a high-priority email while simultaneously seeing a red alert in Grafana. The existing trading bots, sensing the CIO's silence, "fail-safe" back to their original hard-coded logic.
- **Climax:** The Investor takes the wheel, using the **Commander Dashboard** to manually review the last state recorded in MongoDB Atlas. They see that the bots are holding safely but not taking new high-risk entries.
- **Resolution:** The Investor manually restarts the CIO service after the provider outage resolves. The system syncs with the current market state from Redis and resumes autonomous "Coach" duties.

### 3. The Governor: Strategy Evolution (Growth Path)
- **Opening Scene:** The portfolio has hit its 15% drawdown limit over a rough week. The CIO Agent triggers a mandatory "Post-Mortem."
- **Rising Action:** The Agent queries **MongoDB Atlas Vector Search** for similar historical failures. It retrieves a "Lesson Learned" from a previous sideways regime.
- **Climax:** The "Evolutionary Loop" proposes a code mutation—a Pull Request that adjusts the entry logic to include a volume-filter.
- **Resolution:** The Architect reviews and approves the PR. The sovereign fund is now smarter than it was a week ago, moving closer to "Financing on Autopilot."

### Journey Requirements Summary
- **Capability 1 (Orchestration):** Centralized `petrosa-cio` service with NATS subscription/publication and MCP server.
- **Capability 2 (Safety):** Pydantic-based configuration "Nurse" with hard-coded logic gates.
- **Capability 3 (Alerting):** Triple-redundancy alerting system integrated with Grafana, Otel, and Email.
- **Capability 4 (Memory):** MongoDB Atlas native Vector Search for institutional memory and similarity queries.
- **Capability 5 (Automation):** GitOps-driven Pull Request generation for strategy mutations.

---

## Domain-Specific Requirements

### Compliance & Regulatory
- **Self-Auditing Reasoning Trail:** The system must maintain an immutable log in MongoDB Atlas of all "Strategist" proposals and "Auditor" approvals, linked via `petrosa-otel` trace IDs for future financial audits.
- **Data Protection:** All exchange API keys and sensitive credentials must be stored in a Kubernetes Secret or a Vault, injected into the CIO service at runtime, and never exposed to the LLM's prompt context.

### Technical Constraints
- **Sub-50ms Policy Enforcement:** The "Nurse" validation loop (NATS interception -> Redis Policy Check -> Pydantic validation) must complete in < 50ms.
- **Decoupled Reasoning:** The LLM (Policy Maker) acts asynchronously; trades are validated against cached policies to avoid LLM latency bottlenecks.
- **API Rate Limit Awareness:** The CIO must monitor exchange-level rate limits via the Trade Engine's metadata and autonomously throttle its own "Config Patch" requests to prevent blacklisting.

### Integration Requirements
- **NATS Multi-Subject Interoperability:** Secure communication between the `petrosa-cio` orchestrator and the existing bot fleet using verified Pydantic v2 contracts.
- **MCP Tool Fidelity:** 100% type-safety for all FastAPI endpoints exposed to the Agent via the Model Context Protocol.

### Risk Mitigations
- **Targeted De-Risking (15-20% MDD):** Upon breaching 15% drawdown, the CIO enters "Halt & Protect" mode (tighter stops, zero new entries) to attempt a graceful recovery within a 5% safety buffer.
- **MFA for Gate Mutations:** Any modification to core safety constants (Nurse Hard-Gates) in the codebase MUST require manual Git PR approval with MFA (Protected Branches).
- **The "Nuclear Option" (Standalone Switch):** If the portfolio breaches 20% drawdown, a local, standalone Python script in `/canary` is triggered, bypassing all cluster/NATS logic to "Close All & Hibernate."
- **Out-of-Band (OOB) Canary:** An external monitor (GitHub Action/AWS Lambda) that watches the `petrosa-cio` heartbeat and issues critical alerts if the K8s cluster or NATS bus fails.
- **Triple-Redundancy Fail-Safe:** If the `petrosa-cio` service itself errors out, existing trading bots must detect the missing heartbeat and default to their original, conservative hard-coded logic.

---

## Innovation & Novel Patterns

### Detected Innovation Areas
- **Democratic Sovereign Fund:** Challenging the monopoly of large institutions by building an autonomous CIO that provides institutional-grade governance (Strategist-Auditor loop) for an individual investor.
- **Surgical Logic Interception:** Using NATS to "soft-intercept" bot intent without a complex infrastructure rewrite—bringing high-level intelligence to a lightweight cluster.
- **Institutional Memory via MongoDB Atlas:** Native vector search allowing a small-scale fund to perform semantic "failure similarity" checks, an innovation usually reserved for massive data science teams.

### Market Context & Competitive Landscape
- **Democratization of Quant:** Moving from "Dumb Retail Bots" to "Intelligent Sovereign Agents." While hedge funds use proprietary stacks, the innovation here is building a comparable governance layer using accessible open-source (NATS, Pydantic, Mongo) and LLM technology.

### Validation Approach
- **Parallel Shadow Mode:** Validating the complex "Intelligence" by running the CIO service in a **Shadow Configuration**. The Agent will record its "proposals" and "reasons" in MongoDB alongside the *actual* market events, allowing for a statistical comparison of "AI Decision vs. Actual Outcome" before the Agent is given live "hands."

### Risk Mitigation
- **Deterministic Defaults:** If the innovative CIO brain fails (errors, hallucinations, token depletion), the bots instantly fallback to their **conservative, hard-coded default parameters**. This ensures that the "Sovereign Innovation" can never compromise the fundamental safety of the capital.

---

<!-- Content will be appended sequentially through PRD workflow steps -->
