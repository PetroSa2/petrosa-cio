from typing import Any

from pydantic import BaseModel, Field

from cio.apps.nurse.models import RiskLimits
from cio.models.enums import PnlTrend, TriggerType, VolatilityLevel
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
