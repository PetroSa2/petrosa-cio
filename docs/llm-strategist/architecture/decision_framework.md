# Decision Framework & Code Engine

**Source:** `../investigations/003-cio-intelligence-framework.md` & `../investigations/004-llm-prompt-guide-reference.md`

## 1. The Code Engine

The Code Engine is a deterministic Python module that handles all quantitative calculations, hard limit enforcement, and financial mathematics. It runs on every trigger path (HOT, WARM, COLD) and provides the "ground truth" numbers to the LLM personas.

**Key Components:**

*   **Hard Limit Gate:** Checks `global_drawdown_pct`, `open_orders`, and `max_orders_per_symbol` against configured `RiskLimits`. If any limit is breached, returns a `hard_blocked=True` result immediately, bypassing all LLM calls.
*   **EV + Cost Calculator:** Computes `gross_ev`, `fees`, `slippage`, `funding_cost`, and `net_ev`. Enforces minimum EV thresholds (`EV_RATIO_THRESHOLD = 0.003`).
*   **Position Sizer:** Calculates position size using the Kelly Criterion (`KELLY_CAP = 0.25`) scaled by portfolio concentration factors.
*   **Trade Parameter Generator:** Determines `stop_loss`, `take_profit`, `leverage`, and `max_hold_hours` using lookup tables based on the current `VolatilityLevel` and `RegimeEnum`.

## 2. Decision Paths

The system routes triggers through one of three paths to optimize for latency and cost:

*   **HOT Path (< 200ms):**
    *   **Trigger:** Routine trade intent where regime and strategy assessments are cached and valid.
    *   **LLM Calls:** 0. Uses cached `RegimeResult` and `StrategyResult`.
    *   **Flow:** Trigger Gate -> Code Engine -> Decision Assembler (cached) -> Output Router.
*   **WARM Path (< 8s):**
    *   **Trigger:** Strategy degraded, regime changed, exposure threshold crossed, or cache miss on HOT path.
    *   **LLM Calls:** 1-3. Refreshes `RegimeResult` and/or `StrategyResult`, then runs `ActionClassifier`.
    *   **Flow:** Trigger Gate -> Code Engine -> Regime/Strategy Assessor -> Action Classifier -> Decision Assembler -> Output Router.
*   **COLD Path (< 30s):**
    *   **Trigger:** Scheduled review, parameter optimization, escalation.
    *   **LLM Calls:** 3 + Knowledge Retrieval. Full analysis with historical context injection.
    *   **Flow:** Same as WARM + Knowledge Retrieval (Vector DB) -> Action Classifier with history.

## 3. Reasoning Loop

The core reasoning loop orchestrates the interaction between the Code Engine and the LLM Personas.

```python
# Pseudocode logic flow

def reasoning_loop(trigger):
    context = build_context(trigger)

    # Step 1: Code Engine - Hard Limits & Calculations
    code_result = CodeEngine.run(context)
    if code_result.hard_blocked:
        return Decision(action="block", reason=code_result.hard_block_reason)

    # Step 2: LLM - Regime Classification (Cached)
    regime_result = RegimeClassifier.run(context)
    if regime_result.confidence < CONFIDENCE_THRESHOLDS["medium"]:
         # Low confidence -> skip trade
        return Decision(action="skip", reason="regime uncertain")

    # Step 3: LLM - Strategy Assessment (Cached)
    strategy_result = StrategyAssessor.run(context, regime_result)

    # Step 4: LLM - Action Classification
    # Synthesizes Code Engine numbers + Regime/Strategy qualitative flags
    action_result = ActionClassifier.run(code_result, regime_result, strategy_result)

    # Step 5: Assembly & output
    decision = DecisionAssembler.assemble(code_result, regime_result, strategy_result, action_result)
    log_decision(decision)
    return decision
```

## 4. Multiplier Reference Tables

**Stop Loss Volatility Multipliers:**

| Volatility Level | SL Multiplier |
|---|---|
| low | 1.0× |
| medium | 1.2× |
| high | 1.5× |
| extreme | 2.0× |

**Take Profit Regime Multipliers:**

| Regime | TP Multiplier |
|---|---|
| trending_bull | 1.3× |
| trending_bear | 1.3× |
| breakout_phase | 1.5× |
| ranging | 0.8× |
| choppy | 0.6× |
| high_volatility | 0.7× |
| capitulation | 0.6× |
| recovery | 1.0× |
