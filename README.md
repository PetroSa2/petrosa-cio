# Petrosa CIO (Centralized Orchestrator)

**Centralized Orchestrator & Sovereign Gatekeeper for the Petrosa Fund**

The CIO service acts as the "Brain" and "Enforcer" for the entire Petrosa ecosystem. It provides high-stakes governance through a dual-layered architecture: the **Nurse** (a sub-50ms reactive safety gate) and the **Strategist** (an asynchronous LLM-driven policy generator). 

---

## 🌐 PETROSA ECOSYSTEM OVERVIEW

Maintaining consistency across the fund's distributed infrastructure.

### Services in the Ecosystem

| Service | Purpose | Input | Output | Status |
|---------|---------|-------|--------|--------|
| **petrosa-cio** | Centralized orchestrator & gatekeeper | NATS: `intent.>` | NATS: `signals.trading` | **YOU ARE HERE** |
| **petrosa-socket-client** | Real-time WebSocket data ingestion | Binance WebSocket API | NATS: `binance.websocket.data` | Real-time Processing |
| **petrosa-binance-data-extractor** | Historical data extraction & gap filling | Binance REST API | MySQL (klines, funding rates, trades) | Batch Processing |
| **petrosa-bot-ta-analysis** | Technical analysis (28 strategies) | Data Manager API | NATS: `signals.trading` | Signal Generation |
| **petrosa-realtime-strategies** | Real-time signal generation | NATS: `binance.websocket.data` | NATS: `signals.trading` | Live Processing |
| **petrosa-tradeengine** | Order execution & trade management | NATS: `signals.trading` | Binance Orders API, MongoDB audit | Order Execution |
| **petrosa-data-manager** | Data integrity and analytics hub | Multi-source | Unified Data API | Data Hub |

### Data Flow Pipeline (Interception Pattern)

```text
┌──────────────────┐      ┌──────────────────┐
│   Strategies     │      │   TA Bot         │
│ (Real-time/Live) │      │ (Batch Signals)  │
└────┬─────────────┘      └────┬─────────────┘
     │                         │
     │   NATS: intent.trading.*│
     └─────────────┬───────────┘
                   │
                   ▼
┌──────────────────────────────────────────────┐
│                petrosa-cio                   │
│   (THIS SERVICE - THE GATEKEEPER)            │
│                                              │
│  ┌──────────────────┐    ┌────────────────┐  │
│  │      NURSE       │    │   STRATEGIST   │  │
│  │ (Safety Enforcer)│◄───┤ (AI Reasoning) │  │
│  │                  │    │                │  │
│  │ • Redis Policy   │    │ • LLM Policy   │  │
│  │ • Pydantic Gate  │    │ • Regime Sync  │  │
│  │ • Semantic Veto  │    │ • MCP Gateway  │  │
│  └────────┬─────────┘    └────────────────┘  │
└───────────┼──────────────────────────────────┘
            │
            │ Approved Signal (Promoted)
            ▼
┌──────────────────┐      ┌──────────────────┐
│   TradeEngine    │      │    Audit Log     │
│ (Order Execution)│      │  (MongoDB Atlas) │
└──────────────────┘      └──────────────────┘
```

---

## 🔧 CIO - DETAILED DOCUMENTATION

### 1. The Nurse (Enforcement Layer)
- **Latency**: < 50ms P95.
- **Goal**: Hard-stop any trade that violates fund policy or market regime.
- **Logic**: Subscribes to `intent.>`, validates against Pydantic models in Redis, and publishes to `signals.trading` if approved.

### 2. The Strategist (Reasoning Layer)
- **Goal**: Generate and mutate trading policies based on portfolio performance and market regimes.
- **Interface**: Exposes ecosystem-wide configuration schemas via **Model Context Protocol (MCP)** tools.
- **Governance**: Requires a `thought_trace` for all configuration updates.

### 3. Core Infrastructure
- **Redis (Async)**: High-speed policy storage.
- **NATS (Asyncio)**: Low-latency signal promotion.
- **MongoDB Atlas**: Immutable storage for "Traces" (Telemetry) and "Thoughts" (AI Reasoning).
- **Petrosa-Otel**: End-to-end trace propagation.

---

## 🚀 Quick Start

```bash
# Setup dependencies (Poetry v1.5+)
make setup

# Run the CIO service locally
make run

# Run security scan
make security

# Execute complete pipeline (Lint, Test, Security)
make pipeline
```

### Key Make Commands (v2.0)
- `make format`: Ruff formatting & imports.
- `make lint`: Strict type-checking & linting.
- `make test`: Pytest with 40% coverage threshold.
- `make build`: Multi-stage Docker build.

---

## 📚 Documentation Links
- [**Architecture Overview**](docs/ARCHITECTURE.md)
- [**The implementation Plan**](docs/IMPLEMENTATION_PLAN.md)
- [**Product Requirements (PRD)**](docs/PRD.md)
- [**Epic Breakdown**](docs/EPICS.md)
- [**CI/CD Pipeline**](docs/CI_CD_PIPELINE.md)
- [**Testing Standards**](docs/TESTING.md)

---

**Production Status:** 🔄 **BOOTSTRAPPING** - Active implementation of safety gates and MCP tools.