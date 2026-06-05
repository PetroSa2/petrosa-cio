# CIO Revamp Plan — Current State & Target Architecture

**Status:** Draft for review
**Authors:** Brainstorming session, 2026-06-04
**Scope:** Prompts, authorization, characterization, leverage arbitration
**Companion docs:** `architecture/`, `IMPLEMENTATION_PLAN.md`, `CONTRACTS.md`

---

## Executive Summary

The CIO today makes trading decisions through three sequential LLM personas
operating on a thin pipeline of enum labels, applies four sets of hardcoded
risk constants from `engine.py`, and runs leverage arbitration as effectively
dead code. The system works but has structural gaps that block further
sophistication:

1. **Personas decide blindly** — downstream personas receive *labels* (regime
   enum, regime_fit enum, health enum) without the underlying measurements
   that produced them. Information is lost at every hand-off.
2. **Decision logic is global and hardcoded** — `engine.py` has four
   constant dicts that apply to every strategy uniformly. There is no
   per-strategy adaptation. A momentum strategy and a mean-revert strategy
   get identical treatment.
3. **Leverage arbitration is fragmented** — three independent guards
   (`engine.REGIME_LEVERAGE_CAPS`, `arbitrate_leverage()`, and
   `portfolio_tracker.would_breach_ceiling()`) check leverage in sequence
   without sharing context.
4. **Authorization state is ephemeral** — `AuthorityStore`, pending-approval
   queue, and audit log are in-memory only. Any restart wipes operator
   configuration.
5. **The Characterization document is a reference, not a payload** —
   `CharacterizationRef` carries `{strategy_id, strategy_revision_id}` but no
   actual edge envelope, even though FR53/FR58 wire the plumbing for it.

The proposed direction makes the **Characterization document** the central
artifact — a per-strategy edge envelope that captures preferred regimes,
direction bias, volatility band, leverage envelope, baseline win rate, and
risk parameters. Every CIO component reads from it; the hardcoded constants
in `engine.py` get deleted. Each new strategy revision ships with an
LLM-generated characterization (Claude Opus, seeded from public knowledge of
the strategy archetype + the strategy's defaults). A live-calibration overlay
detects drift between the declared characterization and observed
performance, flagging revisions for re-derivation.

This is not a rewrite — it is a re-centering around a document the system
already references but does not yet read.

---

## Part 1 — Current State

### 1.1 Prompt System

**Three personas, three YAML prompt registries:**

| Persona | File | Prompt ID |
|---|---|---|
| `RegimeAnalyst` | `cio/prompts/regime_classifier_v1.yaml` | `PETROSA_PROMPT_REGIME_CLASSIFIER` |
| `StrategyAssessor` | `cio/prompts/strategy_assessor_v1.yaml` | `PETROSA_PROMPT_STRATEGY_ASSESSOR` |
| `ActionClassifier` | `cio/prompts/action_classifier_v1.yaml` | `PETROSA_PROMPT_ACTION_CLASSIFIER` |

Each YAML carries a `system_prompt` (full) and a `system_prompt_minimal`
(low-capability fallback). `cio/prompts/loader.py:select_system_prompt`
chooses between them by the LLM client's `capability_profile`.

**Sequential pipeline (`cio/core/orchestrator.py`):**

```
TriggerContext
  ├─ CodeEngine.run(context)                       [deterministic]
  ├─ Portfolio ceiling pre-check                   [deterministic]
  ├─ RegimeAnalyst.classify(context)               [LLM, cached 900s]
  ├─ StrategyAssessor.assess(context)              [LLM, cached 900s]
  └─ ActionClassifier.classify(context, code, regime, strategy)
                                                   [LLM, no cache]
```

The personas run **sequentially** even though regime and strategy are
independent (the strategy assessor receives the regime enum but does not
depend on the regime classifier completing first in any data sense — they
could be parallelized, only the action classifier truly waits on both).

**What each persona actually sees:**

| Persona | Inputs | Output |
|---|---|---|
| `RegimeAnalyst` | `signal_summary`, `volatility_percentile`, `trend_strength`, `price_action_character` | `regime`, `regime_confidence`, `volatility_level`, `primary_signal` |
| `StrategyAssessor` | `strategy_id`, `win_rate`, `win_rate_delta`, `consecutive_losses`, `recent_pnl_trend`, **`regime` (enum only)**, `regime_confidence` | `health`, `regime_fit`, `activation_recommendation`, optional `param_change` |
| `ActionClassifier` | All enum outputs from above + Code Engine results (`hard_blocked`, `gross_ev`, `kelly_position_usd`, `risk_warnings`) + `pre_decision_context` bundle | `action`, `justification`, `thought_trace` |

**Critical observation: each downstream persona operates on labels of
labels.** `StrategyAssessor` sees `regime=trending_bull` without the
`trend_strength` or `volatility_percentile` that produced it.
`ActionClassifier` sees `regime_fit=poor` and `health=degraded` without
the underlying win-rate trajectory or the volatility band that drove
those judgments. The `pre_decision_context` bundle (FR55–FR58) does
carry `MarketState` and `PortfolioState`, but those are themselves
projections of enums and aggregates — not the raw measurements.

**Other prompt issues:**

- The action classifier prompt has documentary placeholders
  `{market_state}`, `{portfolio_state}`, `{evaluator_verdicts}`,
  `{characterization}` — these are **not** interpolated by Python; they
  are literal strings in the system prompt. The data flows in the user
  message under `pre_decision_context`. The `{}` notation is
  misleading; `validate_prompt()` in `cio/prompts/context_contract.py`
  checks for the literal strings, not template slots.
- Prompts define output schema and "ABSOLUTE RULES" but no decision
  logic — no thresholds, no priority ordering, no decision table. The
  LLM invents its own reasoning on each call.
- `thought_trace` is capped at 120 characters — minimal forensic value.

### 1.2 Authorization System

Three independent layers gate every decision before dispatch:

**(a) Per-ActionType Authority — `cio/core/authority.py:AuthorityStore`**

Thread-safe in-memory map `ActionType → ActionAuthority`:

| State | Behavior |
|---|---|
| `ENABLED` | Dispatch as-is |
| `OPERATOR_APPROVAL_REQUIRED` | Divert to pending queue; no dispatch until `approve_pending()` |
| `DISABLED` | Substitute with next-best safe fallback from `DEFAULT_FALLBACKS` |

Defaults: all 16 ActionTypes start as `ENABLED`. Mutations via
`AuthorityStore.set_state(...)` (HTTP surface in
`cio/apps/authority_api.py`) are appended to an in-memory `_audit` list.

Pending-decision queue lives on the same store (`_pending` dict).
Decisions are enqueued by `apply_authority()` in `cio/core/router.py`
when the resolved state is `OPERATOR_APPROVAL_REQUIRED`. They sit until
`approve_pending(queue_id)` or `reject_pending(queue_id)` is called.

**Critical observation: in-memory only.**
- Authority state is lost on every restart — all 16 actions revert to `ENABLED`.
- Pending decisions are lost on restart with no recovery path.
- The audit log is lost on restart.
- There is no expiry on pending decisions — they sit forever if the
  operator doesn't act.
- There is no operator notification when a decision is enqueued. The
  operator dashboard must poll to discover them.

**(b) Leverage Arbitration — `cio/core/leverage_arbiter.py:arbitrate_leverage`**

A pure function with three branches:

```
recommended_leverage is None     → fallback: decided = per_strategy_bound
recommended_leverage <= bound    → accept:   decided = recommended_leverage
recommended_leverage  > bound    → override: decided = per_strategy_bound (clamp, never reject)
```

Where `per_strategy_bound = min(operator_max, strategy_envelope)` when
the envelope is present, else `operator_max` (from env
`CIO_DEFAULT_MAX_LEVERAGE`, default 10).

**Critical observations:**
- Both `recommended_leverage` and `strategy_envelope` are always
  `None` in production today. The arbiter resolves to `operator_max`
  on every admission. The entire branch tree is dead code.
- The arbiter is naive even when wired — it knows nothing about
  regime, volatility, drawdown, portfolio concentration, strategy
  health, evaluator verdicts, or position direction.
- The "override clamps, never rejects" rule is debatable — a 50×
  recommendation against a 3× envelope is almost certainly a producer
  bug or attack; silent clamping is loud-failure-mode worse than
  rejection.

**(c) Portfolio Aggregate Leverage Ceiling — `cio/core/portfolio_tracker.py`**

Computes `Σ(position_size_usd × leverage) / equity` and rejects with
`rejection_source=AGGREGATE_LEVERAGE_CEILING` if the projected
aggregate exceeds `CIO_PORTFOLIO_LEVERAGE_CEILING` (default 5.0).

Critical observation: this is a third leverage gate that doesn't share
state with the arbiter — they fire in sequence but don't compose.

**(d) Hardcoded leverage caps — `cio/core/engine.py`**

```python
REGIME_LEVERAGE_CAPS = {
    RegimeEnum.TRENDING_BULL:  2.0,
    RegimeEnum.TRENDING_BEAR:  2.0,
    RegimeEnum.BREAKOUT_PHASE: 1.5,
}
DEFAULT_LEVERAGE_CAP = 1.0
```

A **fourth** leverage logic site, baked into the deterministic Code
Engine. Per-regime, not per-strategy.

**Net: four independent leverage decision sites that don't compose.**

### 1.3 Characterization System

**`CharacterizationRef` (`cio/models/context.py`)** currently carries
only:

```python
class CharacterizationRef(BaseModel):
    strategy_id: str
    strategy_revision_id: str  # srev_<module_hash[:12]>_<param_hash[:12]>
    observed_at: datetime
```

There is no payload — no edge envelope, no preferred regimes, no
leverage envelope, no baseline metrics. The reference exists so the
audit trail can cite which revision was active, and so the
**stale-characterization refusal gate**
(`cio/core/characterization_stale_gate.py:is_characterization_stale`)
can refuse to trade when the cited revision has no record in
data-manager.

**`StrategyDefaults` (`cio/models/context.py`)** carries thin static
config (fetched from `data-manager /api/v1/config/strategies/{id}`):

```python
class StrategyDefaults(BaseModel):
    stop_loss_pct: float
    take_profit_pct: float
    leverage: float = 1.0
    max_hold_hours: float
```

These are global per-strategy defaults — not regime-conditional, not
volatility-conditional, not adaptive.

**Critical observations:**
- The schema for what *should* be in the Characterization document is
  not locked anywhere in CIO. Data-manager owns the storage; the
  contract is undefined.
- The personas never receive characterization content; the bundle
  carries only the reference.
- There is no path for creating a characterization for a new strategy
  revision; the stale gate just refuses to trade if one is referenced
  but missing.
- Most production strategies today have no characterization at all
  — they are pre-FR53 legacy intents (no `strategy_revision_id` in
  the payload), so the stale gate skips silently.

### 1.4 Engine Hardcoded Constants

`cio/core/engine.py` has four global constant dicts:

```python
SL_VOL_MULTIPLIERS  = { LOW: 1.0, MEDIUM: 1.2, HIGH: 1.5, EXTREME: 2.0 }
REGIME_TP_MULTIPLIERS = {
    TRENDING_BULL: 1.3, TRENDING_BEAR: 1.3, BREAKOUT_PHASE: 1.5,
    RANGING: 0.8, CHOPPY: 0.6, HIGH_VOLATILITY: 0.7,
    CAPITULATION: 0.6, RECOVERY: 1.0,
}
REGIME_LEVERAGE_CAPS = { TRENDING_BULL: 2.0, TRENDING_BEAR: 2.0, BREAKOUT_PHASE: 1.5 }
DEFAULT_LEVERAGE_CAP = 1.0
REGIME_HARD_BLOCKS = {
    CAPITULATION: "...",
    CHOPPY: "...",
}
```

These apply globally — every strategy gets the same SL haircut at high
volatility, the same TP multiplier in `trending_bull`, the same
leverage cap in `breakout_phase`, the same hard-block on `choppy`.
There is no concept of a strategy that *thrives* in `choppy` (e.g.
mean-revert), or a strategy whose signal quality survives
`capitulation` (some long-vol setups). The constants enforce a single
implicit strategy archetype across the entire system.

### 1.5 Orchestrator Flow Today

```
TriggerContext arrives
  │
  ├─ [1] Stale-characterization gate (FR53)
  │      └─ refuse if strategy_revision_id present but data-manager has no record
  │
  ├─ [2] CodeEngine.run(engine_context)
  │      ├─ Risk gates (drawdown, global orders, symbol orders) → hard_block
  │      ├─ Regime hard-block lookup (REGIME_HARD_BLOCKS) → hard_block
  │      ├─ SL/TP generation (SL_VOL_MULTIPLIERS × REGIME_TP_MULTIPLIERS)
  │      ├─ Kelly sizing
  │      └─ Returns: hard_blocked, block_reason, gross_ev, kelly_position_usd
  │
  ├─ [3] Portfolio aggregate ceiling check
  │      ├─ arbitrate_leverage(recommended=None, envelope=None) → operator_max
  │      └─ would_breach_ceiling(size, leverage, equity) → REJECT or record_admit
  │
  ├─ [4a] If hard_blocked OR deterministic bypass:
  │       └─ ActionClassifier.classify(bypass_mode=True) → BLOCK or EXECUTE
  │
  └─ [4b] LLM path:
          ├─ RegimeAnalyst.classify()        [cached 900s by strategy_id]
          ├─ StrategyAssessor.assess()       [cached 900s by strategy_id]
          ├─ ActionClassifier.classify()
          └─ FR63 spend-ceiling check
```

Three notes on this flow:
- The regime cache key is `regime:{strategy_id}`, but regime is
  market-wide, not strategy-specific — every strategy computes its
  own cached regime even though they should all agree.
- The deterministic bypass path returns `EXECUTE` for any non-blocked
  signal — no nuance, no caution.
- The 900-second TTL means a strategy assessment can be 15 minutes
  stale, even if win-rate just dropped 10 points in the last minute.

---

## Part 2 — Target State

### 2.1 Vision

A single per-strategy **Characterization** document becomes the source
of truth for *how this strategy wants to be reasoned about*. Every
component reads from it:

- `CodeEngine` reads `regime_profile.avoid_regimes` instead of the
  hardcoded `REGIME_HARD_BLOCKS`.
- `CodeEngine` reads `regime_profile.leverage_per_regime` instead of
  the hardcoded `REGIME_LEVERAGE_CAPS`.
- `CodeEngine` reads `volatility_profile.sl_multipliers` instead of
  the hardcoded `SL_VOL_MULTIPLIERS`.
- `StrategyAssessor` compares `win_rate` against
  `performance_baseline.baseline_win_rate` instead of the hardcoded
  `0.45 → healthy / 0.35 → failing` thresholds.
- `ActionClassifier` checks whether the current regime is in
  `regime_profile.preferred_regimes` instead of an enum-fit lookup.
- A **unified leverage arbiter** replaces the three current sites,
  reading the leverage envelope from the characterization and
  applying full-context haircuts (regime × volatility × drawdown ×
  health × portfolio).

The characterization is generated by **Claude Opus** when a new
strategy revision is registered. The LLM is given the strategy's
declared defaults, its archetype description, and public knowledge
about that archetype, and produces a JSON characterization. An
operator reviews and approves before the characterization becomes
active.

A separate **live calibration overlay** tracks observed performance
vs the declared characterization. When drift exceeds a threshold, the
overlay flags the revision for re-derivation. The characterization
itself stays immutable per `strategy_revision_id` (preserving audit
reproducibility); only the calibration overlay updates live.

Existing strategies without a characterization (legacy intents) keep
trading with a **conservative synthesized default** — the most
restrictive envelope: all regimes acceptable, direction both,
leverage 1×, kelly multiplier 0.5, no special edge claims.

### 2.2 Characterization Schema (proposed)

```python
class Characterization(BaseModel):
    # Identity
    strategy_id: str
    strategy_revision_id: str        # srev_<module_hash[:12]>_<param_hash[:12]>
    schema_version: str = "1.0"

    # Provenance — who/what produced this
    derived_from: Literal["declared", "llm_generated", "backtest", "live_recalibrated"]
    derived_at: datetime
    derived_by: str                  # operator_id or "claude-opus-4-7"
    approved_by: str | None          # operator_id who reviewed
    approved_at: datetime | None
    parent_revision_id: str | None   # previous characterization this was re-derived from

    # Strategy archetype description (for prompts + audit)
    archetype: str                   # "momentum", "mean_revert", "breakout", "carry", ...
    description: str                 # one-paragraph plain text

    # ─── Regime profile ──────────────────────────────────────────────
    regime_profile: RegimeProfile

    # ─── Direction bias ──────────────────────────────────────────────
    direction_bias: DirectionBias

    # ─── Volatility envelope ─────────────────────────────────────────
    volatility_profile: VolatilityProfile

    # ─── Leverage envelope ───────────────────────────────────────────
    leverage_envelope: LeverageEnvelope

    # ─── Risk parameters ─────────────────────────────────────────────
    risk_parameters: RiskParameters

    # ─── Performance baseline ────────────────────────────────────────
    performance_baseline: PerformanceBaseline


class RegimeProfile(BaseModel):
    preferred_regimes:  list[RegimeEnum]   # this strategy excels here
    acceptable_regimes: list[RegimeEnum]   # survives but no edge
    avoid_regimes:      list[RegimeEnum]   # hard-block (replaces REGIME_HARD_BLOCKS)
    rationale: str                         # plain-text explanation


class DirectionBias(BaseModel):
    allowed_directions: list[Literal["long", "short"]]
    regime_specific:    dict[RegimeEnum, list[Literal["long", "short"]]] = {}
                        # e.g. {trending_bull: [long], trending_bear: [short]}


class VolatilityProfile(BaseModel):
    band: tuple[float, float]               # acceptable volatility_percentile range
    sl_multipliers: dict[VolatilityLevel, float]  # replaces SL_VOL_MULTIPLIERS
                                            # per-strategy because mean-revert and
                                            # momentum want different SL behavior


class LeverageEnvelope(BaseModel):
    max_leverage:        int                # absolute ceiling (replaces operator_max)
    per_regime:          dict[RegimeEnum, int]   # replaces REGIME_LEVERAGE_CAPS
    drawdown_haircuts:   dict[float, float]      # {0.05: 0.8, 0.10: 0.5, 0.15: 0.0}
                                            # at drawdown X, multiply leverage by Y
    volatility_haircuts: dict[VolatilityLevel, float]
                                            # at vol HIGH, multiply leverage by 0.7
    health_haircuts:     dict[HealthStatus, float]
                                            # at degraded, multiply leverage by 0.5


class RiskParameters(BaseModel):
    sl_pct_base:               float         # base stop-loss before multipliers
    tp_pct_base:               float         # base take-profit before multipliers
    tp_multipliers:            dict[RegimeEnum, float]  # replaces REGIME_TP_MULTIPLIERS
    max_consecutive_losses:    int           # per-strategy "failing" threshold
    max_drawdown_contribution: float         # this strategy's slice of portfolio risk
    max_hold_hours:            float


class PerformanceBaseline(BaseModel):
    baseline_win_rate:         float         # what "healthy" means for THIS strategy
    baseline_sharpe:           float | None
    baseline_avg_win_usd:      float | None
    baseline_avg_loss_usd:     float | None
    min_sample_size:           int           # trades needed before health assessment fires
    kelly_multiplier:          float         # 0.0–1.0, haircut on full Kelly sizing
```

**Why each field exists** — every field replaces a specific
hardcoded constant or unlocks a missing capability:

| Field | Replaces / Unlocks |
|---|---|
| `regime_profile.avoid_regimes` | `REGIME_HARD_BLOCKS` in engine.py |
| `regime_profile.preferred_regimes` | New: `regime_fit=good` decision |
| `direction_bias` | New: long/short awareness (currently absent) |
| `volatility_profile.sl_multipliers` | `SL_VOL_MULTIPLIERS` in engine.py |
| `volatility_profile.band` | New: volatility-based skip |
| `leverage_envelope.per_regime` | `REGIME_LEVERAGE_CAPS` in engine.py |
| `leverage_envelope.drawdown_haircuts` | New: drawdown-aware leverage |
| `leverage_envelope.volatility_haircuts` | New: vol-aware leverage |
| `leverage_envelope.health_haircuts` | New: health-aware leverage |
| `risk_parameters.tp_multipliers` | `REGIME_TP_MULTIPLIERS` in engine.py |
| `risk_parameters.max_consecutive_losses` | Hardcoded `≥6` in prompt |
| `performance_baseline.baseline_win_rate` | Hardcoded `0.45/0.35` in prompt |
| `performance_baseline.kelly_multiplier` | New: per-strategy Kelly haircut |
| `performance_baseline.min_sample_size` | New: skip health when undersampled |

### 2.3 Generation Pipeline (Claude Opus)

**Decision captured: Claude Opus generates v1 characterizations,
seeded from strategy defaults + public knowledge of the archetype.**

Workflow:

```
Strategy author registers new revision
    │
    ▼
┌─────────────────────────────────────┐
│   Characterization Generator        │  (new CLI tool or service)
│                                     │
│   Inputs:                           │
│   - strategy code (read-only)       │
│   - StrategyDefaults from           │
│     data-manager                    │
│   - archetype label                 │
│   - operator-supplied description   │
│                                     │
│   Prompt → Claude Opus              │
│   "Given a {archetype} strategy     │
│    with these defaults, produce a   │
│    Characterization document        │
│    following this schema..."        │
│                                     │
│   Output: Characterization JSON     │
└─────────────────────────────────────┘
    │
    ▼
Operator reviews + edits + approves
    │
    ▼
POST /api/v1/characterizations  →  data-manager (Mongo)
    │
    ▼
CIO fetches at decision time
```

**Generator implementation options:**

- **Standalone CLI in petrosa-cio** — `scripts/generate_characterization.py`
  takes a `strategy_revision_id`, fetches the strategy + defaults, calls
  Opus, dumps JSON for operator review. Simplest.
- **Service in ta-analysis bot** — characterization generation lives
  alongside backtesting; same service produces backtest-empirical
  characterizations once backtests are available.
- **Endpoint in CIO itself** — `POST /api/v1/characterizations/generate`
  with a draft-then-approve flow. Heavier; couples generation to the
  live decision service.

Recommended for v1: **standalone CLI in petrosa-cio** under `scripts/`,
called manually by the operator at revision registration. Move to
ta-analysis bot in v2 when backtests are available to overlay.

**Prompt design for Opus:**

```
You are characterizing a {archetype} trading strategy for the Petrosa
CIO system. Given the strategy's declared defaults, its archetype, and
the operator's description, produce a Characterization document.

The Characterization tells the CIO:
  - Which market regimes this strategy has edge in (preferred)
  - Which regimes it survives without edge (acceptable)
  - Which regimes are toxic to this strategy (avoid — hard-block)
  - What leverage is safe in each regime
  - How to haircut leverage under drawdown / volatility / poor health
  - What "healthy" win rate looks like for this archetype
  - How many trades are needed before health assessment is meaningful

Use public knowledge of how {archetype} strategies behave. Be
conservative — operators will review your output, but the production
default is that your numbers go live.

[schema definition + strategy code summary + defaults follow]

Output ONLY valid JSON matching the schema.
```

The operator review step is non-optional: every Opus-generated
characterization passes through `Status=Draft` until an operator marks
it `Status=Approved`. Only approved characterizations are returned by
data-manager to CIO.

### 2.4 Adaptive Calibration Overlay

**Decision captured: include the adaptive layer from v1.**

The Characterization document itself stays **immutable per
`strategy_revision_id`** — this preserves the FR53 audit reproducibility
("trade T was decided with characterization C"). But a parallel
**calibration document** tracks rolling observed performance:

```python
class CalibrationOverlay(BaseModel):
    strategy_id: str
    strategy_revision_id: str
    window_start: datetime
    window_end: datetime
    sample_size: int               # actual trades in window
    observed_win_rate: float
    observed_sharpe: float | None
    observed_avg_win_usd: float
    observed_avg_loss_usd: float
    regime_distribution:   dict[RegimeEnum, int]      # which regimes saw trades
    direction_distribution: dict[str, int]            # long/short counts

    # Drift signals
    win_rate_drift:        float   # observed - baseline
    drift_severity:        Literal["green", "yellow", "red"]
    drift_flagged_at:      datetime | None
    rederivation_recommended: bool
```

**Drift detection rules (initial):**

| `drift_severity` | Trigger |
|---|---|
| `green`  | `\|observed - baseline\| < 0.05` |
| `yellow` | `0.05 ≤ \|observed - baseline\| < 0.10` |
| `red`    | `\|observed - baseline\| ≥ 0.10` OR `consecutive_losses ≥ max_consecutive_losses` |

`yellow` produces a daily ops report entry. `red` emits a NATS alert
on `alerts.characterization.drift.{strategy_id}` and sets
`rederivation_recommended=true`. The CIO continues to use the
declared characterization — drift does not auto-mutate the document.

Re-derivation:
- Operator triggers `scripts/generate_characterization.py` with the
  overlay as additional context.
- Opus produces a new draft with the observed numbers in scope.
- Operator approves → new `strategy_revision_id`, new
  Characterization. The strategy now publishes intents with the new
  revision id; old intents with the prior revision continue to be
  honored by the stale-gate until they're closed.

**Rolling window**: configurable; default 50 trades or 30 days,
whichever fires first. Configurable per strategy through the
Characterization itself (`performance_baseline.min_sample_size`).

### 2.5 Migration Path

**Decision captured: allow strategies without characterization, with
conservative synthesized defaults.**

Three tiers of strategies during transition:

| Tier | State today | Treatment |
|---|---|---|
| 1. Legacy, no `strategy_revision_id` | Most production strategies | `ConservativeDefaultCharacterization` synthesized at request time; stale-gate skips silently |
| 2. Revision id present, no characterization document | New revisions ship before Opus-generation pipeline is live | Same conservative default; ops report flags "needs characterization" |
| 3. Revision id + approved characterization | Target state | Full characterization-driven reasoning |

**`ConservativeDefaultCharacterization`** — synthesized at boot, never
written to data-manager:

```python
ConservativeDefaultCharacterization = Characterization(
    archetype="unknown",
    description="Synthesized default — no edge envelope declared",
    derived_from="declared",
    derived_by="system_default",
    regime_profile=RegimeProfile(
        preferred_regimes=[],
        acceptable_regimes=[r for r in RegimeEnum if r not in (CAPITULATION, CHOPPY)],
        avoid_regimes=[CAPITULATION, CHOPPY],
        rationale="System default: avoid known toxic regimes only",
    ),
    direction_bias=DirectionBias(allowed_directions=["long", "short"]),
    volatility_profile=VolatilityProfile(
        band=(0.0, 0.9),
        sl_multipliers=SL_VOL_MULTIPLIERS,   # current hardcoded values
    ),
    leverage_envelope=LeverageEnvelope(
        max_leverage=1,                       # leverage 1× by default
        per_regime={},                        # falls through to max_leverage
        drawdown_haircuts={0.10: 0.5, 0.15: 0.0},
        volatility_haircuts={VolatilityLevel.HIGH: 0.5, VolatilityLevel.EXTREME: 0.0},
        health_haircuts={HealthStatus.DEGRADED: 0.5, HealthStatus.FAILING: 0.0},
    ),
    risk_parameters=RiskParameters(
        sl_pct_base=0.01, tp_pct_base=0.02,
        tp_multipliers=REGIME_TP_MULTIPLIERS,    # current hardcoded values
        max_consecutive_losses=4,
        max_drawdown_contribution=0.05,
        max_hold_hours=24.0,
    ),
    performance_baseline=PerformanceBaseline(
        baseline_win_rate=0.50,
        min_sample_size=20,
        kelly_multiplier=0.5,                  # half-Kelly by default
    ),
)
```

This default is **strictly more conservative than the current
production system** (leverage 1× not 2×, half-Kelly not full, more
regimes treated as avoid). Strategies don't break, but the cost of
not declaring an envelope is real — incentive to characterize.

### 2.6 Unified Leverage Arbiter (target)

Replaces the four current sites
(`REGIME_LEVERAGE_CAPS`, `arbitrate_leverage()`,
`portfolio_tracker.would_breach_ceiling`, ad-hoc) with one function:

```python
def arbitrate_leverage(
    *,
    recommended_leverage:   int | None,    # from signal payload
    characterization:       Characterization,
    market_state:           MarketState,
    portfolio_state:        PortfolioState,
    strategy_health:        HealthStatus,
    portfolio_tracker:      PortfolioTracker,
    operator_max:           int,           # global operator ceiling
) -> LeverageDecision:
    """Unified leverage arbitration with full audit trail."""
```

**Algorithm (sequence of haircuts):**

```
start = recommended_leverage or characterization.leverage_envelope.per_regime[regime]
                              or characterization.leverage_envelope.max_leverage

→ cap by operator_max
→ cap by characterization.leverage_envelope.per_regime[regime]
→ haircut by characterization.leverage_envelope.volatility_haircuts[vol_level]
→ haircut by characterization.leverage_envelope.drawdown_haircuts (largest threshold hit)
→ haircut by characterization.leverage_envelope.health_haircuts[health]
→ final aggregate-ceiling check (portfolio_tracker.would_breach_ceiling)
   ├─ if breach with decided leverage → try halving, retry
   └─ if still breach at 1× → REJECT
= decided_leverage + audit_trail
```

**Output:**

```python
@dataclass(frozen=True)
class LeverageDecision:
    decided_leverage:  int
    branch:            Literal["accept", "override", "fallback", "rejected"]
    audit_trail:       list[LeverageHaircut]    # ordered list of every step
    rejection_reason:  str | None

@dataclass(frozen=True)
class LeverageHaircut:
    stage:           str    # "operator_max", "regime_cap", "vol_haircut", ...
    before:          int
    after:           int
    factor:          float
    reason:          str
```

Operator dashboard shows: "Signal asked for 5×. Operator max 10×.
Regime cap (trending_bull) 3×. Volatility haircut (HIGH × 0.7) → 2×.
Drawdown haircut (drawdown 0.08 ≥ 0.05 threshold × 0.8) → 1×. Health
healthy ×1.0 → 1×. Aggregate ceiling check passed. Final: 1×."

**Also: reject-on-egregious-override.** If
`recommended_leverage > characterization.leverage_envelope.max_leverage × 2`,
reject the intent entirely with `rejection_source=LEVERAGE_OUT_OF_ENVELOPE`.
Silent clamping at this magnitude hides producer bugs.

### 2.7 Authorization Persistence (target)

The `AuthorityStore` becomes a thin in-memory cache backed by
data-manager:

```
CIO boot
  └─ GET /api/v1/authority  →  data-manager
                            └─ {action_type: state} map
                            ←  hydrate AuthorityStore
                            ←  hydrate pending decisions
                            ←  hydrate audit log (last N entries)

CIO mutation (set_state)
  └─ POST /api/v1/authority/{action_type}
                            ←  201, updated state
     update local cache

CIO enqueue_pending
  └─ POST /api/v1/authority/pending
                            ←  201, pending_id
     also: NATS publish alerts.cio.pending_decision.{action_type}
     also: ttl=300s — automatic reject if not resolved

CIO approve/reject_pending
  └─ DELETE /api/v1/authority/pending/{id} with body {approved, reason}
                            ←  204
```

Two new behaviors:

- **TTL on pending decisions**: 5 minutes default. After expiry,
  CIO synthesizes an automatic `reject_pending` with reason
  `pending_decision_expired`. Configurable per action type.
- **NATS notification on enqueue**: `alerts.cio.pending_decision.{action_type}`
  fires the moment a decision is diverted. Operator dashboard
  subscribes; no polling needed.

The decisional flow is unchanged — `apply_authority()` still
consults the store. Only the storage layer changes.

### 2.8 Personas (deferred but documented)

The thick-context + structured-reasoning persona refactor is the
**second** major theme (after characterization). It is documented here
for completeness but should ship after characterization is live, not
in parallel.

When ready, each persona changes:

- **Inputs become "thick"** — each persona gets the full raw signal
  block, per-trade history slice, position direction, and the
  characterization. Not just enum labels.
- **Outputs become structured** — each persona returns:

  ```python
  class PersonaAnalysis(BaseModel):
      label:       str    # the existing enum
      drivers:     list[str]   # 1-3 specific input fields + values that drove the label
      risks:       list[str]   # 1-3 things that would invalidate this verdict
      invalidators: list[str]  # observable conditions that would flip the label
      confidence:  float       # 0.0-1.0, persona's own confidence in the label
  ```

  The next persona reads the **drivers + risks**, not just the
  label. Audit trail persists the full analysis. Operator dashboard
  can show: "Why did we EXECUTE? Action classifier: 'health was
  healthy, regime preferred per characterization, no drawdown
  haircut, evaluator verdicts all green. Risks noted: regime
  confidence was medium not high; characterization derived 14 days
  ago and overlay shows yellow drift on win rate.'"

This refactor is invasive (every persona, every test). Doing it
after characterization is live means we have the right inputs in
place when we refactor the personas, instead of refactoring twice.

---

## Part 3 — Decisions Captured

| Decision | Choice |
|---|---|
| **Characterization author (v1)** | Strategy author via Claude Opus, seeded from strategy defaults + public archetype knowledge. Operator reviews and approves before activation. |
| **Migration posture** | Allow strategies without characterization, with conservative synthesized defaults. Strategies with characterizations get richer treatment. |
| **Adaptive layer** | Include from v1. Characterization itself is immutable per `strategy_revision_id`; a separate `CalibrationOverlay` document tracks live drift and flags revisions for re-derivation. |
| **Storage backend for authority** | data-manager (Mongo). Not Qdrant — Qdrant is vector search, wrong tool. Not Redis alone — durability needed. |
| **Personas refactor** | Documented but deferred. Ship characterization first, then refactor personas with the new inputs in place. |
| **Leverage arbiter** | Unified replacement of four current sites. Per-strategy envelopes from characterization. Full audit trail of haircuts. Reject on egregious override (>2× envelope). |

---

## Part 4 — Open Questions

1. **Where does the characterization-generator CLI live?**
   - Option A: `petrosa-cio/scripts/generate_characterization.py`
   - Option B: New service `petrosa-characterization-generator`
   - Option C: Inside `petrosa-bot-ta-analysis` (alongside backtests)
   - **Default proposal: A for v1, migrate to C in v2 when backtests overlay.**

2. **Calibration rolling window — trades, time, or both?**
   - Trades-only is noisy for low-volume strategies.
   - Time-only is noisy for high-volume.
   - **Default proposal: `min(N trades, T days)`, configurable per strategy via `performance_baseline.min_sample_size`.**

3. **Who reviews the LLM-generated characterizations?**
   - Single operator today (`@yurisa2`)?
   - Future: multi-operator approval with quorum?
   - **Default proposal: single-operator approval for v1, model the API for quorum later.**

4. **Should `direction_bias` be enforceable?**
   - A long-only strategy emits a short intent — reject, or downgrade?
   - **Default proposal: reject with `rejection_source=DIRECTION_VIOLATES_ENVELOPE`. Silent downgrade hides bugs.**

5. **Backward compatibility for `StrategyDefaults`?**
   - `StrategyDefaults.leverage`, `sl_pct`, `tp_pct` overlap with
     `Characterization.leverage_envelope` and `risk_parameters`.
   - **Default proposal: `StrategyDefaults` remains the source for
     strategies without a characterization. When both exist,
     characterization wins. Deprecate `StrategyDefaults` once full
     migration completes.**

6. **Schema versioning?**
   - `Characterization.schema_version` is in the model.
   - Need a migration story when v1.0 → v2.0.
   - **Default proposal: characterizations are immutable per revision;
     a new schema version triggers re-derivation, not in-place migration.**

7. **Does the prompt-context contract (FR55–FR58) need updating?**
   - Currently checks for `{characterization}` placeholder.
   - With real characterization payload, should also check for
     `{regime_profile}`, `{leverage_envelope}`, etc.?
   - **Default proposal: keep the four top-level surfaces as the contract;
     drill-down structure is internal to the bundle.**

8. **Where does the calibration overlay live?**
   - Co-located with the characterization in data-manager?
   - Computed live in CIO from the audit trail?
   - **Default proposal: stored in data-manager as a separate document,
     updated by a CIO background task that scans recent decisions.**

9. **What happens to the existing FR53 stale-characterization gate?**
   - Today: refuses if `strategy_revision_id` present but no document.
   - Target: still refuses, but the "document" now means the full
     characterization, not just the reference.
   - **Default proposal: gate behavior unchanged; document semantics
     widen.**

10. **Provider for Opus calls during characterization generation?**
    - Direct Anthropic API, or through the existing `CIO_LLM_Client` abstraction?
    - **Default proposal: through `CIO_LLM_Client` for consistency, but
      with a separate `capability_profile=characterization_generator`
      forcing the full prompt (no minimal variant).**

---

## Part 5 — Implementation Phases

Each phase is independently shippable; later phases depend on earlier ones.

### Phase 0 — Schema lock + data-manager endpoint

**Goal:** Lock the Characterization schema; expose CRUD in data-manager.

- Move the `Characterization` model (and sub-models) to a shared
  location — likely `petrosa-utils` or `petrosa-data-manager/models/`
  so both CIO and data-manager use the same types.
- Add `petrosa-data-manager` endpoints:
  - `GET /api/v1/characterizations?strategy_id=&strategy_revision_id=`
  - `POST /api/v1/characterizations` (creates draft)
  - `PATCH /api/v1/characterizations/{id}/approve` (operator approval)
  - `GET /api/v1/calibration-overlays?strategy_id=&strategy_revision_id=`
- Mongo collection `characterizations` with `{strategy_id, strategy_revision_id}` unique index.
- Out of scope: CIO integration (Phase 1).

**Deliverables:**
- Locked schema in code.
- data-manager endpoints with tests.
- Migration script to load `ConservativeDefaultCharacterization`
  shape (for documentation only, not stored).

**Acceptance:**
- `curl POST /api/v1/characterizations` with a sample JSON returns 201.
- `curl GET` retrieves it.
- Approval flow toggles state correctly.

### Phase 1 — CIO consumption of characterization

**Goal:** CIO fetches and threads characterizations through the
decision pipeline; the existing reference becomes a real payload.

- Add `Characterization` to `cio/models/context.py` (alongside the
  existing `CharacterizationRef`).
- Update `PreDecisionContext` to carry `characterization: Characterization`
  (typed full payload, not just the ref).
- Update `ContextBuilder._fetch_characterization_ref()` →
  `_fetch_characterization()` to fetch the full document.
- Add `ConservativeDefaultCharacterization` synthesis path for
  strategies without a document.
- Thread the characterization into every persona's user payload.

**Deliverables:**
- New `Characterization` model.
- `ContextBuilder` fetches and falls back.
- Personas receive the characterization (but don't yet act on it).

**Acceptance:**
- Every `TriggerContext` has either a real or conservative
  characterization.
- Stale-gate behavior unchanged.

### Phase 2 — Replace engine.py hardcoded constants

**Goal:** Delete `REGIME_HARD_BLOCKS`, `REGIME_LEVERAGE_CAPS`,
`REGIME_TP_MULTIPLIERS`, `SL_VOL_MULTIPLIERS` — read from the
characterization instead.

- Update `CodeEngine.run()` to read multipliers and caps from the
  characterization.
- Delete the four constant dicts.
- Update tests to expect characterization-driven behavior.

**Deliverables:**
- Constants removed; characterization-driven equivalents in place.
- Existing tests pass with `ConservativeDefaultCharacterization`
  as the test default.

**Acceptance:**
- `engine.py` has zero `dict[RegimeEnum, ...]` constants.
- Per-strategy regression: a "momentum" characterization and a
  "mean-revert" characterization produce different `block_reason`
  on the same trigger in `choppy` regime.

### Phase 3 — Unified leverage arbiter

**Goal:** Replace `arbitrate_leverage()` + `REGIME_LEVERAGE_CAPS` +
`portfolio_tracker.would_breach_ceiling()` ad-hoc composition with
one function.

- Implement the algorithm in §2.6.
- Wire `recommended_leverage` from signal payload (the
  long-pending F item).
- Reject-on-egregious-override path.
- Audit trail in `LeverageDecision`.
- Surface haircut list to operator dashboard.

**Deliverables:**
- New unified arbiter.
- Old `arbitrate_leverage()` deprecated; callsites updated.
- Dashboard shows haircut breakdown per decision.

**Acceptance:**
- A simulated trigger with `recommended_leverage=5`,
  characterization `max_leverage=3`, drawdown 8%, vol HIGH,
  health degraded produces decided_leverage with full audit
  trail showing each haircut step.

### Phase 4 — Opus characterization generator

**Goal:** A working CLI that produces characterizations from strategy
metadata.

- `petrosa-cio/scripts/generate_characterization.py`.
- Opus prompt design + iteration (real test with current
  production strategies).
- Operator review loop (CLI dumps JSON; operator edits and POSTs to
  data-manager).
- Add an "archetype" label to every existing strategy as a one-time
  migration.

**Deliverables:**
- CLI tool.
- Operator runbook in `docs/`.
- Characterizations for at least 3 current production strategies as
  proof of concept.

**Acceptance:**
- Run the CLI on a known-momentum strategy and a known-mean-revert
  strategy; the produced characterizations should reflect their
  archetypes (preferred regimes differ, leverage envelopes differ).

### Phase 5 — Calibration overlay + drift detection

**Goal:** Live performance tracking against the declared
characterization; drift alerts.

- New `CalibrationOverlay` model + data-manager endpoint.
- CIO background task: scan recent decisions per strategy_revision,
  compute rolling observed metrics, update the overlay.
- NATS alert on red drift.
- Daily ops report includes overlay summary.

**Deliverables:**
- Overlay storage + endpoint.
- CIO background task.
- Alert wiring.

**Acceptance:**
- Inject simulated underperformance into a strategy; observe the
  overlay flip yellow → red; NATS alert fires.

### Phase 6 — Authority persistence

**Goal:** Authority state, pending decisions, audit log survive
restarts via data-manager.

- New data-manager `/api/v1/authority/*` endpoints.
- CIO hydrate-on-boot.
- TTL + auto-expire for pending decisions.
- NATS notification on enqueue.

**Deliverables:**
- data-manager authority endpoints.
- CIO storage abstraction over `AuthorityStore`.
- Restart-resilience tests.

**Acceptance:**
- Set an action to `DISABLED`, restart CIO, verify state persists.
- Enqueue a pending decision, restart, verify it's still there.
- Pending decision auto-rejects after 5 minutes if untouched.

### Phase 7 — Persona refactor (thick context + structured reasoning)

**Goal:** Personas receive raw signals + per-trade history + position
direction + characterization; emit structured analysis instead of
just labels.

This is the largest phase by code surface and is sequenced last so
the new inputs (characterization, calibration) are already wired.

- New `PersonaAnalysis` output type.
- Rewrite each persona's `_build_user_context` to pass thick context.
- Rewrite each persona prompt to consume thick context + return
  structured analysis.
- Update `DecisionAssembler` to consume `PersonaAnalysis` instead
  of bare enums.
- Dashboard shows analysis breakdown per decision.

**Deliverables:**
- Refactored personas.
- New audit-trail shape (with analysis).
- Updated dashboard.

**Acceptance:**
- Every decision in the audit trail has drivers, risks, and
  invalidators for each persona's verdict — not just enum labels.

---

## Part 6 — Risks and Mitigations

| Risk | Mitigation |
|---|---|
| **Opus-generated characterizations are wrong** | Operator review is mandatory; conservative defaults catch the worst cases; calibration overlay flags drift. |
| **Schema thrash during early adoption** | Lock v1.0 schema before Phase 1 starts; schema_version field allows v2 to coexist. |
| **Per-strategy characterizations explode prompt token budget** | Pass only the relevant subset (regime profile + leverage envelope for current regime) to each persona. Cache the rest. |
| **Calibration overlay races with active decisions** | Overlay is computed in background; decisions always read the immutable characterization, never the live overlay. Overlay is informational. |
| **Operator approval becomes a bottleneck** | Conservative defaults mean no-approval-yet ≠ no-trade; the cost of slow review is reduced precision, not lost throughput. |
| **Data-manager dependency for authority creates a new SPOF** | CIO retains in-memory cache; data-manager unavailable at boot → CIO logs a warning and starts with conservative defaults (all enabled, no pending). Operator can re-set after data-manager returns. |
| **Egregious-override rejection breaks producers with buggy leverage values** | Phase 3 ships with the rejection threshold tunable per characterization; surface the rejection clearly so producer bugs are visible. |
| **Multiple revisions of same strategy in flight (graceful transition)** | Stale-gate already handles this — each intent carries its own `strategy_revision_id`. Both old and new characterizations stay valid until all open positions close. |

---

## Part 7 — Out of Scope

This plan intentionally does **not** cover:

- **Parallelizing regime + strategy assessor** — a small latency win,
  not architecturally meaningful. Defer.
- **Collapsing 3 LLM calls into 1** — premature; the right number of
  calls falls out of the persona refactor in Phase 7.
- **Replacing the deterministic-bypass EXECUTE default** — separate
  ticket; the spend-ceiling fallback should respect characterization
  but the redesign of bypass logic is its own conversation.
- **Multi-operator approval / quorum** — explicitly deferred to v2
  (see Open Question 3).
- **Backtesting-derived characterizations** — Phase 4 ships Opus-only;
  backtest overlay is a v2 feature when ta-analysis bot can produce them.
- **OpenTelemetry instrumentation of new components** — assumed to be
  applied consistently per repo conventions (`petrosa-otel`); not
  enumerated.

---

## Part 8 — Summary

| | Today | Target |
|---|---|---|
| Strategy adaptation | Global hardcoded constants in engine.py | Per-strategy characterization from data-manager |
| Author of decision logic | Engineer editing constants in code | Claude Opus, operator-reviewed, per revision |
| Leverage logic sites | 4 (engine consts, arbiter, portfolio tracker, hardcoded) | 1 unified arbiter |
| Leverage awareness | recommended + envelope (both always None) | Full context: regime × vol × drawdown × health × portfolio |
| Authority persistence | In-memory, lost on restart | data-manager backed, TTL, NATS-notified |
| Persona inputs | Enum labels | (Future) thick context: raw signals + history + position direction |
| Persona outputs | Enum labels | (Future) structured analysis: drivers + risks + invalidators |
| Characterization | Reference only | Full payload + immutable per revision + live calibration overlay |
| Strategies without characterization | All of them | Conservative synthesized default; migration as revisions ship |

The proposal does not rewrite the CIO. It moves the decision parameters
from engineer-edited code constants to operator-reviewed per-strategy
documents, unifies the fragmented leverage logic, and persists
authorization state. Each phase is independently shippable; each is
a strict improvement over the prior state.

---

*End of document.*
