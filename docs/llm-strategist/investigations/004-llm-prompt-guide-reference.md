# PETROSA Intelligence Framework

**Version:** 1.0
**Date:** 2026-03-08
**Classification:** Internal — Engineering Use Only
**Status:** Approved for Implementation

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Design Principles](#2-design-principles)
3. [Code vs LLM Decision Matrix](#3-code-vs-llm-decision-matrix)
4. [System Architecture](#4-system-architecture)
5. [Decision Paths](#5-decision-paths)
6. [The Code Engine](#6-the-code-engine)
7. [The Three LLM Calls](#7-the-three-llm-calls)
8. [Prompt Designs](#8-prompt-designs)
9. [Decision Assembly](#9-decision-assembly)
10. [Parsing & Validation](#10-parsing--validation)
11. [Trade Lifecycle](#11-trade-lifecycle)
12. [Learning System](#12-learning-system)
13. [Knowledge Base](#13-knowledge-base)
14. [Cost Model](#14-cost-model)
15. [Failure Modes & Mitigations](#15-failure-modes--mitigations)
16. [Prompt Library Structure](#16-prompt-library-structure)
17. [Appendix: Reference Tables](#17-appendix-reference-tables)

---

## 1. System Overview

PETROSA is an autonomous quantitative trading intelligence system running on Kubernetes. It manages 34 live trading strategies (28 in TA-bot, 6 in realtime-strategies) and makes parameter adjustment and trade execution decisions without human intervention.

The CIO (Chief Intelligence Officer) is the autonomous brain. It receives trade intents from strategies via NATS, intercepts them, and decides: execute, modify, skip, or block. It also monitors strategy health and proactively adjusts parameters when market conditions change.

```
strategies → NATS intent.* → CIO → TradeEngine
                              ↕
                      Strategy APIs (TA-bot, Realtime)
                              ↕
                      petrosa-data-manager
```

**What the CIO does today:**
- Intercepts trade signals
- Enforces risk limits
- Blocks trades that violate drawdown or order count limits

**What this framework adds:**
- Dynamic strategy parameter management
- Market regime awareness
- Autonomous parameter optimisation
- Full decision audit trail with outcome correlation

---

## 2. Design Principles

Four principles govern every design decision in this framework.

**Principle 1 — Code owns numbers, LLMs own language.**
Any output that is a number computed from other numbers is produced by a Python function. LLMs are called only when the input is natural language context (regime descriptions, strategy documentation, qualitative health signals) and the output is a classification. LLMs hallucinate floats. This is not a limitation to work around — it is a constraint to design for.

**Principle 2 — Maximum three LLM calls per decision.**
The entire decision pipeline runs on at most three LLM calls: Regime Classifier, Strategy Assessor, Action Classifier. Everything else is code. On the HOT path, this drops to zero LLM calls when cached assessments are valid.

**Principle 3 — Schemas must have four fields or fewer.**
Every LLM output schema has at most four top-level fields. Above this limit, small models begin omitting fields, inventing field names, or producing malformed JSON. Smaller schemas mean fewer parse failures and cheaper outputs.

**Principle 4 — Determinism through hard gates, not LLM judgment.**
All hard limits (drawdown, order counts, minimum EV) are enforced by code before any LLM is called. LLMs are never in a position to "decide" whether to override a safety limit. The code blocks the action before the LLM sees the request.

---

## 3. Code vs LLM Decision Matrix

Before any prompt is written, every decision must be classified. This table is the authoritative reference:

| Decision | Owner | Reason |
|---|---|---|
| Hard limit checks (drawdown, max orders) | **Code** | Binary, deterministic, safety-critical |
| Expected value calculation | **Code** | Arithmetic — LLMs hallucinate floats |
| Kelly fraction | **Code** | Formula with no ambiguity |
| Fee + slippage + funding cost | **Code** | Arithmetic |
| Position size | **Code** | Derived from Kelly × scale factors |
| Stop loss / take profit levels | **Code** | Strategy default × lookup multiplier |
| Capital utilisation ratio | **Code** | Division |
| Portfolio concentration check | **Code** | Sum and comparison |
| Trigger routing (HOT/WARM/COLD) | **Code** | Switch statement on trigger_type enum |
| Order type selection | **Code** | Rule: spread_pct < threshold → limit |
| Parameter value computation | **Code** | Direction signal × step size × schema bounds |
| Hard exit triggers | **Code** | Stop loss / take profit / time expiry comparisons |
| Market regime classification | **LLM** | Multi-signal pattern recognition |
| Strategy qualitative fit assessment | **LLM** | Natural language strategy docs + regime context |
| Action classification | **LLM** | Synthesising flags + qualitative assessments |
| Parameter change justification | **LLM** | Audit trace grounded in strategy docs |

The split is roughly 75% code, 25% LLM. Code handles all quantitative work. LLMs handle classification and language.

---

## 4. System Architecture

```
TRIGGER RECEIVED (NATS intent.* or scheduled)
              │
              ▼
┌─────────────────────────────────────────────────────────┐
│                   TRIGGER GATE (code)                   │
│                                                         │
│  1. Parse trigger_type and payload                      │
│  2. Run hard limit checks against RiskLimits config     │
│  3. If any hard limit breached → BLOCK, exit immediately│
│  4. Check TTL — if expired → discard, log, exit         │
│  5. Route to HOT / WARM / COLD path                     │
└────────────────────────┬────────────────────────────────┘
                         │
          ┌──────────────┼──────────────┐
          │              │              │
        HOT            WARM           COLD
       path            path           path
          │              │              │
          ▼              ▼              ▼
┌──────────────────────────────────────────────────────────┐
│                   CODE ENGINE (all paths)                │
│                                                          │
│  ┌─────────────────┐  ┌─────────────────────────────┐   │
│  │ Hard Limit Gate │  │ EV + Cost Calculator        │   │
│  │ (double-check)  │  │ gross_ev, fees, slippage,   │   │
│  │                 │  │ funding, net_ev, ev_ratio    │   │
│  └─────────────────┘  └─────────────────────────────┘   │
│                                                          │
│  ┌─────────────────┐  ┌─────────────────────────────┐   │
│  │ Position Sizer  │  │ Trade Param Generator       │   │
│  │ Kelly × capital │  │ stop_loss, take_profit,     │   │
│  │ × portfolio_    │  │ leverage, max_hold_hours     │   │
│  │ scale_factor    │  │ (lookup tables, no LLM)     │   │
│  └─────────────────┘  └─────────────────────────────┘   │
│                                                          │
│  ┌─────────────────┐                                     │
│  │ Execution Calc  │                                     │
│  │ order_type,     │                                     │
│  │ entry_offset,   │                                     │
│  │ split_order     │                                     │
│  └─────────────────┘                                     │
└──────────────────────────────┬───────────────────────────┘
                               │  CodeEngineResult
                               ▼
┌──────────────────────────────────────────────────────────┐
│              LLM CALLS (conditional on path)             │
│                                                          │
│  ┌───────────────────────────────────────────────────┐   │
│  │  LLM CALL 1 — REGIME CLASSIFIER                   │   │
│  │  Haiku · max 300 input / 80 output tokens         │   │
│  │  Cache: 15 min per (asset_class, session)          │   │
│  │  Fires: WARM + COLD only (cached on HOT)           │   │
│  └───────────────────────────────────────────────────┘   │
│                                                          │
│  ┌───────────────────────────────────────────────────┐   │
│  │  LLM CALL 2 — STRATEGY ASSESSOR                   │   │
│  │  Haiku · max 500 input / 200 output tokens        │   │
│  │  Cache: per (strategy_id, regime) until audit      │   │
│  │          trail update or regime change             │   │
│  │  Fires: WARM + COLD; HOT uses cache or skips      │   │
│  └───────────────────────────────────────────────────┘   │
│                                                          │
│  ┌───────────────────────────────────────────────────┐   │
│  │  LLM CALL 3 — ACTION CLASSIFIER                   │   │
│  │  Haiku · max 400 input / 120 output tokens        │   │
│  │  Cache: none — unique DecisionResult per call     │   │
│  │  Fires: WARM + COLD only                          │   │
│  └───────────────────────────────────────────────────┘   │
└──────────────────────────────┬───────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────┐
│              DECISION ASSEMBLER (code)                   │
│                                                          │
│  Combines CodeEngineResult + LLM classification outputs  │
│  Applies activation_recommendation scale to position     │
│  Converts param direction signal to concrete value       │
│  Produces DecisionResult struct                          │
└──────────────────────────────┬───────────────────────────┘
                               │  DecisionResult
                               ▼
┌──────────────────────────────────────────────────────────┐
│              OUTPUT ROUTER (code)                        │
│                                                          │
│  action = execute       → NATS signals.* + audit log    │
│  action = modify_params → Strategy API + param freeze   │
│  action = skip          → audit log only                │
│  action = block         → alert + audit log             │
│  action = pause_strategy→ Strategy API disable + alert  │
│  action = escalate      → human alert queue             │
└──────────────────────────────┬───────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────┐
│              DECISION LOGGER (code)                      │
│                                                          │
│  Writes DecisionRecord to MongoDB (append-only)          │
│  Embeds record into Qdrant vector index                  │
│  Schedules outcome enrichment after trade close          │
└──────────────────────────────────────────────────────────┘
```

---

## 5. Decision Paths

### Path Classification (code, no LLM)

```python
def classify_path(trigger: Trigger, context: TriggerContext) -> str:
    if trigger.trigger_type == "trade_intent":
        if is_strategy_assessed_in_current_regime(trigger.strategy_id, context.regime):
            return "HOT"
        return "HOT_PLUS"  # One LLM call needed

    if trigger.trigger_type in ("strategy_degraded", "regime_changed", "exposure_threshold"):
        return "WARM"

    if trigger.trigger_type in ("scheduled_review", "parameter_optimization", "escalation"):
        return "COLD"

    return "WARM"  # Unknown triggers default to full analysis
```

### Path Specifications

| Path | Trigger | LLM Calls | Max Latency | Est. Cost |
|---|---|---|---|---|
| HOT | Routine trade intent (cached assessments valid) | 0 | 200ms | ~$0.000 |
| HOT+ | Trade intent, strategy not yet assessed in current regime | 1 (Strategy Assessor) | 2s | ~$0.0002 |
| WARM | Strategy degraded, regime changed, exposure threshold | 2–3 | 8s | ~$0.0004 |
| COLD | Scheduled review, parameter optimisation, escalation | 3 + knowledge fetch | 30s | ~$0.0005 |

### HOT Path Logic

On HOT path, the LLM calls are skipped entirely when both caches are valid. All safety is maintained because the Code Engine's hard limit checks run on every path regardless:

```
HOT PATH FLOW:
  1. Trigger Gate (code) — hard limits, routing
  2. Code Engine (code) — full calculation suite
  3. Cache check:
       if regime_cache.valid AND strategy_cache.valid:
           → Decision Assembler with cached LLM outputs
           → If all flags green: action = "execute" directly
           → If any flag amber: escalate to WARM
       else:
           → HOT+ path (fire Strategy Assessor, skip Regime if cached)
  4. Output Router
  5. Decision Logger
```

---

## 6. The Code Engine

The Code Engine is a single Python class that runs all quantitative calculations. It is the replacement for what would otherwise be five separate LLM personas. It runs in under 5ms and costs zero tokens.

### 6.1 CodeEngineResult

```python
@dataclass
class CodeEngineResult:
    # Hard gate flags
    hard_blocked: bool
    hard_block_reason: str | None

    # EV and cost
    gross_ev_usd: float | None
    fee_cost_usd: float | None
    slippage_cost_usd: float | None
    funding_cost_usd: float | None
    total_cost_usd: float | None
    net_ev_usd: float | None
    ev_ratio: float | None
    ev_passes: bool          # net_ev / position_size >= 0.003
    cost_viable: bool        # net_ev > total_cost * 1.5

    # Position sizing
    kelly_fraction: float | None
    portfolio_scale_factor: float       # 0.5 | 0.7 | 1.0
    computed_position_size_usd: float | None

    # Risk flags
    risk_warnings: list[str]
    drawdown_pct_of_max: float

    # Trade parameters (all from lookup tables)
    stop_loss_pct: float | None
    take_profit_pct: float | None
    leverage: float
    max_hold_hours: float | None

    # Execution
    order_type: str                     # "limit" | "market"
    split_order: bool
    entry_offset_pct: float
```

### 6.2 Hard Limit Checks

```python
def _check_hard_limits(self, ctx: TriggerContext, result: CodeEngineResult) -> CodeEngineResult:
    limits = ctx.risk_limits

    if ctx.global_drawdown_pct >= limits.max_drawdown_pct:
        result.hard_blocked = True
        result.hard_block_reason = (
            f"drawdown {ctx.global_drawdown_pct:.1%} >= limit {limits.max_drawdown_pct:.1%}"
        )
        return result

    if ctx.open_orders_global >= limits.max_orders_global:
        result.hard_blocked = True
        result.hard_block_reason = (
            f"open_orders {ctx.open_orders_global} >= limit {limits.max_orders_global}"
        )
        return result

    if ctx.open_orders_symbol >= limits.max_orders_per_symbol:
        result.hard_blocked = True
        result.hard_block_reason = (
            f"symbol_orders {ctx.open_orders_symbol} >= limit {limits.max_orders_per_symbol}"
        )
        return result

    result.hard_blocked = False
    result.drawdown_pct_of_max = ctx.global_drawdown_pct / limits.max_drawdown_pct
    if result.drawdown_pct_of_max > 0.7:
        result.risk_warnings.append(f"drawdown_at_{result.drawdown_pct_of_max:.0%}_of_max")

    return result
```

### 6.3 Cost Calculation

```python
SLIPPAGE_MULTIPLIERS = {
    "low": 1.0, "medium": 1.5, "high": 2.5, "extreme": 4.0
}

def _calculate_costs(self, ctx: TriggerContext, result: CodeEngineResult) -> CodeEngineResult:
    p = ctx.proposed_trade
    slippage_mult = SLIPPAGE_MULTIPLIERS[ctx.volatility_level]

    order_type = "limit" if ctx.urgency == "low" and p.spread_pct < 0.001 else "market"
    entry_fee = ctx.maker_fee if order_type == "limit" else ctx.taker_fee

    result.fee_cost_usd = p.position_size_usd * (entry_fee + ctx.taker_fee)
    result.slippage_cost_usd = (
        p.position_size_usd * p.spread_pct * 0.5 * slippage_mult
    )
    result.funding_cost_usd = (
        p.position_size_usd * ctx.funding_rate_8h * p.expected_hold_8h_periods
        if ctx.is_perpetual_futures else 0.0
    )
    result.total_cost_usd = (
        result.fee_cost_usd + result.slippage_cost_usd + result.funding_cost_usd
    )
    return result
```

### 6.4 EV Calculation

```python
EV_RATIO_THRESHOLD = 0.003   # 0.3% net expected return per unit of risk
COST_VIABILITY_RATIO = 1.5   # net_ev must exceed total_cost by this factor

def _calculate_ev(self, ctx: TriggerContext, result: CodeEngineResult) -> CodeEngineResult:
    s = ctx.strategy_stats
    if not (s.win_rate and s.avg_win_usd and s.avg_loss_usd):
        result.net_ev_usd = None
        result.ev_passes = False
        result.cost_viable = False
        return result

    result.gross_ev_usd = (
        (s.win_rate * s.avg_win_usd) - ((1 - s.win_rate) * s.avg_loss_usd)
    )
    result.net_ev_usd = result.gross_ev_usd - result.total_cost_usd
    result.ev_ratio = result.net_ev_usd / ctx.proposed_trade.position_size_usd
    result.ev_passes = result.ev_ratio >= EV_RATIO_THRESHOLD
    result.cost_viable = result.net_ev_usd > (result.total_cost_usd * COST_VIABILITY_RATIO)
    return result
```

### 6.5 Position Sizing

```python
KELLY_CAP = 0.25   # Never recommend more than 25% of bankroll

PORTFOLIO_SCALE_RULES = [
    # (net_exposure_threshold, concentration_threshold, scale_factor)
    (0.6, 0.4, 0.5),   # High concentration → 50% scale
    (0.4, 0.3, 0.7),   # Moderate concentration → 70% scale
    (0.0, 0.0, 1.0),   # Normal → full scale
]

def _calculate_position_size(self, ctx: TriggerContext, result: CodeEngineResult) -> CodeEngineResult:
    s = ctx.strategy_stats
    if not (s.win_rate and s.avg_win_usd and s.avg_loss_usd):
        result.kelly_fraction = None
        result.computed_position_size_usd = None
        return result

    raw_kelly = s.win_rate - ((1 - s.win_rate) / (s.avg_win_usd / s.avg_loss_usd))
    result.kelly_fraction = min(max(raw_kelly, 0.0), KELLY_CAP)

    net_exposure = ctx.portfolio.net_directional_exposure
    concentration = ctx.portfolio.same_asset_pct

    result.portfolio_scale_factor = 1.0
    for exp_thresh, conc_thresh, scale in PORTFOLIO_SCALE_RULES:
        if net_exposure > exp_thresh or concentration > conc_thresh:
            result.portfolio_scale_factor = scale
            break

    raw_size = result.kelly_fraction * ctx.available_capital_usd
    result.computed_position_size_usd = min(
        raw_size * result.portfolio_scale_factor,
        ctx.risk_limits.max_position_size_usd
    )
    return result
```

### 6.6 Trade Parameter Generation

```python
VOLATILITY_SL_MULTIPLIERS = {
    "low": 1.0, "medium": 1.2, "high": 1.5, "extreme": 2.0
}

REGIME_TP_MULTIPLIERS = {
    "trending_bull": 1.3, "trending_bear": 1.3, "breakout_phase": 1.5,
    "ranging": 0.8,       "choppy": 0.6,        "high_volatility": 0.7,
    "capitulation": 0.6,  "recovery": 1.0
}

REGIME_LEVERAGE_CAPS = {
    "trending_bull": 2.0, "trending_bear": 2.0, "breakout_phase": 1.5
    # All other regimes: cap at 1.0 (no leverage)
}

def _calculate_trade_params(self, ctx: TriggerContext, result: CodeEngineResult) -> CodeEngineResult:
    defaults = ctx.strategy_defaults
    vol = ctx.volatility_level
    regime = ctx.regime

    result.stop_loss_pct = defaults.stop_loss_pct * VOLATILITY_SL_MULTIPLIERS[vol]
    result.take_profit_pct = defaults.take_profit_pct * REGIME_TP_MULTIPLIERS.get(regime, 1.0)
    result.leverage = min(
        defaults.leverage,
        REGIME_LEVERAGE_CAPS.get(regime, 1.0)
    )
    result.max_hold_hours = (
        defaults.max_hold_hours / 2
        if vol in ("high", "extreme") else defaults.max_hold_hours
    )
    return result
```

---

## 7. The Three LLM Calls

The intelligence of the system lives in three focused LLM calls. Each is designed around what LLMs genuinely do better than code: reading multi-signal natural language context and producing a classification.

### Call 1 — Regime Classifier

**Purpose:** Classify the current market regime from a pre-compressed signal summary.
**Why LLM:** Regime classification requires weighing multiple conflicting signals and producing a judgment. A lookup table cannot handle the full signal space.
**Cache:** Output cached 15 minutes per `(asset_class, session_key)`. At 10k decisions/day this fires ~64 times/day — not 10k.
**Model:** Haiku
**Token budget:** 300 input / 80 output

### Call 2 — Strategy Assessor

**Purpose:** Assess strategy health and fit against the current regime using strategy documentation.
**Why LLM:** Strategy docs describe in natural language when a strategy works. Matching that description to a regime context is a language task, not a formula.
**Cache:** Per `(strategy_id, regime)` key. Invalidated on audit trail update or regime shift.
**Model:** Haiku
**Token budget:** 500 input / 200 output

### Call 3 — Action Classifier

**Purpose:** Classify the final action from the pre-computed `DecisionResult` struct.
**Why LLM:** The action step requires synthesising qualitative assessments with quantitative flags and producing an audit-grade justification for the decision log.
**Cache:** None — every `DecisionResult` is unique.
**Model:** Haiku
**Token budget:** 400 input / 120 output

---

## 8. Prompt Designs

### 8.1 Global Rules Block

Prepended to every LLM call. 5 rules, each one earns its place.

**`PETROSA_BASE_v1.0`**

```
You are a component of PETROSA, an autonomous trading system.

ABSOLUTE RULES:
1. Respond with ONLY a JSON object. No text before or after the JSON.
2. Never invent numbers. If a number is not in your input, output null.
3. All enum fields must exactly match the allowed values listed in your prompt.
4. If required input data is missing, output: {"error": "MISSING_INPUT"}
5. thought_trace: 1 sentence, max 120 characters, must reference a specific
   field name and value from your input.
```

> Rule 2 eliminates arithmetic hallucination at the prompt level.
> Rule 5 forces grounding — the model must name a field that exists in the input. A thought_trace that does not contain an input field name flags hallucination for the validator.

---

### 8.2 Regime Classifier

**`PETROSA_PROMPT_REGIME_v1.0`**

**System prompt:**

```
You are the REGIME CLASSIFIER for PETROSA.
Classify the market regime from the signal_summary provided.
Use ONLY the values in signal_summary. Do not use prior knowledge of asset prices.

VALID REGIME VALUES — output exactly one:
  trending_bull | trending_bear | ranging | breakout_phase |
  high_volatility | capitulation | recovery | choppy

CLASSIFICATION RULES (apply in order, stop at first match):
  1. volatility_percentile > 85 AND trend_strength < 0.60 → high_volatility
  2. trend_strength > 0.60 AND recent_higher_highs = true  → trending_bull
  3. trend_strength > 0.60 AND recent_lower_lows = true    → trending_bear
  4. price_action_character = "breakout"                   → breakout_phase
  5. price_action_character = "mean_reverting"             → ranging
  6. signals conflict or trend_strength < 0.30             → choppy

REGIME CONFIDENCE RULES:
  high:   2+ signals agree on the same regime
  medium: 1 primary signal, others neutral
  low:    signals conflict or primary signal ambiguous
  → When confidence = low, output regime = "choppy" as the safe fallback.
```

**Input format:**

```json
{
  "signal_summary": {
    "trend_strength": 0.28,
    "volatility_percentile": 91,
    "price_action_character": "erratic",
    "volume_relative_to_30d": 1.4,
    "recent_higher_highs": false,
    "recent_lower_lows": true
  }
}
```

**Output schema:**

```json
{
  "regime": "<enum>",
  "regime_confidence": "high | medium | low",
  "primary_signal": "<field_name_that_drove_classification>",
  "thought_trace": "<1 sentence, max 120 chars, must name a field and its value>"
}
```

**Example output:**

```json
{
  "regime": "high_volatility",
  "regime_confidence": "high",
  "primary_signal": "volatility_percentile",
  "thought_trace": "volatility_percentile 91 > 85 and trend_strength 0.28 < 0.60 — rule 1 fires"
}
```

> `regime_confidence` is a 3-value enum, never a float. Small models produce unreliable floats but reliably select from three named options when given concrete definitions.
> `primary_signal` must be a field name from the input. The validator rejects any value not in the input schema — exposing hallucination cheaply.

---

### 8.3 Strategy Assessor

**`PETROSA_PROMPT_STRATEGY_v1.0`**

**System prompt:**

```
You are the STRATEGY ASSESSOR for PETROSA.
Assess health and regime fit of the strategy from health_signals and strategy_doc.
Use ONLY these inputs. Do not use prior knowledge of trading strategies.

HEALTH CLASSIFICATION (apply in order, stop at first match):
  1. consecutive_losses >= 5   → health = "failing"
  2. win_rate_delta < -0.30    → health = "failing"
  3. win_rate_delta < -0.10    → health = "degraded"
  4. Otherwise                 → health = "healthy"

REGIME FIT CLASSIFICATION:
  Read the regime_fit_notes in strategy_doc for the current_regime.
  - Notes contain "avoid" or "poor"              → fit = "poor"
  - Notes contain "works well" or "strong"       → fit = "good"
  - No notes for current_regime, or notes neutral → fit = "neutral"

ACTIVATION RECOMMENDATION:
  health = "failing"                        → "pause"
  health = "degraded" AND fit = "poor"      → "pause"
  health = "degraded"                       → "reduce"
  health = "healthy"  AND fit = "poor"      → "reduce"
  Otherwise                                 → "run"

PARAM CHANGE RULE:
  Only set param_change if activation_recommendation IN ["reduce", "pause"].
  param_change.param must exist in strategy_doc.param_schema.
  param_change.direction must be "increase" or "decrease". Never output a value.
  param_change.reason must reference a specific health_signal field and its value.
  If no param change is warranted, output param_change: null.
```

**Input format:**

```json
{
  "strategy_doc": {
    "strategy_id": "rsi_extreme_reversal",
    "regime_fit_notes": {
      "high_volatility": "avoid — false signal rate increases significantly above vol_pct 80",
      "trending_bull": "works well — follow trend with tight stops"
    },
    "param_schema": {
      "position_size_multiplier": {"min": 0.1, "max": 2.0},
      "rsi_oversold_threshold": {"min": 15, "max": 40}
    },
    "current_param_values": {
      "position_size_multiplier": 1.0,
      "rsi_oversold_threshold": 30
    }
  },
  "health_signals": {
    "win_rate_delta": -0.18,
    "consecutive_losses": 2,
    "recent_pnl_trend": "negative"
  },
  "current_regime": "high_volatility"
}
```

**Output schema:**

```json
{
  "health": "healthy | degraded | failing",
  "regime_fit": "good | neutral | poor",
  "activation_recommendation": "run | reduce | pause",
  "param_change": null | {
    "param": "<param name from strategy_doc.param_schema>",
    "direction": "increase | decrease",
    "reason": "<max 80 chars, must reference a health_signal field>"
  },
  "thought_trace": "<1 sentence, max 120 chars, must name a field and its value>"
}
```

**Example output:**

```json
{
  "health": "degraded",
  "regime_fit": "poor",
  "activation_recommendation": "reduce",
  "param_change": {
    "param": "position_size_multiplier",
    "direction": "decrease",
    "reason": "win_rate_delta -0.18 in high_volatility regime flagged as avoid in strategy_doc"
  },
  "thought_trace": "win_rate_delta -0.18 triggers degraded; high_volatility regime_fit_notes say avoid"
}
```

> The LLM outputs `"decrease"` — never a new value. The Decision Assembler computes the actual value using `current_param_values`, `param_schema` bounds, and a fixed step size. This eliminates the most common small-LLM failure: outputting parameter values outside valid range.

---

### 8.4 Action Classifier

**`PETROSA_PROMPT_ACTION_v1.0`**

**System prompt:**

```
You are the ACTION CLASSIFIER for PETROSA.
You receive a pre-computed decision_result. All numbers have already been calculated.
Your job: apply the decision tree below, then write the justification.

DO NOT recompute numbers. All values you need are in decision_result.

DECISION TREE — apply rules in order, first match wins:

  RULE 1: decision_result.hard_blocked = true
          → action = "block"

  RULE 2: decision_result.ev_passes = false
          → action = "skip"

  RULE 3: decision_result.regime_confidence = "low"
          → action = "skip"

  RULE 4: decision_result.cost_viable = false
          → action = "skip"

  RULE 5: decision_result.activation_recommendation = "pause"
          → action = "pause_strategy"

  RULE 6: decision_result.activation_recommendation = "reduce"
          AND decision_result.param_change IS NOT null
          → action = "modify_params"

  RULE 7: decision_result.strategy_health = "healthy"
          AND decision_result.regime_fit IN ["good", "neutral"]
          AND decision_result.regime_confidence IN ["high", "medium"]
          → action = "execute"

  RULE 8: No rule matched above
          → action = "escalate"

JUSTIFICATION RULES:
  - 1 to 2 sentences maximum.
  - Must reference the rule number that fired and at least one field value.
  - Do not repeat the action word in the justification.
  - Maximum 200 characters.
```

**Input format:**

```json
{
  "decision_result": {
    "hard_blocked": false,
    "ev_passes": true,
    "net_ev_usd": 34.20,
    "cost_viable": true,
    "total_cost_usd": 12.40,
    "regime_confidence": "high",
    "regime_fit": "poor",
    "strategy_health": "degraded",
    "activation_recommendation": "reduce",
    "param_change": {
      "param": "position_size_multiplier",
      "direction": "decrease",
      "reason": "win_rate_delta -0.18 in avoid regime"
    },
    "computed_position_size_usd": 1840.00,
    "risk_warnings": ["drawdown_at_62pct_of_max"]
  }
}
```

**Output schema:**

```json
{
  "action": "execute | skip | block | modify_params | pause_strategy | escalate",
  "justification": "<1-2 sentences, max 200 chars, references rule number and a field value>",
  "thought_trace": "<1 sentence, max 120 chars, names the rule and field that determined action>"
}
```

**Example output:**

```json
{
  "action": "modify_params",
  "justification": "Rule 6 fired: activation_recommendation is reduce with param_change set. Position size multiplier will decrease due to win_rate_delta -0.18 in a poor-fit regime.",
  "thought_trace": "Rule 6: activation_recommendation=reduce and param_change is not null"
}
```

> The prompt encodes the decision tree as numbered rules with "first match wins" — the most important structural choice. Small LLMs follow explicit ordered rules reliably. They do not reliably perform holistic multi-factor synthesis. Asking "weigh these factors and decide" produces inconsistent outputs. Asking "walk this tree and report which branch fired" produces consistent outputs.

---

## 9. Decision Assembly

The Decision Assembler is pure code. It combines `CodeEngineResult` with the two LLM classification outputs and produces the `DecisionResult` struct that feeds the Action Classifier.

```python
def assemble_decision(
    code_result: CodeEngineResult,
    regime: RegimeResult | None,
    strategy: StrategyResult | None,
) -> DecisionResult:

    # Hard block is always terminal
    if code_result.hard_blocked:
        return DecisionResult(
            action="block",
            hard_blocked=True,
            justification=code_result.hard_block_reason
        )

    # Apply activation_recommendation scale to position size
    scale = {"run": 1.0, "reduce": 0.5, "pause": 0.0}.get(
        strategy.activation_recommendation if strategy else "run",
        1.0
    )
    final_position_size = (code_result.computed_position_size_usd or 0.0) * scale

    # Convert param direction signal to concrete value
    param_change = None
    if strategy and strategy.param_change:
        param_change = apply_param_direction(
            signal=strategy.param_change,
            current_values=strategy.current_param_values,
            param_schema=strategy.param_schema
        )

    return DecisionResult(
        hard_blocked=False,
        ev_passes=code_result.ev_passes,
        net_ev_usd=code_result.net_ev_usd,
        cost_viable=code_result.cost_viable,
        total_cost_usd=code_result.total_cost_usd,
        regime_confidence=regime.regime_confidence if regime else "low",
        regime_fit=strategy.regime_fit if strategy else "neutral",
        strategy_health=strategy.health if strategy else "healthy",
        activation_recommendation=strategy.activation_recommendation if strategy else "run",
        param_change=param_change,
        computed_position_size_usd=final_position_size,
        stop_loss_pct=code_result.stop_loss_pct,
        take_profit_pct=code_result.take_profit_pct,
        leverage=code_result.leverage,
        order_type=code_result.order_type,
        split_order=code_result.split_order,
        entry_offset_pct=code_result.entry_offset_pct,
        risk_warnings=code_result.risk_warnings,
    )


def apply_param_direction(
    signal: ParamChangeSignal,
    current_values: dict,
    param_schema: dict
) -> AppliedParamChange:
    """
    Converts LLM direction signal ("increase" | "decrease") to a concrete value.
    Uses a fixed step size of 20% of the valid range per adjustment event.
    Maximum change: 25% of current value regardless of step size.
    """
    schema = param_schema[signal.param]
    current = current_values[signal.param]
    valid_range = schema["max"] - schema["min"]
    step = valid_range * 0.20

    if signal.direction == "increase":
        candidate = current + step
    else:
        candidate = current - step

    # Clamp to schema bounds
    new_value = min(max(candidate, schema["min"]), schema["max"])

    # Safety: cap at 25% change from current value
    max_change = current * 0.25
    if abs(new_value - current) > max_change:
        new_value = current + max_change if signal.direction == "increase" else current - max_change
        new_value = min(max(new_value, schema["min"]), schema["max"])

    return AppliedParamChange(
        param=signal.param,
        old_value=current,
        new_value=round(new_value, 4),
        direction=signal.direction,
        reason=signal.reason
    )
```

---

## 10. Parsing & Validation

Every LLM response passes through a three-stage validator before use. Parse failures always default to the most conservative safe output — never to `execute`.

```python
ENUM_SCHEMAS = {
    "REGIME_CLASSIFIER": {
        "enums": {
            "regime": {
                "trending_bull", "trending_bear", "ranging", "breakout_phase",
                "high_volatility", "capitulation", "recovery", "choppy"
            },
            "regime_confidence": {"high", "medium", "low"},
        },
        "required": ["regime", "regime_confidence", "primary_signal", "thought_trace"],
    },
    "STRATEGY_ASSESSOR": {
        "enums": {
            "health": {"healthy", "degraded", "failing"},
            "regime_fit": {"good", "neutral", "poor"},
            "activation_recommendation": {"run", "reduce", "pause"},
        },
        "required": ["health", "regime_fit", "activation_recommendation", "thought_trace"],
    },
    "ACTION_CLASSIFIER": {
        "enums": {
            "action": {"execute", "skip", "block", "modify_params", "pause_strategy", "escalate"},
        },
        "required": ["action", "justification", "thought_trace"],
    },
}

SAFE_DEFAULTS = {
    "REGIME_CLASSIFIER": {
        "regime": "choppy",              # Most conservative — disables aggressive strategies
        "regime_confidence": "low",      # Triggers skip in assembler
        "primary_signal": "FALLBACK",
        "thought_trace": "PARSE_FAILURE",
    },
    "STRATEGY_ASSESSOR": {
        "health": "degraded",            # Conservative — triggers reduce
        "regime_fit": "neutral",
        "activation_recommendation": "reduce",
        "param_change": None,
        "thought_trace": "PARSE_FAILURE",
    },
    "ACTION_CLASSIFIER": {
        "action": "skip",                # Always safe on failure
        "justification": "Action skipped: classifier parse failure",
        "thought_trace": "PARSE_FAILURE",
    },
}


def parse_and_validate(raw: str, prompt_id: str) -> dict:
    schema = ENUM_SCHEMAS[prompt_id]

    # Stage 1: strict JSON parse
    parsed = None
    try:
        parsed = json.loads(raw.strip())
    except json.JSONDecodeError:
        # Stage 2: extract JSON substring (model added preamble)
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group())
            except json.JSONDecodeError:
                pass

    if parsed is None:
        log_parse_failure(prompt_id, "NO_JSON_FOUND", raw)
        return SAFE_DEFAULTS[prompt_id]

    # Stage 3: validate required fields present
    for field in schema["required"]:
        if field not in parsed:
            log_parse_failure(prompt_id, f"MISSING_FIELD_{field}", raw)
            return SAFE_DEFAULTS[prompt_id]

    # Stage 4: validate enum values
    for field, allowed in schema["enums"].items():
        if field in parsed and parsed[field] not in allowed:
            log_parse_failure(prompt_id, f"INVALID_ENUM_{field}:{parsed[field]}", raw)
            return SAFE_DEFAULTS[prompt_id]

    # Stage 5: validate thought_trace references an input field
    # (Catches hallucination — trace that names no input field is suspicious)
    trace = parsed.get("thought_trace", "")
    if trace and trace != "PARSE_FAILURE":
        known_input_fields = get_known_input_fields(prompt_id)
        if not any(field in trace for field in known_input_fields):
            log_grounding_warning(prompt_id, trace)
            # Do not fail — log for review but accept the output

    return parsed
```

### Context Compression — Pre-LLM Processing

All data is compressed before injection. LLMs never receive raw data.

| Data Source | Raw Size | Compressed Form | Saving |
|---|---|---|---|
| Audit trail (100 rows) | ~2,500 tokens | `"Last 20 trades: 14W 6L. WR 70%. Avg win $45, avg loss $28. Streak: +3"` | ~2,400 tokens |
| OHLCV history | ~3,500 tokens | `"price $43,210. 24h +2.1%. Vol 1.4× 30d avg. H/L $44,100/$42,800"` | ~3,400 tokens |
| Open positions (all) | ~1,500 tokens | `"4 open: 2 BTC long, 1 ETH long, 1 SOL short. Net: 62% long"` | ~1,450 tokens |
| Strategy full config | ~900 tokens | `"RSI_ER: degraded. win_rate_delta -18%. consec_losses 2"` | ~850 tokens |

**Total saving per warm-path decision: ~8,100 tokens** (~$0.002 at Haiku input rates).

---

## 11. Trade Lifecycle

### 11.1 Entry Decision Flow

```
Signal arrives on NATS intent.*
    → Trigger Gate (code): hard limits, routing, TTL check
    → Code Engine: full calculation suite
    → Regime Classifier (LLM, cached): regime + confidence
    → Strategy Assessor (LLM, cached): health + fit
    → Decision Assembler (code): produces DecisionResult
    → Action Classifier (LLM): action + justification
    → Output Router (code): executes action
    → Decision Logger (code): writes DecisionRecord
```

### 11.2 Hard Exit (Code Only — No LLM)

Hard exits fire when a price level or time threshold is crossed. These are evaluated continuously by a monitor loop, never by an LLM.

```python
def check_hard_exits(position: OpenPosition, market: MarketSnapshot) -> ExitSignal | None:

    current_pnl_pct = (market.current_price - position.entry_price) / position.entry_price
    if position.direction == "short":
        current_pnl_pct = -current_pnl_pct

    if current_pnl_pct <= -position.stop_loss_pct:
        return ExitSignal(exit_type="stop_loss", urgency="immediate")

    if current_pnl_pct >= position.take_profit_pct:
        return ExitSignal(exit_type="take_profit", urgency="immediate")

    hold_hours = (now() - position.open_time).total_seconds() / 3600
    if hold_hours >= position.max_hold_hours:
        return ExitSignal(exit_type="time_expiry", urgency="next_candle")

    return None  # No hard exit — soft exit evaluation proceeds
```

### 11.3 Soft Exit (LLM-Assisted)

Soft exits require qualitative judgment. They trigger when hard rules don't fire but conditions have deteriorated.

```
Soft exit trigger conditions:
  - hold_duration > 1.5 × avg_hold_hours for this strategy
  - regime changed to regime_fit = "poor" for this strategy
  - portfolio correlation spiked above threshold
  - strategy declared degraded mid-trade

On soft exit trigger:
  → Re-run Strategy Assessor with current health signals
  → Re-run Action Classifier with updated DecisionResult
  → If action = "skip" or "pause_strategy": exit position
  → If action = "execute" (continue): hold, log, re-evaluate in 15min
```

### 11.4 Strategy Parameter Adjustment

After `modify_params` action is confirmed by Action Classifier:

```python
async def apply_param_change(change: AppliedParamChange, strategy_id: str, service: str):

    # Post to Strategy API with audit fields
    payload = {
        "parameters": {change.param: change.new_value},
        "changed_by": "cio-agent",
        "reason": change.reason,
        "decision_id": current_decision_id(),
    }

    url = get_service_url(service)  # TA-bot or realtime-strategies
    response = await http_client.post(
        f"{url}/api/v1/strategies/{strategy_id}/config",
        json=payload
    )
    response.raise_for_status()

    # Enforce parameter freeze — no further changes to this strategy for 30 minutes
    param_freeze_cache.set(
        key=f"freeze:{strategy_id}",
        value={"changed_at": now(), "param": change.param},
        ttl_seconds=1800
    )
```

Parameter freeze prevents feedback loops where a change triggers a regime signal which triggers another change.

---

## 12. Learning System

### 12.1 DecisionRecord Schema

Every decision writes an append-only record to MongoDB and a vector embedding to Qdrant.

```json
{
  "id": "uuid-v4",
  "timestamp": "ISO8601",
  "trigger_type": "trade_intent | strategy_degraded | regime_changed | ...",
  "trigger_payload": {},
  "code_engine_result": {
    "ev_passes": true,
    "net_ev_usd": 34.20,
    "total_cost_usd": 12.40,
    "computed_position_size_usd": 1840.00,
    "stop_loss_pct": 0.018,
    "take_profit_pct": 0.032,
    "leverage": 1.0
  },
  "regime_result": {
    "regime": "high_volatility",
    "regime_confidence": "high",
    "primary_signal": "volatility_percentile"
  },
  "strategy_result": {
    "health": "degraded",
    "regime_fit": "poor",
    "activation_recommendation": "reduce",
    "param_change": {"param": "position_size_multiplier", "direction": "decrease"}
  },
  "action_result": {
    "action": "modify_params",
    "justification": "Rule 6 fired: activation_recommendation reduce with param_change set.",
    "thought_trace": "Rule 6: activation_recommendation=reduce and param_change is not null"
  },
  "applied_param_change": {
    "param": "position_size_multiplier",
    "old_value": 1.0,
    "new_value": 0.8,
    "direction": "decrease"
  },
  "outcome": null,
  "outcome_metrics": {
    "pnl_usd": null,
    "fees_paid_usd": null,
    "hold_duration_hours": null,
    "exit_reason": null,
    "regime_at_exit": null,
    "win_rate_48h_post_change": null
  },
  "enriched_at": null
}
```

### 12.2 Outcome Enrichment

After a trade closes, a background job fills `outcome_metrics` and re-indexes the record:

```python
async def enrich_decision_record(decision_id: str, trade_result: TradeResult):
    record = await mongo.decisions.find_one({"id": decision_id})

    record["outcome"] = "win" if trade_result.pnl_usd > 0 else "loss"
    record["outcome_metrics"] = {
        "pnl_usd": trade_result.pnl_usd,
        "fees_paid_usd": trade_result.fees_paid_usd,
        "hold_duration_hours": trade_result.hold_duration_hours,
        "exit_reason": trade_result.exit_reason,
        "regime_at_exit": trade_result.regime_at_exit,
    }
    record["enriched_at"] = now()

    await mongo.decisions.replace_one({"id": decision_id}, record)

    # Re-embed with outcome included — improves future retrieval relevance
    embedding = embed_decision_record(record)
    await qdrant.upsert(collection="decisions", points=[{
        "id": decision_id,
        "vector": embedding,
        "payload": {
            "strategy_id": record["trigger_payload"].get("strategy_id"),
            "regime": record["regime_result"]["regime"],
            "action": record["action_result"]["action"],
            "outcome": record["outcome"],
        }
    }])
```

For parameter changes, a 48-hour post-change job measures win rate recovery:

```python
async def evaluate_param_change_outcome(decision_id: str):
    record = await mongo.decisions.find_one({"id": decision_id})
    if not record.get("applied_param_change"):
        return

    strategy_id = record["trigger_payload"]["strategy_id"]
    change_time = record["timestamp"]
    window_end = change_time + timedelta(hours=48)

    trades_in_window = await mongo.trades.find({
        "strategy_id": strategy_id,
        "open_time": {"$gte": change_time, "$lte": window_end}
    }).to_list()

    if len(trades_in_window) >= 3:
        win_rate_post = sum(1 for t in trades_in_window if t["pnl"] > 0) / len(trades_in_window)
        record["outcome_metrics"]["win_rate_48h_post_change"] = win_rate_post
        await mongo.decisions.replace_one({"id": decision_id}, record)
```

### 12.3 Learning Loops

**Loop 1 — Retrieval-Based (zero cost, immediate)**
On COLD path, the 5 most similar past `DecisionRecord` objects are retrieved from Qdrant by cosine similarity to the current `TriggerContext` embedding. These are summarised into a `historical_context` block injected into the Action Classifier input. The system learns by analogy — past decisions with similar context and bad outcomes are visible to the classifier.

```python
async def retrieve_similar_decisions(context: TriggerContext, top_k: int = 5) -> str:
    query_embedding = embed_trigger_context(context)
    results = await qdrant.search(
        collection="decisions",
        query_vector=query_embedding,
        limit=top_k,
        query_filter=Filter(
            must=[FieldCondition(key="regime", match=MatchValue(value=context.regime))]
        )
    )

    summaries = []
    for r in results:
        p = r.payload
        summaries.append(
            f"Past {p['action']} in {p['regime']}: {p['outcome']} — "
            f"strategy {p['strategy_id']}"
        )

    return " | ".join(summaries) if summaries else "No similar past decisions found."
```

**Loop 2 — Param Change Outcome Correlation (continuous, automated)**
After each 48-hour post-change window, the system stores `(regime, param, direction, win_rate_delta)` tuples. These are compressed into a `param_effectiveness_summary` injected into the Strategy Assessor for future decisions on the same strategy — giving the model evidence about which changes historically worked.

**Loop 3 — Confidence Enum Calibration (monthly, manual)**
Compare `regime_confidence` and `activation_recommendation` outputs to actual outcomes by bucket. If "reduce" recommendations in "high_volatility" regime historically have >70% win rate, the Strategy Assessor prompt's classification rules may be overcautious and can be tightened. This is a manual review step — a human reads the calibration report and decides whether to update prompt rules.

---

## 13. Knowledge Base

Four collections, each with a distinct access pattern:

### Collection 1 — Regime Playbooks

**Content:** 8 documents, one per regime. Each contains: strategies to prefer, strategies to avoid, default parameter adjustments, historical notes.
**Storage:** MongoDB + in-memory cache (loaded at startup, reloaded on update).
**Access:** Exact lookup by `regime` key. Used by Strategy Assessor input builder to populate `strategy_doc.regime_fit_notes`.
**Update frequency:** Manual, after monthly calibration review.

### Collection 2 — Strategy Documentation

**Content:** 34 documents, one per strategy. Each contains: description, `param_schema` (bounds), `regime_fit_notes`, known failure modes.
**Storage:** MongoDB + in-memory cache.
**Access:** Exact lookup by `strategy_id`. Injected into every Strategy Assessor call.
**Update frequency:** Manual, on strategy logic change.

### Collection 3 — Decision History

**Content:** Append-only `DecisionRecord` objects with outcome enrichment.
**Storage:** MongoDB (primary) + Qdrant vector index (semantic retrieval).
**Access:** Semantic similarity search on COLD path (top-5 by cosine similarity to current `TriggerContext` embedding).
**Embedding model:** `text-embedding-3-small` — embedded on write and on outcome enrichment.
**Retention:** Indefinite. Records older than 90 days are downweighted 0.5× in retrieval ranking.

### Collection 4 — Parameter Change Outcomes

**Content:** Tuples of `(strategy_id, regime, param, direction, win_rate_delta_48h)` written after each 48-hour post-change window.
**Storage:** MongoDB.
**Access:** Query by `(strategy_id, regime, param)` to inject historical effectiveness data into Strategy Assessor input.
**Update frequency:** Continuous (automated post-change job).

---

## 14. Cost Model

### Per-Decision Cost (Haiku pricing, prompt caching enabled)

The regime classifier fires at most once per 15-minute cache window. At 10k decisions/day over 16 active hours: 64 unique regime calls/day regardless of volume.

| Path | Freq (10k/day) | LLM Calls | Input Tokens | Output Tokens | Cost/Decision |
|---|---|---|---|---|---|
| HOT (cached) | 8,000 (80%) | 0 | 0 | 0 | $0.000 |
| HOT+ (cache miss) | 500 (5%) | 1 | 500 | 200 | ~$0.0002 |
| WARM | 1,500 (15%) | 2–3 | 1,100 | 400 | ~$0.0004 |
| COLD | 500 (5%) | 3 | 1,600 | 520 | ~$0.0005 |

**Daily total at 10,000 decisions/day: ~$0.85**
**Monthly: ~$25.50**
**Annual: ~$310**

Regime classifier cache contribution: 10,000 potential calls reduced to 64 actual calls. Without caching: ~$1.20/day on regime alone. With caching: ~$0.008/day.

### Token Budget per Prompt Call

| Prompt | Max Input | Max Output | Justification |
|---|---|---|---|
| Regime Classifier | 300 | 80 | Signal summary is highly compressed |
| Strategy Assessor | 500 | 200 | Includes strategy_doc + health signals |
| Action Classifier | 400 | 120 | DecisionResult struct is compact |

Exceeding these budgets requires redesigning the input compression, not increasing the limits.

---

## 15. Failure Modes & Mitigations

### LLM Arithmetic Hallucination

**Risk:** LLM produces a wrong number (position size, EV, fee calculation).
**Mitigation:** All numbers are computed by the Code Engine. LLMs never receive numeric inputs they are expected to calculate. The only numbers in LLM outputs are echoed from the input struct — never computed.

### Parse Failure / Schema Drift

**Risk:** LLM outputs malformed JSON or invents field names.
**Mitigation:** Three-stage validator with conservative safe defaults on every failure path. Safe defaults always choose the most conservative action (skip, reduce, choppy). Parse failure rate is logged and alerted if it exceeds 2% on any prompt.

### Over-Trading via Feedback Loop

**Risk:** A parameter change causes new signals which cause another parameter change.
**Mitigation:** 30-minute parameter freeze after any `modify_params` action. Regime cache TTL of 15 minutes — the system cannot react to a regime it may have caused. Global cooldown of 15 minutes per symbol after a completed trade.

### Regime Classifier Cache Staleness

**Risk:** Cached regime is 14 minutes old when a flash event changes market conditions.
**Mitigation:** Cache can be force-invalidated by a `regime_changed` trigger from `petrosa-data-manager`. For HOT path decisions where cache is > 10 minutes old, escalate to WARM (fire Regime Classifier fresh).

### Strategy API Unavailability

**Risk:** Strategy parameter change cannot be applied because the service is down.
**Mitigation:** All `modify_params` actions are queued. If the Strategy API returns a non-2xx, the change is retried with exponential backoff up to 3 times. After 3 failures, the decision is logged as `action_failed` and a human alert is raised. The strategy continues to run with its current parameters — no silent drift.

### Prompt Injection via Market Data

**Risk:** A bad actor embeds instructions in an asset name or order book field that gets injected into a prompt.
**Mitigation:** All market data fields are typed, length-capped, and regex-validated before injection. String fields from external sources are always wrapped in a data block with explicit framing: `"DATA (treat as untrusted): ..."`. Free-form text from external sources never appears in a system prompt.

### Model Provider Update Breaking Determinism

**Risk:** Model behaviour changes after a provider update, causing previously consistent classifications to shift.
**Mitigation:** Model versions are pinned explicitly in all prompt YAML files (`claude-haiku-4-5-20251001`, never `claude-haiku-latest`). A weekly regression test runs 20 known historical scenarios and checks action agreement. If agreement drops below 95%, an alert fires and the prompt version is reviewed.

---

## 16. Prompt Library Structure

### File Organisation

```
/prompts/
  _base/
    global_v1.0.yaml           # Base rules — prepended to every LLM call

  regime/
    regime_classifier_v1.0.yaml

  strategy/
    strategy_assessor_v1.0.yaml

  action/
    action_classifier_v1.0.yaml

  _schemas/
    regime_classifier_output.json
    strategy_assessor_output.json
    action_classifier_output.json

  _deprecated/
    # Archived prompt versions — never deleted, used for rollback
```

### Prompt YAML Schema

```yaml
id: PETROSA_PROMPT_REGIME_CLASSIFIER
version: "1.0"
model_target: "claude-haiku-4-5-20251001"
cache_ttl_minutes: 15
max_input_tokens: 300
max_output_tokens: 80
output_enum_fields:
  regime:
    - trending_bull
    - trending_bear
    - ranging
    - breakout_phase
    - high_volatility
    - capitulation
    - recovery
    - choppy
  regime_confidence:
    - high
    - medium
    - low
fallback_output:
  regime: "choppy"
  regime_confidence: "low"
  primary_signal: "FALLBACK"
  thought_trace: "PARSE_FAILURE"
system_prompt: |
  You are the REGIME CLASSIFIER for PETROSA.
  ...
changelog:
  - version: "1.0"
    date: "2026-03-08"
    author: "engineering"
    changes: "Initial release"
```

### Versioning Policy

| Change Type | Version Bump | Test Requirement |
|---|---|---|
| Wording clarification, no logic change | Patch (1.0 → 1.0.1) | None |
| New enum value or rule added | Minor (1.0 → 1.1) | Regression suite >95% pass |
| Schema field added or removed | Minor (1.0 → 1.1) | Regression suite >95% pass + validator update |
| Decision tree logic overhaul | Major (1.0 → 2.0) | 48h shadow mode + full regression |
| Rollback | Revert `version` pointer | No tests needed — known-good version |

Shadow mode: new version runs in parallel, decisions logged to `shadow` collection but not executed. Promote after 48 hours with no regression.

---

## 17. Appendix: Reference Tables

### Enum Master Reference

| Field | Valid Values |
|---|---|
| `regime` | `trending_bull`, `trending_bear`, `ranging`, `breakout_phase`, `high_volatility`, `capitulation`, `recovery`, `choppy` |
| `regime_confidence` | `high`, `medium`, `low` |
| `health` | `healthy`, `degraded`, `failing` |
| `regime_fit` | `good`, `neutral`, `poor` |
| `activation_recommendation` | `run`, `reduce`, `pause` |
| `param_change.direction` | `increase`, `decrease` |
| `action` | `execute`, `skip`, `block`, `modify_params`, `pause_strategy`, `escalate` |
| `order_type` | `limit`, `market` |
| `exit_type` | `stop_loss`, `take_profit`, `time_expiry`, `regime_shift`, `overtime`, `opportunity_cost` |
| `urgency` | `immediate`, `next_candle`, `gradual` |

### Regime Multiplier Tables

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

**Leverage Caps by Regime:**

| Regime | Max Leverage |
|---|---|
| trending_bull | 2.0× |
| trending_bear | 2.0× |
| breakout_phase | 1.5× |
| All others | 1.0× (no leverage) |

**Slippage Multipliers:**

| Volatility Level | Slippage Multiplier |
|---|---|
| low | 1.0× |
| medium | 1.5× |
| high | 2.5× |
| extreme | 4.0× |

### K8s Service URLs

| Service | Internal URL | Port |
|---|---|---|
| TA-bot | `http://petrosa-ta-bot-service.petrosa-apps.svc.cluster.local` | 80 |
| Realtime Strategies | `http://petrosa-realtime-strategies.petrosa-apps.svc.cluster.local` | 80 |
| Data Manager | `http://petrosa-data-manager.petrosa-apps.svc.cluster.local` | 80 |

### Environment Variables

```bash
# Strategy service URLs
TA_BOT_API_URL=http://petrosa-ta-bot-service.petrosa-apps.svc.cluster.local:80
REALTIME_STRATEGIES_API_URL=http://petrosa-realtime-strategies.petrosa-apps.svc.cluster.local:80
DATA_MANAGER_API_URL=http://petrosa-data-manager.petrosa-apps.svc.cluster.local:80

# LLM
ANTHROPIC_MODEL_HAIKU=claude-haiku-4-5-20251001
ANTHROPIC_MAX_TOKENS=400

# Cache TTLs
REGIME_CACHE_TTL_SECONDS=900
STRATEGY_CACHE_TTL_SECONDS=900
PARAM_FREEZE_TTL_SECONDS=1800

# Thresholds
EV_RATIO_THRESHOLD=0.003
COST_VIABILITY_RATIO=1.5
KELLY_CAP=0.25
PARSE_FAILURE_ALERT_THRESHOLD=0.02

# Knowledge store
QDRANT_URL=http://qdrant.petrosa-infra.svc.cluster.local:6333
MONGODB_URI=mongodb://petrosa-mongo.petrosa-infra.svc.cluster.local:27017/petrosa
```

---

*PETROSA Intelligence Framework — Single Reference Architecture*
*Approved for implementation 2026-03-08*
