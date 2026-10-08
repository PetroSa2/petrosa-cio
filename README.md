# Petrosa CIO

**Sovereign Gatekeeper and Strategy Controller for the Petrosa Fund**

The CIO (Chief Investment Officer) service acts as the central intelligence and risk management layer. It orchestrates strategy execution, monitors fund health (via the Nurse), and ensures all trading signals align with the fund's risk mandates.

---

## 🌐 Overview

Petrosa CIO is responsible for:
* **Strategy Orchestration**: Managing the lifecycle of trading strategies (Strategist).
* **Fund Health Monitoring**: Real-time risk and performance oversight (Nurse).
* **Signal Validation**: Ensuring all intents-to-trade are valid and authorized.
* **Drift Calibration**: Continuous shadow validation against market reality (Probe).
* **Sovereign Gatekeeping**: Final authority on all execution signals.

---

## 🏗️ Architecture

### Core Components

| Component | Purpose |
|-----------|---------|
| **Strategist** (`apps/strategist`) | High-level strategy execution and coordination. |
| **Nurse** (`apps/nurse`) | Real-time health monitoring, risk checks, and alerting. |
| **Probe** (`core/probe.py`) | Shadow validation probe for read-only Binance connectivity. |
| **Alerting** (`core/alerting`) | Centralized alerting and notification system. |
| **NATS** (`core/nats`) | High-performance event bus for internal communication. |
| **DB** (`core/db`) | Persistent storage for state and history. |

### Data Flow

```
Trade Engine / External Signals
  ↓ (Signal Intent)
Petrosa CIO (Strategist)
  ↓ (Risk Check / Validation)
Nurse (Health Verification)
  ↓ (Authorized Signal)
Execution Layer
```

---

## 📚 Documentation Structure

Core documentation:
- `README.md` - Project overview and quick start
- `docs/INDEX.md` - Documentation index
- `docs/CONTRACTS.md` - Service contracts
- `docs/HEARTBEAT_SPEC.md` - Heartbeat behavior
- `docs/EMERGENCY_OPERATIONS.md` - Operational procedures

---

## 🚀 Quick Start

### Prerequisites

* Python 3.11+
* Docker
* Gitleaks & Trivy (for full security scanning)

### Installation

```bash
# Complete setup
make setup

# Or manually
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

---

## 🧪 Development

### Code Quality

```bash
# Run linters
make lint

# Format code
make format

# Run tests
make test

# Security scan
make security
```

### Complete Pipeline

```bash
# Run all checks
make pipeline
```

---

## 📝 License

MIT License - Petrosa Systems

---

## 👥 Authors

Petrosa Systems - Trading Infrastructure Team
