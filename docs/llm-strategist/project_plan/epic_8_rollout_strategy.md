# Epic 8: Cross-Service Integration & Shadow Rollout Strategy

**Status:** Draft
**Owner:** John (Product Owner)
**Architect:** Winston

## 1. Architectural Pivot: Requesty Proxy Integration

The CIO must not connect directly to LLM providers. All traffic must route through the `Requesty` OpenAI-compatible proxy.

### Configuration Changes
*   **Env Variable:** Add `LLM_API_BASE` to configuration (e.g., `https://router.requesty.ai/v1`).
*   **Authentication:** The system will likely use a unified key.
    *   *Action:* Verify if `OPENAI_API_KEY` is the standard auth header for Requesty, or if a custom header is needed. For now, we assume standard OpenAI-style bearer tokens.
*   **LiteLLM Config:** Update `LiteLLMClient` to pass `api_base=os.getenv("LLM_API_BASE")` and `api_key=os.getenv("REQUESTY_API_KEY")` to `litellm.acompletion`.

## 2. Phased Rollout Plan

We cannot deploy the CIO in isolation. It relies on a synchronized ecosystem.

### Phase 1: Cross-Repo Alignment
Before CIO deployment, we must verify and update the surrounding services.

**Tasks:**
1.  **`petrosa-data-manager`:**
    *   *Audit:* Verify `/analysis/regime` API returns the exact JSON structure expected by `RegimeResult`.
    *   *Audit:* Verify `volatility_level` enum matches our `VolatilityLevel` definition.
2.  **`petrosa-tradeengine`:**
    *   *Audit:* Verify `/state` API returns `portfolio`, `risk_limits`, and `env_stats` keys.
    *   *Bridge:* Initial rollout will publish translated `Signal` objects to the legacy `signals.trading` topic to ensure compatibility without requiring immediate TradeEngine reconfiguration.
    *   *Contract:* Future phase will update TradeEngine to listen to `trade.execute.{strategy_id}` and accept the `DecisionResult` payload directly.
3.  **`petrosa-ta-bot` / `realtime-strategies`:**
    *   *Audit:* Verify `/strategy/{id}/config` returns `stats` and `defaults` matching our `StrategyStats` and `StrategyDefaults` models.
    *   *Contract:* Ensure they emit `trade.intent.*` NATS messages with the correct correlation ID headers.

### Phase 2: Shadow Mode (The "Muted Voice")
Deploy the CIO to production, but disable its ability to act.

**Mechanism:**
*   **Config:** Add `DRY_RUN=true` environment variable.
*   **OutputRouter Logic:**
    *   If `DRY_RUN` is true:
        *   Log `[SHADOW MODE] Would have published to {subject}: {payload}`.
        *   Increment metrics `cio_shadow_decisions_total`.
        *   **DO NOT** call `nats.publish`.
*   **Validation:**
    *   Let the CIO run for 24-48 hours.
    *   Compare `[SHADOW MODE]` logs against what the legacy logic (if any) did, or manually review the decisions for sanity.
    *   Monitor `cio_llm_latency_seconds` and `cio_llm_tokens_total` to verify budget assumptions.

### Phase 3: The "Soft" Switch (Canary)
Once Shadow Mode is proven safe:
1.  Set `DRY_RUN=false` for *one* low-risk strategy ID (e.g., via a `ENABLED_STRATEGIES` allowlist).
2.  Observe execution end-to-end.
3.  Gradually expand the allowlist.

## 3. Implementation Checklist (Amelia)

- [ ] Update `env.example` with `LLM_API_BASE`, `REQUESTY_API_KEY`, and `DRY_RUN`.
- [ ] Update `k8s/secrets.yaml` template with `REQUESTY_API_KEY`.
- [ ] Refactor `LiteLLMClient` to support the custom `api_base`.
- [ ] Implement `DRY_RUN` logic in `OutputRouter`.
- [ ] Create `tests/unit/test_shadow_mode.py` to verify the mute switch works.
