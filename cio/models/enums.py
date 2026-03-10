try:
    from enum import StrEnum
except ImportError:
    # Fallback for Python < 3.11
    from enum import Enum

    class StrEnum(str, Enum):  # noqa: UP042
        pass


class RegimeEnum(StrEnum):
    """Internal framework regimes used by LLM Personas and Decision Arbiter."""

    TRENDING_BULL = "trending_bull"
    TRENDING_BEAR = "trending_bear"
    RANGING = "ranging"
    BREAKOUT_PHASE = "breakout_phase"
    HIGH_VOLATILITY = "high_volatility"
    CAPITULATION = "capitulation"
    RECOVERY = "recovery"
    CHOPPY = "choppy"


class DataManagerRegimeEnum(StrEnum):
    """Exact regimes returned by petrosa-data-manager /analysis/regime API."""

    TURBULENT_ILLIQUIDITY = "turbulent_illiquidity"
    STABLE_ACCUMULATION = "stable_accumulation"
    BREAKOUT_PHASE = "breakout_phase"
    CONSOLIDATION = "consolidation"
    BULLISH_ACCELERATION = "bullish_acceleration"
    BEARISH_ACCELERATION = "bearish_acceleration"
    BALANCED_MARKET = "balanced_market"
    TRANSITIONAL = "transitional"
    UNKNOWN = "unknown"


class VolatilityLevel(StrEnum):
    """Volatility classification levels.

    Note: EXTREME is framework-internal only. Not returned by data-manager API.
    Set by Code Engine when internal volatility calculation exceeds HIGH threshold.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    EXTREME = "extreme"


class ConfidenceLevel(StrEnum):
    """3-value enum for LLM and API confidence classification."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class HealthStatus(StrEnum):
    """Strategy health status based on performance delta."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILING = "failing"


class RegimeFit(StrEnum):
    """Qualitative fit of a strategy to the current market regime."""

    GOOD = "good"
    NEUTRAL = "neutral"
    POOR = "poor"


class ActivationRecommendation(StrEnum):
    """LLM recommendation for strategy activation level."""

    RUN = "run"
    REDUCE = "reduce"
    PAUSE = "pause"


class ActionType(StrEnum):
    """Final decision actions taken by the CIO."""

    EXECUTE = "execute"
    MODIFY_PARAMS = "modify_params"
    SKIP = "skip"
    BLOCK = "block"
    PAUSE_STRATEGY = "pause_strategy"
    ESCALATE = "escalate"


class TriggerType(StrEnum):
    """Types of events that trigger the CIO reasoning loop."""

    TRADE_INTENT = "trade_intent"
    STRATEGY_DEGRADED = "strategy_degraded"
    REGIME_CHANGED = "regime_changed"
    EXPOSURE_THRESHOLD = "exposure_threshold"
    SCHEDULED_REVIEW = "scheduled_review"
    PARAMETER_OPTIMIZATION = "parameter_optimization"
    ESCALATION = "escalation"


class ParamChangeDirection(StrEnum):
    """Direction of a parameter adjustment signal from the Strategy Assessor."""

    INCREASE = "increase"
    DECREASE = "decrease"


class OrderType(StrEnum):
    """Execution order types."""

    LIMIT = "limit"
    MARKET = "market"


class ExitType(StrEnum):
    """Reasons for closing a trading position."""

    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    TIME_EXPIRY = "time_expiry"
    REGIME_SHIFT = "regime_shift"
    OVERTIME = "overtime"
    OPPORTUNITY_COST = "opportunity_cost"


class PnlTrend(StrEnum):
    """Qualitative trend of recent PnL."""

    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"
