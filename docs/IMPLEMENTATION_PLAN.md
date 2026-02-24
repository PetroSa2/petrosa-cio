---
stepsCompleted: [1]
workflowType: 'implementation_plan'
project_name: 'petrosa_k8s'
service_name: 'petrosa-cio'
date: '2026-02-24'
inputDocuments:
  - _bmad-output/planning-artifacts/prd.md
  - _bmad-output/planning-artifacts/architecture.md
  - _bmad-output/planning-artifacts/epics.md
  - petrosa-tradeengine/tradeengine/config_manager.py
  - petrosa-tradeengine/contracts/trading_config.py
---

# Implementation Plan: petrosa-cio Sovereign Orchestrator

This plan outlines the "Brownfield" construction of the `petrosa-cio` service, focusing on extracting existing configuration intelligence from the fleet and promoting it to a central governance layer.

## 1. User Objective
Initialize and build the `petrosa-cio` service to act as a sovereign gatekeeper for the PetroSa2 ecosystem. The service must intercept trading "intents," validate them against rich semantic risk policies, and promote them to actionable signals for the `petrosa-tradeengine`. The core success metric is **Earnings Transparency**: quantifying both actual profit and prevented losses through a unified governance report.

## 2. Proposed Roadmap (Month 1: The Sovereign Maturity Model)

### Week 1: Foundation & Shadow Calibration (Story 1.1, 1.2)
- **Sovereign Extraction:** Carve out existing contracts and `TradingConfigManager` from TradeEngine.
- **Shadow Validation Interface:** Implement a read-only probe to validate Nurse Pydantic models against live Binance API responses (zero-drift calibration).
- **Telemetry Prep:** Ensure all spanning logic includes `potential_pnl` and `blocked_reason` metadata.

### Week 2: The Signal Interceptor & ROI Engine (Story 2.1, 2.2)
- **NATS Interception:** Implement the `intent.>` to `signals.trading` proxy loop.
- **Shadow ROI Initialization:** Start tracking theoretical PnL of all blocked intents in MongoDB Atlas.
- **Heartbeat & Fail-Safe:** Establish the 200ms hard-timeout governance heartbeat.

### Week 3: Semantic Guarding & Trace-Enforced Control (Story 3.1, 4.1)
- **Market Regime Guard:** Add logical vetoes for volatility and drawdown thresholds.
- **Audit-First Governance:** Update MCP tools to require `thought_trace` for all configuration patches.
- **Discovery:** Expose all semantic schemas as documented MCP tools.

### Week 4: The Fail-Safe Exit & Friday Reporting (Story 4.2, 5.1, 5.2, 6.2)
- **Integrated Nuclear Option:** Standalone `/canary` script for cluster-independent emergency closure.
- **Friday Earning Report:** Consolidated dashboard view of Net Profit + Shadow ROI (Savings).
- **1-Click Rollback:** Snapshot-based configuration reversion via MCP.

## 3. Physical Directory Structure

```text
petrosa-cio/
├── apps/
│   ├── nurse/
│   │   ├── enforcer.py         # Main auditing logic
│   │   ├── defaults.py         # Migrated semantic schemas
│   │   ├── models.py           # Migrated Pydantic contracts
│   │   └── guard.py            # Market regime semantic checks
│   ├── strategist/
│   │   ├── mcp_tools.py        # MCP server integration
│   │   └── memory.py           # Vector Store interface
├── core/
│   ├── nats/
│   │   ├── interceptor.py      # Intent-to-Signal proxy
│   │   └── heartbeat.py        # Health handler
│   ├── db/
│   │   ├── mongo.py            # Audit & Config persistence
│   │   └── redis.py            # High-speed policy cache
│   └── telemetry.py            # Petrosa-Otel setup
├── canary/
│   └── nuclear_option.py       # Standalone fail-safe
├── tests/                      # Pytest hierarchy
└── pyproject.toml
```

## 4. Verification Plan

### Automated Tests
- **Unit Tests:** Validate `TradingConfigManager` migration by re-running TradeEngine config tests.
- **Integration Tests:** 
    - Mock NATS publisher sending `intent.trading.btc`.
    - Verify Nurse promotes it to `signals.trading` within < 50ms.
    - Verify `traceparent` is preserved through the interception.
- **Latency Benchmarking:** Measure end-to-end audit time (Redis lookup + Pydantic validation) to ensure < 50ms target.

### Manual Verification
- **MCP Discovery:** Connect Cursor/Claude to the MCP gateway and verify all 60+ parameters are discoverable with full descriptions.
- **Nuclear Option:** Run `canary/nuclear_option.py --dry-run` to verify API connectivity.
- **Alert Test:** Trigger a "Red Line" breach and verify arrival in Grafana, Otel, and Email inbox.

## 5. Rollback Plan
- **Infrastructure:** Use `git revert` on the `petrosa-cio` repo.
- **Interception:** If the CIO fails, strategies can be manually reverted to publish directly to `signals.trading` (bypassing the interceptor).
- **Database:** MongoDB snapshots provide 1-click configuration rollback.
