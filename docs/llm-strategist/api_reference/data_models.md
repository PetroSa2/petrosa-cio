# Data Models Reference

**Source:** `../investigations/004-llm-prompt-guide-reference.md` and `apps/strategist/models/`

## 1. Core Enums

**`apps/strategist/models/enums.py`**

*   **RegimeEnum:** `trending_bull`, `trending_bear`, `ranging`, `breakout_phase`, `high_volatility`, `capitulation`, `recovery`, `choppy`.
*   **DataManagerRegimeEnum:** `turbulent_illiquidity`, `stable_accumulation`, `breakout_phase`, `consolidation`, `bullish_acceleration`, `bearish_acceleration`, `balanced_market`, `transitional`, `unknown`.
*   **VolatilityLevel:** `low`, `medium`, `high`, `extreme` (Code Engine only).
*   **ConfidenceLevel:** `high`, `medium`, `low`.
*   **HealthStatus:** `healthy`, `degraded`, `failing`.
*   **RegimeFit:** `good`, `neutral`, `poor`.
*   **ActivationRecommendation:** `run`, `reduce`, `pause`.
*   **ActionType:** `execute`, `modify_params`, `skip`, `block`, `pause_strategy`, `escalate`.
*   **ParamChangeDirection:** `increase`, `decrease`.

## 2. Regime Models

**`apps/strategist/models/regime.py`**

### `RegimeAPIResponse`

*   `data`: `RegimeAPIData` (`regime`, `volatility_level`, `confidence`)
*   `metadata`: `RegimeAPIMetadata` (`timestamp`, `collection`)

### `RegimeResult`

*   `regime`: `RegimeEnum`
*   `regime_confidence`: `ConfidenceLevel`
*   `primary_signal`: `str`
*   `thought_trace`: `str`
*   **@classmethod `from_api_response`:** Converts API response to framework model, handling threshold logic (`>= 0.80` high, `>= 0.70` medium).

## 3. Strategy Models

**`apps/strategist/models/strategy.py`**

### `StrategyResult`

*   `health`: `HealthStatus`
*   `regime_fit`: `RegimeFit`
*   `activation_recommendation`: `ActivationRecommendation`
*   `param_change`: `Optional[ParamChangeSignal]` (`param`, `direction`, `reason`)
*   `thought_trace`: `str`

### `ParamChangeSignal`

*   `param`: `str`
*   `direction`: `ParamChangeDirection`
*   `reason`: `str`

### `AppliedParamChange`

*   `param`: `str`
*   `old_value`: `float`
*   `new_value`: `float`
*   `direction`: `ParamChangeDirection`
*   `reason`: `str`

## 4. Trigger Context

**`apps/strategist/models/context.py`**

### `TriggerContext`

*   `correlation_id`: `str` (required)
*   `source_subject`: `str` (required)
*   `trigger_type`: `TriggerType`
*   `trigger_payload`: `Dict`
*   `regime`: `RegimeResult`
*   `volatility_level`: `VolatilityLevel`
*   `strategy_id`: `str`
*   `strategy_stats`: `StrategyStats` (`win_rate`, `avg_win_usd`, `win_rate_delta`)
*   `strategy_defaults`: `Dict`
*   `risk_limits`: `RiskLimits` (`max_drawdown_pct`, `max_orders_global`)
*   `portfolio`: `PortfolioSummary` (`net_directional_exposure`, `same_asset_pct`)
*   `historical_context`: `Optional[str]` (COLD path only)

## 5. Code Engine Result (To Be Implemented)

**`apps/strategist/models/engine.py` (Planned)**

*   `hard_blocked`: `bool`
*   `hard_block_reason`: `Optional[str]`
*   `gross_ev_usd`: `Optional[float]`
*   `fee_cost_usd`: `Optional[float]`
*   `slippage_cost_usd`: `Optional[float]`
*   `net_ev_usd`: `Optional[float]`
*   `ev_passes`: `bool`
*   `cost_viable`: `bool`
*   `kelly_fraction`: `Optional[float]`
*   `computed_position_size_usd`: `Optional[float]`
*   `stop_loss_pct`: `Optional[float]`
*   `take_profit_pct`: `Optional[float]`
*   `leverage`: `float`
*   `order_type`: `OrderType`

## 6. Decision Result (To Be Implemented)

**`apps/strategist/models/decision.py` (Planned)**

*   `action`: `ActionType`
*   `justification`: `str`
*   `thought_trace`: `str`
*   `applied_param_change`: `Optional[AppliedParamChange]`
*   `outcome_metrics`: `Optional[Dict]` (Filled post-trade)
