# Petrosa CIO Contracts

## Operational Contracts and Resilience

### Audit Timeout (Nurse Enforcer)
The `NurseEnforcer` acts as a safety guard around the core reasoning loop (`Orchestrator`). To accommodate the inherent latency of external LLM API calls and cluster-internal data retrieval, the enforcer imposes a maximum timeout of **10.0 seconds** per audit. 
If the reasoning loop fails to complete within this timeframe, the enforcer will automatically fall back to a safe `RETRY_SAFE` state to prevent hanging the event loop.

### Data Fetching Resilience (Context Builder)
The `ContextBuilder` gathers real-time state from the Data Manager and Trade Engine to inform the LLM's decisions. To account for potential latency spikes during high-volume periods, the HTTP client used for these internal API calls is configured with a **30.0-second** timeout.

### Regime-Based Hard Blocks (Code Engine)
The `CodeEngine` enforces strict safety rules based on the current market regime. 
*   **Contract:** A trade signal will be hard-blocked if the current market regime is listed in `REGIME_HARD_BLOCKS` (e.g., 'choppy') **AND** the confidence in that regime assessment is anything other than `low`.
*   **Resilience:** If the regime assessment has `low` confidence (often the result of a temporary failure to fetch regime data from the Data Manager), the engine will **bypass** the hard block. This ensures that temporary network glitches or service slowdowns do not permanently halt all trading activity.

## NATS Listening Contract
The CIO service is designed to audit intents before they are executed. It listens for incoming messages on the subject defined by the `NATS_TOPIC_INTENTS` environment variable. To actively shadow organic trades, this should be aligned with the Trade Engine's subscription topic (e.g., `signals.trading.*`).