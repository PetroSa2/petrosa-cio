# CIO Strategist: End-to-End Testing & Rollout Plan

This document defines the mandatory validation steps before the CIO Strategist is promoted to production.

## Phase 1: Local Simulation (The "Lab" Test)
**Goal:** Verify the full stack logic without external cluster dependencies.

### 1.1 Environment Setup
Create a `.env` file in the `petrosa-cio` root (based on `env.example`):
```env
ENVIRONMENT=development
LOG_LEVEL=DEBUG
LLM_PROVIDER=mock
VECTOR_PROVIDER=mock
DRY_RUN=true
NATS_URL=nats://localhost:4222
REDIS_URL=redis://localhost:6379
```

### 1.2 Infrastructure Mocking
1. **Run NATS/Redis:** Use Docker to provide the transport layer.
   ```bash
   docker run -d --name nats-test -p 4222:4222 nats:latest
   docker run -d --name redis-test -p 6379:6379 redis:latest
   ```
2. **Run API Mocks:** Since the `ContextBuilder` now makes real HTTP calls, use the `scripts/verify_live_heartbeat.py` script. It uses `unittest.mock` to simulate the Data-Manager, TradeEngine, and TA-Bot responses perfectly.

### 1.3 Behavioral Validation
Execute the Heartbeat Simulation:
```bash
PYTHONPATH=. python3 scripts/verify_live_heartbeat.py
```
**Success Criteria:**
- [ ] Log shows `Starting reasoning loop` with a unique `correlation_id`.
- [ ] Log shows `Final decision: execute`.
- [ ] Log shows `[SHADOW MODE] Would have published to signals.trading`.
- [ ] Log shows `[SHADOW MODE] Would have published to trade.execute.momentum_v1`.
- [ ] Log shows `Mock Vector Upsert` (Audit path verified).

---

## Phase 2: CI/CD Pipeline (The "Registry" Test)
**Goal:** Ensure the package is buildable and compliant with Petrosa standards.

1. **Local Pipeline Pass:**
   ```bash
   make pipeline
   ```
2. **Commit & Push:**
   ```bash
   git commit -m "feat(cio): implement T-Junction logic and production alignment"
   git push origin <branch>
   ```
3. **Registry Verification:**
   Ensure the Docker image builds successfully in the CI environment and is pushed to `yurisa2/petrosa-cio:latest`.

---

## Phase 3: Cluster Shadow Rollout (The "Observation" Phase)
**Goal:** Observe real market data without any execution risk via GitOps deployment.

### 3.1 Pipeline Trigger
Commit and push the staged changes in both `petrosa-cio` and `petrosa_k8s`. The automated CI/CD pipeline will:
1. Build and push the `petrosa-cio:latest` image.
2. Synchronize the shared ConfigMaps and Secrets.
3. Deploy the `petrosa-cio` pod to the `petrosa-apps` namespace.

### 3.2 Live Monitoring & Verification
1. **Log Audit:**
   ```bash
   kubectl logs -f deployment/petrosa-cio -n petrosa-apps
   ```
   Monitor for `[SHADOW MODE]` entries triggered by real strategy intents from `ta-bot`.
2. **Telemetry Audit:**
   Verify metrics are appearing in Prometheus/Grafana:
   - `cio_llm_latency_seconds_bucket`
   - `cio_decision_actions_total`
3. **Memory Audit:**
   Check Qdrant (via UI or CLI) to ensure the `cio_strategy_history` collection is being populated with decision traces.

---

## Phase 4: Gradual Promotion (The "Live" Test)
**Goal:** Transition from shadow mode to active execution.

1. **Canary Strategy:** Pick ONE low-risk strategy.
2. **Configuration Change:**
   Update `deployment.yaml` or a ConfigMap override to set `DRY_RUN=false` for that specific strategy (requires a future code update to support strategy-level dry-run, otherwise use global `DRY_RUN=false`).
3. **Verification:**
   Observe the TradeEngine logs to confirm it receives and executes the translated `Signal` from the CIO.
