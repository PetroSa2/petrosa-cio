import uuid
from datetime import datetime
from typing import Any

try:
    from datetime import UTC
except ImportError:  # pragma: no cover — py310 compatibility
    from datetime import timezone

    UTC = timezone.utc  # noqa: UP017

from pydantic import BaseModel, Field

from cio.apps.nurse.models import RiskLimits
from cio.models.enums import (
    ConfidenceLevel,
    PnlTrend,
    RegimeEnum,
    TriggerType,
    VolatilityLevel,
)
from cio.models.regime import RegimeResult


class StrategyStats(BaseModel):
    """
    Historical and real-time performance metrics for a strategy.
    All fields are Optional to support new strategies with no trading history.
    When all fields are None, the Code Engine defaults to HealthStatus.HEALTHY.
    """

    win_rate: float | None = None
    avg_win_usd: float | None = None
    avg_loss_usd: float | None = None
    win_rate_delta: float | None = None
    consecutive_losses: int | None = None
    recent_pnl_trend: PnlTrend | None = None


class PortfolioSummary(BaseModel):
    """Aggregate portfolio state for exposure and concentration analysis."""

    gross_exposure: float  # 0.0 - 1.0
    same_asset_pct: float  # 0.0 - 1.0
    open_positions_count: int


class StrategyDefaults(BaseModel):
    """Default trading parameters for a strategy, sourced from strategy config."""

    stop_loss_pct: float
    take_profit_pct: float
    leverage: float = 1.0
    max_hold_hours: float


class MarketSignals(BaseModel):
    """Raw qualitative and quantitative signals for LLM analysis."""

    signal_summary: str
    current_price: float
    volatility_percentile: float
    trend_strength: float
    price_action_character: str


class TriggerContext(BaseModel):
    """
    Complete context object for a reasoning loop iteration.
    Assembled by the Context Builder before any persona runs.
    """

    # Orchestration and Routing (Winston's requirements)
    correlation_id: str = Field(
        ..., description="NATS message ID or unique GUID for this request flow"
    )
    decision_id: str = Field(
        default_factory=lambda: uuid.uuid4().hex,
        description="uuid4 hex string (32 chars, no hyphens) assigned by the CIO per intent; propagated to signals and Qdrant audit trail",
    )
    source_subject: str = Field(
        ..., description="Original NATS subject the trigger was received on"
    )

    # Trigger Data
    trigger_type: TriggerType
    trigger_payload: dict[str, Any]

    # Environment State
    regime: RegimeResult
    volatility_level: VolatilityLevel
    market_signals: MarketSignals

    # Strategy Context
    strategy_id: str
    # FR53 / P3.4 (#130): content-addressable strategy revision the intent
    # claims to apply to. None ⇒ legacy intent (pre-P3.4) — refusal gate
    # skips silently rather than rejecting every legacy producer.
    strategy_revision_id: str | None = None
    strategy_stats: StrategyStats
    strategy_defaults: StrategyDefaults

    # Portfolio & Risk State
    global_drawdown_pct: float
    open_orders_global: int
    open_orders_symbol: int
    available_capital_usd: float
    portfolio: PortfolioSummary
    risk_limits: RiskLimits

    # Market Constants for calculations
    maker_fee: float = 0.0002
    taker_fee: float = 0.0004
    is_perpetual_futures: bool = True
    funding_rate_8h: float = 0.0

    # Optional historical context from vector DB (COLD path only)
    historical_context: str | None = None

    # P1.4-AC1 / FR55-FR58 (#131): structured PreDecisionContext bundle
    # assembled by the orchestrator before personas run. None until the
    # bundle is wired into the assembly path (legacy call sites stay
    # green); downstream stories (122.2 missing-context, 122.3 prompt
    # contract) flip this to required once they ship.
    pre_decision_context: "PreDecisionContext | None" = None


# ----------------------------------------------------------------------
# P1.4-AC1 / FR55-FR58 (petrosa-cio#131): PreDecisionContext bundle
# ----------------------------------------------------------------------
# This is a deliberately *narrow* typed snapshot of the four subsystem
# inputs every arbitration decision consumes. It is distinct from
# `TriggerContext` (which carries trigger metadata + LLM-facing market
# signals) — `TriggerContext.pre_decision_context` embeds this bundle so
# downstream code can read the typed snapshot without re-querying.
#
# Out of scope for this story (deferred to the named EPIC children):
#   * missing-context fallback semantics  → 122.2
#   * prompt-contract enforcement on the bundle → 122.3
#   * dashboard exposure of the bundle → 122.7
# ----------------------------------------------------------------------


class MarketState(BaseModel):
    """FR55 — typed snapshot of market state at the moment of arbitration.

    Mirrors the bits of ``RegimeResult`` + ``MarketSignals`` that the
    arbitration prompt needs to reason about, projected into a single
    flat shape with an explicit ``observed_at`` so the audit trail can
    join across the four bundle fields.
    """

    regime: RegimeEnum
    regime_confidence: ConfidenceLevel
    volatility_level: VolatilityLevel
    current_price: float = Field(
        ..., description="Current market price at assembly time (quote currency)"
    )
    primary_signal: str = Field(
        ...,
        description=(
            "The signal that drove the regime classification — carried "
            "forward for the audit trail."
        ),
    )
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PortfolioState(BaseModel):
    """FR56 — typed snapshot of portfolio + risk state at arbitration time.

    Surfaces the aggregate figures (drawdown, capital, exposure) that the
    Code Engine + personas already consume separately on
    ``TriggerContext``. Keeping them together in one typed model is what
    lets downstream stories reason about "the portfolio at decision T"
    as a unit rather than reconstructing the picture field-by-field.
    """

    gross_exposure: float = Field(
        ..., description="Aggregate gross exposure (0.0 - 1.0)"
    )
    same_asset_pct: float = Field(
        ..., description="Concentration in the trigger's asset (0.0 - 1.0)"
    )
    open_positions_count: int = Field(..., ge=0)
    global_drawdown_pct: float = Field(
        ..., description="Portfolio-wide drawdown vs. high-water mark"
    )
    available_capital_usd: float = Field(..., ge=0.0)
    open_orders_global: int = Field(..., ge=0)
    open_orders_symbol: int = Field(..., ge=0)
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class EvaluatorVerdict(BaseModel):
    """FR57 — typed verdict snapshot from one subsystem evaluator.

    Subscribers tracking ``evaluator.{subsystem}.verdict`` already keep
    a ``(verdict, reason, observed_at)`` triple in-memory; this model is
    the typed projection of that triple so the bundle can carry it
    through arbitration without dict-shape drift.

    `verdict` is intentionally a free-form `str` (rather than a strict
    enum) to mirror the upstream P2.1 publisher contract which accepts
    `healthy | unhealthy | unknown`; downstream stories may tighten this
    once the full vocabulary is locked across subsystems.
    """

    subsystem: str = Field(..., min_length=1)
    verdict: str = Field(..., description="healthy | unhealthy | unknown")
    reason: str = Field("", description="Operator-readable explanation, may be empty")
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class CharacterizationRef(BaseModel):
    """FR58 — minimal reference to the admitted Characterization.

    The bundle does NOT carry the full Characterization document (that
    payload is large and lives in data-manager); it carries the
    revision identifier + admission timestamp so arbitration can:

      * cite the exact revision in the audit trail, and
      * defer the full fetch to AC2 (missing-context handling).

    ``strategy_revision_id`` follows the ``srev_<module_hash[:12]>_<parameter_hash[:12]>``
    shape locked by FR53 / P3.4 in petrosa-data-manager#179.
    """

    strategy_id: str = Field(..., min_length=1)
    strategy_revision_id: str = Field(..., min_length=1)
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PreDecisionContext(BaseModel):
    """FR55-FR58 — the structured bundle every arbitration decision consumes.

    Assembled once by the orchestrator (or, in production, by
    ``ContextBuilder.assemble_pre_decision_context``) and threaded
    through to personas + audit trail. The four fields map 1:1 to the
    AC1.a contract on the parent EPIC ticket — they are intentionally
    typed individually so the LLM prompt-contract story (AC3, separate
    child) can assert shape without inspecting the underlying
    subsystems.

    ``characterization`` is ``None`` when the strategy has not yet
    received an admitted characterization (e.g. a brand-new strategy
    in admission flow). Refusal semantics for stale characterizations
    are owned by the FR53 / P3.4 stale-gate (petrosa-cio#130); this
    field only records *what was observed* at decision time.
    """

    market_state: MarketState
    portfolio_state: PortfolioState
    evaluator_verdicts: dict[str, EvaluatorVerdict] = Field(default_factory=dict)
    characterization: CharacterizationRef | None = None
    assembled_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# Resolve the forward reference declared on TriggerContext above so
# Pydantic can finalize the schema once PreDecisionContext is defined.
TriggerContext.model_rebuild()
