from pydantic import BaseModel, Field

from cio.models.enums import (
    ActionType,
    ActivationRecommendation,
    ConfidenceLevel,
    HealthStatus,
    OrderType,
    RegimeEnum,
    RegimeFit,
    VolatilityLevel,
)
from cio.models.regime import RegimeResult
from cio.models.strategy import AppliedParamChange, StrategyResult


class ActionResult(BaseModel):
    """Lightweight result from the Action Classifier LLM."""

    action: ActionType
    justification: str = Field(..., max_length=200)
    thought_trace: str = Field(..., max_length=120)


class DecisionResult(BaseModel):
    """
    Final synthesis object for a CIO decision iteration.
    Acts as the handoff between the Decision Assembler and the Action Classifier.
    """

    # 1. Hard Gate Flags
    hard_blocked: bool
    hard_block_reason: str | None = None

    # 2. EV and Cost Analysis
    ev_passes: bool
    cost_viable: bool
    net_ev_usd: float | None = None
    total_cost_usd: float | None = None

    # 3. LLM Classification Results
    regime_confidence: ConfidenceLevel
    regime_fit: RegimeFit
    strategy_health: HealthStatus
    activation_recommendation: ActivationRecommendation

    # 4. Parameter Change
    param_change: AppliedParamChange | None = None

    # 5. Final Trade Parameters
    computed_position_size_usd: float | None = None
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None
    leverage: float = 1.0
    order_type: OrderType = OrderType.LIMIT
    split_order: bool = False
    entry_offset_pct: float = 0.0

    # 6. Warnings
    risk_warnings: list[str] = Field(default_factory=list)

    # 7. Final Action
    action: ActionType | None = None
    justification: str | None = None
    thought_trace: str | None = None


# SAFE_DEFAULTS: Authoritative source for parse failure fallbacks
SAFE_DEFAULTS: dict[str, BaseModel] = {
    "PETROSA_PROMPT_REGIME_CLASSIFIER": RegimeResult(
        regime=RegimeEnum.CHOPPY,
        regime_confidence=ConfidenceLevel.LOW,
        volatility_level=VolatilityLevel.MEDIUM,
        primary_signal="FALLBACK",
        thought_trace="PARSE_FAILURE",
    ),
    "PETROSA_PROMPT_STRATEGY_ASSESSOR": StrategyResult(
        health=HealthStatus.FAILING,
        regime_fit=RegimeFit.POOR,
        activation_recommendation=ActivationRecommendation.PAUSE,
        param_change=None,
        thought_trace="PARSE_FAILURE",
    ),
    "PETROSA_PROMPT_ACTION_CLASSIFIER": ActionResult(
        action=ActionType.SKIP,
        justification="Action skipped: classifier parse failure",
        thought_trace="PARSE_FAILURE",
    ),
}

# Top-level fallback for the Orchestrator
SAFE_DECISION_RESULT = DecisionResult(
    hard_blocked=False,
    ev_passes=False,
    cost_viable=False,
    regime_confidence=ConfidenceLevel.LOW,
    regime_fit=RegimeFit.NEUTRAL,
    strategy_health=HealthStatus.HEALTHY,
    activation_recommendation=ActivationRecommendation.RUN,
    action=ActionType.SKIP,
    justification="Critical failure: returning safe default decision",
    thought_trace="SYSTEM_ERROR",
)

TIMEOUT_RETRY_RESULT = DecisionResult(
    hard_blocked=False,
    ev_passes=False,
    cost_viable=False,
    regime_confidence=ConfidenceLevel.LOW,
    regime_fit=RegimeFit.NEUTRAL,
    strategy_health=HealthStatus.HEALTHY,
    activation_recommendation=ActivationRecommendation.RUN,
    action=ActionType.RETRY_SAFE,
    justification="TIMEOUT_GUARD: Audit exceeded 200ms limit. Safe-failing to RETRY_SAFE.",
    thought_trace="TIMEOUT_ENFORCEMENT",
)

CRITICAL_FAILURE_RESULT = DecisionResult(
    hard_blocked=True,
    ev_passes=False,
    cost_viable=False,
    regime_confidence=ConfidenceLevel.LOW,
    regime_fit=RegimeFit.NEUTRAL,
    strategy_health=HealthStatus.FAILING,
    activation_recommendation=ActivationRecommendation.PAUSE,
    action=ActionType.FAIL_SAFE,
    justification="CRITICAL_FAILURE: System error or unhandled exception. Safe-failing to FAIL_SAFE.",
    thought_trace="CRITICAL_FAILURE_ENFORCEMENT",
)
