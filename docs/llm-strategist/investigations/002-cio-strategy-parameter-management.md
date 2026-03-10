# Investigation: CIO Strategy Parameter Management Integration (Phase 2: LLM-Assisted)

**Date:** 2026-03-08
**Status:** INVESTIGATION COMPLETE - APPROVAL PENDING
**Author:** OpenCode Agent
**Reviewer:** BMAD Party Mode Team Review

---

> ⚠️ **Document Type Note:** This is an investigation document, not an implementation spec. It evaluates feasibility and outlines options. Detailed implementation (exact Python class stubs, specific K8s URLs, detailed env vars) should be created as separate design documents or during story refinement.

---

## Executive Summary

**The Goal:** Build an **autonomous quantitative trading system** where an LLM (the CIO) manages all trading decisions end-to-end — from market analysis to strategy adjustment to execution — without human intervention.

**The Problem:** Currently, the Petrosa fund has:
- 34 trading strategies running across 2 services (28 in TA-bot, 6 in realtime-strategies)
- A CIO that can intercept and veto trades (✅ working)
- **BUT** the CIO cannot adjust strategy parameters dynamically based on market conditions
- **AND** there is no unified "brain" making autonomous decisions

**The Solution:** Enable the CIO to:
1. **Sense** market conditions (regime, volatility, trends) via petrosa-data-manager
2. **Think** analyze strategy performance and decide parameter adjustments
3. **Act** modify strategy parameters in real-time via MCP tools
4. **Learn** from decisions and outcomes via conversation logging

**Business Value:**
- **24/7 Autonomous Operation** — No human required to monitor or adjust strategies
- **Faster Reaction** — Seconds vs hours/days for parameter changes
- **Consistent Decision-Making** — Systematic, data-driven choices
- **Profit Optimization** — Continuous parameter tuning for maximum risk-adjusted returns
- **Capital Preservation** — Automatic scale-down in adverse conditions

---

## The Bigger Picture: Autonomous Trading Intelligence

This investigation is part of a larger vision for the Petrosa fund:

```
Phase 1 (Current): Human-in-the-Loop
├── Human monitors markets
├── Human adjusts strategy parameters
└── Human approves trades

Phase 2 (This Investigation): LLM-Assisted
├── LLM monitors markets (via petrosa-data-manager)
├── LLM suggests parameter changes
├── Human approves changes
└── LLM executes approved changes

Phase 3 (Future - REQUIRES SEPARATE INVESTIGATION): Fully Autonomous
├── LLM monitors markets continuously
├── LLM makes all decisions autonomously
├── LLM learns from outcomes
└── Human only for oversight/escalations
```

> ⚠️ **IMPORTANT:** Phase 3 ("Fully Autonomous") is NOT automatic or inevitable. It requires a separate, dedicated investigation covering:
> - LLM hallucination risks affecting live capital
> - Model drift under novel market conditions
> - Adversarial prompt injection via market data feeds
> - Feedback loops where LLM actions distort the market data it reads
> - Flash-crash scenarios and circuit breakers
> - Regulatory and compliance considerations
>
> **This investigation focuses ONLY on Phase 2 (LLM-Assisted).**

---

## Problem Statement

The idea is for CIO (LLM management layer) to check each strategy's history and control parameters to manage risk. This investigation assesses whether the TA-bot and realtime-strategies services are ready for this integration.

---

## Conclusion

**NOT READY FOR IMPLEMENTATION** - The CIO cannot currently manage strategy parameters in the TA-bot or realtime-strategies services due to architectural gaps that need to be addressed. The services HAVE the APIs, but CIO integration is not yet implemented.

> **Status Update (2026-03-08):** APIs verified to exist in both services. Implementation Option A selected pending formal approval.
>
> **Discovery (2026-03-08):** petrosa-data-manager already has a full Market Regime Classifier with 8 regimes and API endpoint (`/analysis/regime`). Epic 3.1 should integrate with existing data-manager instead of building new regime detection.

---

## Petrosa Fund Objectives

This investigation supports the core objectives of the Petrosa fund:

| Objective | Description | How This Helps |
|-----------|-------------|----------------|
| **Automated Adaptation** | Strategies adjust to changing market conditions | LLM modifies parameters based on regime |
| **Risk-Adjusted Returns** | Maximize returns while preserving capital | Automatic position sizing based on risk |
| **24/7 Operation** | No human monitoring required | LLM makes decisions autonomously |
| **Systematic Trading** | Consistent, data-driven decisions | LLM follows defined decision framework |
| **Continuous Learning** | Improve from past decisions | Conversation logging + outcome correlation |

---

## Success Criteria

| Criterion | Definition | Target |
|-----------|------------|--------|
| **LLM Autonomy** | LLM can modify strategy parameters without human intervention | 100% of parameter changes |
| **Response Time** | Time from regime detection to parameter update | < 60 seconds |
| **Coverage** | Number of strategies controllable by LLM | 34 (28 + 6) |
| **Reliability** | System uptime for param management | 99.9% |
| **Auditability** | All decisions logged with reasoning | 100% |
| **Safety** | Hard limits enforced (drawdown, max orders) | Always |

---

## API Verification (2026-03-08 Review)

✅ **Verified Endpoints in Both Services:**

| Endpoint | Method | TA-bot | Realtime |
|----------|--------|--------|----------|
| `/api/v1/strategies` | GET | ✅ | ✅ |
| `/api/v1/strategies/{id}/config` | GET/POST | ✅ | ✅ |
| `/api/v1/strategies/{id}/config/{symbol}` | GET/POST | ✅ | ✅ |
| `/api/v1/strategies/{id}/audit` | GET | ✅ | ✅ |
| `/api/v1/strategies/{id}/rollback` | POST | ✅ | ✅ |
| `/api/v1/strategies/{id}/schema` | GET | ✅ | ✅ |
| `/api/v1/strategies/{id}/defaults` | GET | ✅ | ✅ |
| `/api/v1/strategies/{id}/config` | DELETE | ✅ | ✅ |
| `/api/v1/strategies/cache/refresh` | POST | ✅ | ✅ |

⚠️ **Security Gap Identified:** Internal API calls currently have NO authentication. Service-to-service auth must be added before production use.

---

## Business Value Justification

### Primary Value Proposition
**Automated adaptation to market conditions and profit maximization** — The LLM (as CIO) becomes the autonomous owner of all trading decisions, capable of:
1. Monitoring strategy performance in real-time
2. Adjusting strategy parameters dynamically based on market conditions
3. Optimizing for profit without human intervention

### Target User
**The LLM itself** — The CIO agent owns the entire trading operation end-to-end.

### Use Cases

| Use Case | Description |
|----------|-------------|
| **Dynamic Parameter Adjustment** | LLM detects poor strategy performance and adjusts RSI thresholds, spread widths, sensitivity params in real-time |
| **Market Regime Adaptation** | When volatility spikes, LLM automatically reduces position sizes or disables aggressive strategies |
| **Profit Optimization** | LLM experiments with parameters to maximize returns, backed by full audit trail |
| **Emergency Response** | LLM can rapidly respond to market events (flash crashes, news) by adjusting strategy behavior |

### ROI Justification
- **Time-to-response**: From hours/days (human intervention) to seconds (LLM autonomous)
- **Opportunity cost**: No more missed trades while waiting for manual config updates
- **Consistency**: 24/7 LLM monitoring vs human availability

### Comparison to Other Epics

| Epic | Relationship | Recommendation |
|------|--------------|----------------|
| Epic 3 (Semantic Guarding) | **Prerequisite** — provides market regime context for when to adjust | Do FIRST (Auto-detect via petrosa-data-manager + LLM override) |
| Epic 5 (Alerting) | Independent | Do in parallel or later |
| Epic 6 (Memory/Shadow ROI) | Enhances this — provides historical context for optimization | Do later |

---

## Approval Required

| Role | Name | Status | Date |
|------|------|--------|------|
| Technical Lead | | PENDING | |
| Fund Manager / Stakeholder | | PENDING | |
| Product Owner | | PENDING | |

> **Note:** This investigation document contains recommendations only. Implementation requires formal approval from Technical Lead and Stakeholder before work begins.

---

## Current Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         CURRENT DATA FLOW                                   │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│   ┌──────────────────────┐     ┌─────────────┐     ┌───────────────────┐  │
│   │   TA-Bot            │     │    NATS     │     │      CIO         │  │
│   │ (Signal Generator) │────▶│ intent.*    │────▶│ (Interception)   │  │
│   │ 28 strategies      │     │             │     │ Nurse/Guard      │  │
│   └──────────────────────┘     └─────────────┘     └────────┬────────┘  │
│                                                              │             │
│   ┌──────────────────────┐     ┌─────────────┐              │             │
│   │ Realtime Strategies  │────▶│ intent.*    │──────────────┘             │
│   │ 6 strategies        │     │             │                            │
│   └──────────────────────┘     └─────────────┘                            │
│                                    │                                       │
│                                    ▼                                       │
│                            ┌─────────────┐                                 │
│                            │ TradeEngine │                                 │
│                            │ (Execution) │                                 │
│                            └─────────────┘                                 │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## What CIO Currently Has

| Capability | Implementation | Status |
|------------|---------------|--------|
| Signal Interception | NATS `intent.>` → `signals.trading` | ✅ Working |
| Risk Limits | `RiskLimits` model via MCP (max_drawdown, position size) | ✅ Working |
| Execution Policy | `ExecutionPolicy` model (modes, manual approval) | ✅ Working |
| Shadow ROI Tracking | ROI Engine from Nurse audit logs | ✅ Working |
| Regime Guard | Market regime + drawdown checks | ✅ Working |
| LLM Reasoning | MCP tool for LLM-powered analysis | ✅ Working |
| Earnings Summary | MCP tool showing PnL/shadow ROI | ✅ Working |

---

## Gap Analysis

### What the CIO is MISSING

| Requirement | TA-bot Support | Realtime Strategies Support | CIO Access |
|-------------|---------------|----------------------------|------------|
| List all strategies | ✅ (28 strategies) | ✅ (6 strategies) | ❌ **NO** |
| Get strategy parameters | ✅ REST API | ✅ REST API | ❌ **NO** |
| Set strategy parameters | ✅ REST API | ✅ REST API | ❌ **NO** |
| Get strategy audit trail | ✅ Full history | ✅ Full history | ❌ **NO** |
| Get strategy performance | ❌ Not implemented | ❌ Not implemented | ❌ **N/A** |
| Rollback strategy config | ✅ Available | ✅ Available | ❌ **NO** |

---

## Target Services Capabilities

### petrosa-bot-ta-analysis

The TA-bot already has a comprehensive runtime configuration system:

- **Config Manager**: Full CRUD for 28 strategies
- **API Endpoints**:
  - `GET /api/v1/strategies` - List all strategies
  - `GET /api/v1/strategies/{id}/config` - Get config
  - `POST /api/v1/strategies/{id}/config` - Update config
  - `GET /api/v1/strategies/{id}/audit` - Get audit trail
- **Audit Trail**: Full history with who/what/when/why
- **Parameter Schema**: Full validation for each strategy
- **Rollback**: Version-based rollback

### petrosa-realtime-strategies

Similar capabilities for 6 realtime strategies:

- **Config Manager**: Full CRUD for 6 strategies
- **API Endpoints**: Same pattern as TA-bot
- **Audit Trail**: Full history
- **Parameter Schema**: Full validation
- **Rollback**: Version-based rollback

---

## Required Changes

### 1. Add Service Discovery to CIO

Environment variables needed:
```bash
TA_BOT_API_URL=http://petrosa-ta-bot:8080
REALTIME_STRATEGIES_API_URL=http://petrosa-realtime-strategies:8080
```

### 2. Extend CIO MCP Server

New MCP tools needed in `apps/strategist/mcp_server.py`:

```python
# Example tool definitions
{
    "name": "list_ta_bot_strategies",
    "description": "List all TA-bot strategies with configuration status",
    "input_schema": {...}
},
{
    "name": "get_ta_bot_strategy_config",
    "description": "Get configuration for a specific TA-bot strategy",
    "input_schema": {...}
},
{
    "name": "set_ta_bot_strategy_config",
    "description": "Update configuration for a TA-bot strategy",
    "input_schema": {...}
},
{
    "name": "get_ta_bot_strategy_audit",
    "description": "Get audit trail for a TA-bot strategy",
    "input_schema": {...}
},
# Same for realtime-strategies
```

### 3. Implementation Architecture Options

#### Option A: HTTP API Calls (Recommended for MVP)
- CIO makes HTTP calls to TA-bot/realtime-strategies APIs
- Pros: Simple, decoupled, each service maintains its own config
- Cons: Adds latency, needs service discovery

#### Option B: Shared MongoDB
- CIO reads/writes same MongoDB collections as TA-bot/realtime-strategies
- Pros: Faster, no new service dependencies
- Cons: Tight coupling, potential conflicts, shared schema responsibility

#### Option C: NATS-Based Commands
- Add new NATS subjects for config management
- Pros: Native to Petrosa ecosystem
- Cons: More complex, requires new protocol

---

## Decision: Integration Architecture

**Selected: Option A - HTTP API Calls**

### Confirmed K8s Service Names

| Service | K8s Service Name | Port | Target Port | Full Internal URL |
|---------|-----------------|------|-------------|-------------------|
| TA-bot | `petrosa-ta-bot-service` | 80 | 8000 | `http://petrosa-ta-bot-service.petrosa-apps.svc.cluster.local:80` |
| Realtime-strategies | `petrosa-realtime-strategies` | 80 | 8080 | `http://petrosa-realtime-strategies.petrosa-apps.svc.cluster.local:80` |

---

## Implementation Suggestions (Option A)

### 1. Add Environment Variables

**File:** `.env.example`

```bash
# Strategy Service URLs (internal K8s)
TA_BOT_API_URL=http://petrosa-ta-bot-service.petrosa-apps.svc.cluster.local:80
REALTIME_STRATEGIES_API_URL=http://petrosa-realtime-strategies.petrosa-apps.svc.cluster.local:80
```

### 2. Create HTTP Service Client

**Suggested File:** `core/service_clients.py`

```python
"""HTTP client for calling other Petrosa services."""

import httpx
from typing import Any


class StrategyServiceClient:
    """Generic HTTP client for strategy services."""

    def __init__(self, base_url: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self):
        self._client = httpx.AsyncClient(timeout=self.timeout)
        return self

    async def __aexit__(self, *args):
        if self._client:
            await self._client.aclose()

    async def get(self, path: str) -> dict[str, Any]:
        """GET request to service."""
        response = await self._client.get(f"{self.base_url}{path}")
        response.raise_for_status()
        return response.json()

    async def post(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        """POST request to service."""
        response = await self._client.post(f"{self.base_url}{path}", json=data)
        response.raise_for_status()
        return response.json()


class TaBotClient(StrategyServiceClient):
    """Client for TA-bot service."""

    async def list_strategies(self) -> list[dict]:
        result = await self.get("/api/v1/strategies")
        return result.get("data", [])

    async def get_config(self, strategy_id: str, symbol: str | None = None) -> dict:
        path = f"/api/v1/strategies/{strategy_id}/config"
        if symbol:
            path = f"/api/v1/strategies/{strategy_id}/config/{symbol}"
        return await self.get(path)

    async def set_config(
        self,
        strategy_id: str,
        parameters: dict,
        changed_by: str,
        reason: str | None = None,
        symbol: str | None = None,
    ) -> dict:
        path = f"/api/v1/strategies/{strategy_id}/config"
        if symbol:
            path = f"/api/v1/strategies/{strategy_id}/config/{symbol}"
        return await self.post(path, {
            "parameters": parameters,
            "changed_by": changed_by,
            "reason": reason,
        })

    async def get_audit(self, strategy_id: str, limit: int = 100) -> list[dict]:
        return await self.get(f"/api/v1/strategies/{strategy_id}/audit?limit={limit}")


class RealtimeStrategiesClient(StrategyServiceClient):
    """Client for realtime-strategies service."""

    # Same interface as TaBotClient
    async def list_strategies(self) -> list[dict]: ...
    async def get_config(self, strategy_id: str, symbol: str | None = None) -> dict: ...
    async def set_config(self, strategy_id: str, parameters: dict, changed_by: str, reason: str | None = None, symbol: str | None = None) -> dict: ...
    async def get_audit(self, strategy_id: str, limit: int = 100) -> list[dict]: ...
```

### 3. Extend MCP Server with New Tools

**File:** `apps/strategist/mcp_server.py`

**New tools to add:**

| Tool Name | Description | Parameters |
|-----------|-------------|------------|
| `list_ta_bot_strategies` | List all TA-bot strategies | none |
| `get_ta_bot_strategy_config` | Get config for a strategy | `strategy_id`, `symbol?` |
| `set_ta_bot_strategy_config` | Update strategy config | `strategy_id`, `parameters`, `changed_by`, `reason?`, `symbol?` |
| `get_ta_bot_strategy_audit` | Get audit trail | `strategy_id`, `limit?` |
| `list_realtime_strategies` | List all realtime strategies | none |
| `get_realtime_strategy_config` | Get config for a strategy | `strategy_id`, `symbol?` |
| `set_realtime_strategy_config` | Update strategy config | `strategy_id`, `parameters`, `changed_by`, `reason?`, `symbol?` |
| `get_realtime_strategy_audit` | Get audit trail | `strategy_id`, `limit?` |

**Tool definitions example:**

```python
# In tool definitions list
{
    "name": "list_ta_bot_strategies",
    "description": "List all available TA-bot trading strategies with their configuration status. "
                   "Use this to discover strategies before modifying their configurations.",
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": []
    }
},
{
    "name": "get_ta_bot_strategy_config",
    "description": "Get current configuration for a specific TA-bot strategy. "
                   "Returns global config or symbol-specific override if provided.",
    "input_schema": {
        "type": "object",
        "properties": {
            "strategy_id": {
                "type": "string",
                "description": "Strategy identifier (e.g., 'rsi_extreme_reversal')"
            },
            "symbol": {
                "type": "string",
                "description": "Optional trading symbol (e.g., 'BTCUSDT') for symbol-specific config"
            }
        },
        "required": ["strategy_id"]
    }
},
{
    "name": "set_ta_bot_strategy_config",
    "description": "Update configuration parameters for a TA-bot strategy. "
                   "IMPORTANT: Include a thought_trace explaining why this change is safe.",
    "input_schema": {
        "type": "object",
        "properties": {
            "strategy_id": {"type": "string"},
            "parameters": {"type": "object", "description": "Key-value pairs of parameters to update"},
            "changed_by": {"type": "string", "description": "Who is making the change"},
            "reason": {"type": "string", "description": "Reason for the change"},
            "symbol": {"type": "string", "description": "Optional symbol for symbol-specific config"}
        },
        "required": ["strategy_id", "parameters", "changed_by"]
    }
},
# ... repeat pattern for realtime-strategies
```

### 4. Integration Points

**In MCP server initialization:**

```python
# Add near top of mcp_server.py
ta_bot_client: TaBotClient | None = None
realtime_strategies_client: RealtimeStrategiesClient | None = None


async def init_service_clients():
    """Initialize HTTP clients for strategy services."""
    global ta_bot_client, realtime_strategies_client

    ta_bot_url = os.getenv("TA_BOT_API_URL")
    if ta_bot_url:
        ta_bot_client = TaBotClient(ta_bot_url)

    realtime_url = os.getenv("REALTIME_STRATEGIES_API_URL")
    if realtime_url:
        realtime_strategies_client = RealtimeStrategiesClient(realtime_url)
```

### 5. Test Coverage

**Suggested test file:** `tests/test_service_clients.py`

- Mock HTTP responses for each endpoint
- Test error handling (connection errors, 4xx/5xx responses)
- Test parameter validation

---

## Risk Assessment

### Phase 2 (LLM-Assisted) Risks

| Risk | Level | Mitigation |
|------|-------|------------|
| Security | **HIGH** | Service-to-service authentication **REQUIRED** - Currently NO auth on internal APIs |
| Complexity | Medium | Add HTTP client to CIO |
| Reliability | Low | APIs already exist |
| Performance | Low | MCP calls already async |
| Integration | Medium | Epic 3 coupling underspecified — requires integration design |

### Phase 3 (Fully Autonomous) Risks — REQUIRES SEPARATE INVESTIGATION

> ⚠️ **These risks must be addressed before any move toward Phase 3.**

| Risk | Description | Severity |
|------|-------------|----------|
| **LLM Hallucination** | LLM makes incorrect parameter decisions based on fabricated "data" | CRITICAL |
| **Model Drift** | LLM performance degrades under novel market conditions it hasn't seen | HIGH |
| **Prompt Injection** | Adversarial inputs in market data feeds could manipulate LLM decisions | HIGH |
| **Feedback Loop** | LLM actions change market data it reads, creating self-reinforcing errors | HIGH |
| **Flash Crash** | Rapid, automated decisions during market volatility amplify losses | CRITICAL |
| **Catastrophic Loss** | Unbounded parameter adjustments could result in total capital loss | CRITICAL |
| **Regulatory** | Fully autonomous trading may trigger regulatory requirements | MEDIUM |

**Required before Phase 3:**
- [ ] Dedicated risk investigation document
- [ ] Circuit breaker design (automatic trading halt triggers)
- [ ] Maximum loss thresholds with automatic shutdown
- [ ] LLM decision audit and human review process
- [ ] Regulatory compliance assessment
- [ ] Adversarial testing for prompt injection
- [ ] Chaos testing for feedback loop scenarios

---

## Next Steps (Implementation Plan)

### Prerequisites (Must Complete First)
- [ ] Get formal approval from Technical Lead and Stakeholder
- [ ] Add service-to-service authentication to TA-bot and realtime-strategies APIs
- [ ] Add env vars to K8s deployments

### Implementation Stories (After Approval)

| Story ID | Title | Points | Priority | Status | Notes |
|----------|-------|--------|----------|--------|-------|
| S1 | Add service URL env vars to CIO | 1 | P2 | ⬜ | |
| S2 | Implement StrategyServiceClient base class | 2 | P2 | ⬜ | |
| S3 | Implement TaBotClient with all endpoints | 3 | P2 | ⬜ | |
| S4 | Implement RealtimeStrategiesClient | 3 | P2 | ⬜ | |
| S5 | Add list_ta_bot_strategies MCP tool | 2 | P2 | ⬜ | |
| S6 | Add get/set_ta_bot_strategy_config MCP tools | 3 | P2 | ⬜ | |
| S7 | Add get_ta_bot_strategy_audit MCP tool | 2 | P2 | ⬜ | |
| S8 | Add list_realtime_strategies MCP tool | 2 | P2 | ⬜ | |
| S9 | Add get/set_realtime_strategy_config MCP tools | 3 | P2 | ⬜ | |
| S10 | Add get_realtime_strategy_audit MCP tool | 2 | P2 | ⬜ | |
| S11 | Add unit tests for HTTP clients | 3 | P2 | ⬜ | |
| S12 | Integration test MCP → Service API | 5 | P3 | ⬜ | **May surface issues rippling to S2-S10** |
| S13 | Update K8s configs with env vars | 2 | P2 | ⬜ | |

> ⚠️ **Estimate Assumptions:**
> - Clean implementation of HTTP clients and MCP tooling
> - Service discovery via K8s works reliably
> - Integration testing complexity may reveal hidden issues
> - K8s config changes across environments not included
> - Error handling edge cases may require additional work
> - Security (auth) deferred to post-launch (NOT included in points)

**Optimistic estimate: ~33 points (~2 sprints)**
**Realistic estimate: 40-50 points (~3 sprints)** — buffer for unknowns recommended

---

### Test Strategy

| Test Type | Coverage | Notes |
|-----------|----------|-------|
| Unit | HTTP client methods | Mock httpx responses |
| Integration | MCP tool → Service API | Requires running services |
| Error handling | Service unavailable, invalid params, auth failures | Critical for production |
| Performance | Latency under load | Target: <500ms per call |

---

## Intelligence Framework Requirements

> ⚠️ **CRITICAL PREREQUISITE WARNING**
> This section identifies requirements that MUST be resolved BEFORE or DURING implementation, not after. Approving implementation without defining the reasoning framework, system prompt, and decision loop means building infrastructure for a "brain that doesn't exist yet."

### 1. Integration Design: CIO ↔ Epic 3 (Semantic Guarding)

**The coupling is currently underspecified.** "Epic 3 must be completed FIRST" is not an integration design.

**Required for integration:**
- [ ] Define exact data flow: Regime detection → Redis → Nurse → LLM context
- [ ] Define how LLM receives regime information (MCP tool? System prompt context? Both?)
- [ ] Define latency requirements for regime detection → LLM decision
- [ ] Define fallback behavior if regime data is unavailable

### 2. Future Discussion Topics (TBD - Must Resolve Before Phase 2 Full Rollout)

> ⚠️ **NOTE:** The following topics require dedicated discussion sessions and are out of scope for this investigation.

| Topic | Description | Priority | Status |
|-------|-------------|----------|--------|
| **Personas** | Multiple LLM personas for different market positions (bull, bear, sideways, high-vol) | High | ⬜ Pending Discussion |
| **Reasoning Frameworks** | ReAct, Chain-of-Thought, or custom frameworks for decision making | High | ⬜ Pending Discussion |
| **Learning System** | How the LLM learns from its own decisions over time | Medium | ⬜ Pending Discussion |
| **Goal Setting** | Defining profit targets, risk tolerance, and success metrics | Medium | ⬜ Pending Discussion |
| **System Prompt** | Full prompt engineering for autonomous trader persona | High | ⬜ Pending Discussion |
| **Decision Loop** | Define exact flow from market data → decision → execution | High | ⬜ Pending Discussion |

---

### 2. Decision Constraints & Hard Limits

**Required for MVP:**

| Constraint | Value | Type | Notes |
|------------|-------|------|-------|
| Max orders per symbol | 10 | Hard | Per trading session |
| Max orders global | 100 | Hard | Total across all symbols |
| Max position size per strategy | Configurable | Soft | Can be adjusted by LLM with reason |
| Max drawdown global | 5% | Hard | Stop trading if exceeded |
| Emergency stop | Manual trigger | Hard | Nuclear option |

**Prioritization Logic:**

> ⚠️ **This formula is a placeholder requiring significant development.** The variables (expected_return, probability) must be sourced from somewhere — the LLM's own estimation? A separate forecasting model? Historical backtests? Without a concrete answer, this creates false confidence.

```python
# Concept (REQUIRES DEFINITION)
priority_score = (expected_return * probability) / (risk * exposure)

# Questions to resolve:
# - Where does expected_return come from?
# - Where does probability come from?
# - Is this LLM-estimated or model-derived?
# - How often is this recalculated?
# - What historical data backs this up?
```

**Lower score = lower priority** — High-risk, low-probability trades get deprioritized.

---

### 3. Fee & Spread Calculation

**Required Capability:**

The LLM must be able to calculate:
- Expected fees per trade (maker/taker rates)
- Spread costs for entry/exit
- Net profit after fees
- Fee impact on position sizing

**MCP Tool Proposal:**
```python
{
    "name": "calculate_trade_costs",
    "description": "Calculate expected fees and spread costs for a trade",
    "input_schema": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "quantity": {"type": "number"},
            "entry_price": {"type": "number"},
            "exit_price": {"type": "number"},
            "maker_fee": {"type": "number", "default": 0.0002},
            "taker_fee": {"type": "number", "default": 0.0004}
        },
        "required": ["symbol", "quantity", "entry_price"]
    }
}
```

---

### 4. Conversation & Reasoning Logging

**Purpose:** Full transparency, auditability, and continuous learning from decisions.

**Required Data Model:**

```json
{
  "conversation_id": "uuid",
  "timestamp": "2026-03-08T10:30:00Z",
  "turn_number": 1,
  "role": "assistant",
  "thought_trace": "Full reasoning text (min 100 chars)",
  "decision": "reduce_position_size",
  "parameters_changed": {
    "strategy_id": "rsi_extreme_reversal",
    "parameter": "position_size_multiplier",
    "old_value": 1.0,
    "new_value": 0.5
  },
  "context": {
    "market_regime": "breakout_phase",
    "volatility": "high",
    "confidence": 0.9
  },
  "outcome": null,  // Filled in after trade executes
  "result_metrics": null  // Filled in after trade closes
}
```

**Post-Trade Enrichment:**
- After trade closes, enrich the conversation log with actual PnL, fees paid, duration
- Enable "reasoning vs outcome" correlation analysis
- Support vector store for semantic retrieval of similar past decisions

---

### 5. Variables & Inputs Classification

**Input Categories:**

| Category | Examples | Source | Update Frequency |
|----------|----------|--------|------------------|
| **Market Data** | Price, volume, order book | Binance | Real-time |
| **Regime** | Volatility level, trend direction | petrosa-data-manager | Every 15 min |
| **Strategy Config** | Parameters, thresholds | TA-bot / Realtime APIs | On change |
| **Performance** | PnL, drawdown, win rate | TradeEngine / Nurse | Real-time |
| **Risk Limits** | Max position, max orders | Config | Static / Manual |
| **Fees** | Maker/taker rates | Static / API | Static |

**All inputs must be:**
- [ ] Enumerated in a schema
- [ ] Have clear data types and valid ranges
- [ ] Have defined update frequencies
- [ ] Have fallback values if unavailable

---

## Related Documentation

- [MCP Tools](MCP_TOOLS.md)
- [Architecture](ARCHITECTURE.md)
- [petrosa-bot-ta-analysis README](../petrosa-bot-ta-analysis/README.md)
- [petrosa-realtime-strategies README](../petrosa-realtime-strategies/README.md)
