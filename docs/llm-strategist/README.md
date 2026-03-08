# LLM Strategist Project Documentation

This directory contains the comprehensive documentation for the Petrosa CIO LLM Strategist project.

## 🚀 Project Status

| Milestone | Epic | Status | Description |
|---|---|---|---|
| **Phase 1** | **Epic 1: The Foundation** | ✅ **DONE** | Domain models, LLM Client interface, and behavioral Mock. |
| **Phase 1** | **Epic 2: Reasoning Loop** | ✅ **DONE** | Orchestrator, Code Engine, and Decision Assembly logic. |
| **Phase 1** | **Epic 3: Personas** | ✅ **DONE** | Implementation of Regime Analyst, Strategy Assessor, and Action Classifier. |
| **Phase 1** | **Epic 4: Integration** | ✅ **DONE** | NATS/HTTP integration, production wiring, and containerization. |
| Phase 2 | Epic 5: Live Mock Obs | ✅ **DONE** | Deployment to K8s with Mock LLM for live data verification. |
| Phase 2 | Epic 6: Real LLM Switch | ✅ **DONE** | Transition to LiteLLM with real API keys and usage monitoring. |
| Phase 2 | **Epic 8: Shadow Rollout** | ✅ **DONE** | Requesty integration and DRY_RUN safety latch. |
| Phase 2 | **Epic 9: Pre-Flight Sync** | ✅ **DONE** | T-Junction logic, Quantity Fix, and cross-repo alignment. |
| Phase 2 | **Epic 10: Testing & Rollout** | ✅ **DONE** | Comprehensive plan for local simulation and cluster deployment. |
| Phase 3 | Epic 7: The COLD Path | ✅ **DONE** | Vector DB integration for deep historical context. |

## Documentation Structure

### 📂 [Architecture](architecture/)
System design, decision frameworks, and component interactions.
- `overview.md`: High-level system overview, design principles, and orchestration pipeline.
- `decision_framework.md`: Deep dive into the Code Engine, Decision Paths (HOT/WARM/COLD), and Reasoning Loop.

### 📂 [Project Plan](project_plan/)
Detailed execution plans and story breakdowns.
- `epic_1_foundation_stories.md`: Finalized stories and acceptance criteria for Epic 1.

### 📂 [Decisions](decisions/)
Records of key project decisions and approvals.
- `party_mode_kickoff.md`: Phase 2 approval and project launch.
- `epic_1_grooming_notes.md`: Detailed refinements for Epic 1.

### 📂 [Guides](guides/)
Developer guides and best practices.
- `prompt_engineering.md`: Global rules, prompt structures, and versioning policy.

### 📂 [API Reference](api_reference/)
Internal API definitions and data models.
- `data_models.md`: Reference for Pydantic models (Regime, Strategy, Context) and Enums.

### 📂 [Investigations](investigations/)
Original research documents that formed the basis of this project.
- `001-llm-infrastructure.md`
- `002-cio-strategy-parameter-management.md`
- `003-cio-intelligence-framework.md`
- `004-llm-prompt-guide-reference.md`

---

**Project Goal:** Build an autonomous quantitative trading intelligence system where an LLM (the CIO) manages trading decisions end-to-end — from market analysis to strategy adjustment to execution — without human intervention, governed by strict code-based safety limits.
