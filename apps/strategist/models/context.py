from typing import Dict, List, Optional, Any
from pydantic import BaseModel, Field

from .enums import TriggerType, VolatilityLevel, RegimeEnum
from .regime import RegimeResult

class RiskLimits(BaseModel):
    """Hard risk limits enforced by the Code Engine."""
    max_drawdown_pct: float
    max_orders_global: int
    max_orders_per_symbol: int
    max_position_size_usd: float

class StrategyStats(BaseModel):
    """Historical and real-time performance metrics for a strategy."""
    win_rate: Optional[float] = None
    avg_win_usd: Optional[float] = None
    avg_loss_usd: Optional[float] = None
    win_rate_delta: float
    consecutive_losses: int
    recent_pnl_trend: str

class PortfolioSummary(BaseModel):
    """Aggregate portfolio state for exposure and concentration analysis."""
    net_directional_exposure: float  # 0.0 - 1.0
    same_asset_pct: float            # 0.0 - 1.0
    open_positions_count: int

class TriggerContext(BaseModel):
    """
    Complete context object for a reasoning loop iteration.
    Assembled by the Context Builder before any persona runs.
    """
    # Orchestration and Routing (Winston's requirements)
    correlation_id: str = Field(..., description="NATS message ID or unique GUID for this request flow")
    source_subject: str = Field(..., description="Original NATS subject the trigger was received on")
    
    # Trigger Data
    trigger_type: TriggerType
    trigger_payload: Dict[str, Any]
    
    # Environment State
    regime: RegimeResult
    volatility_level: VolatilityLevel
    
    # Strategy Context
    strategy_id: str
    strategy_stats: StrategyStats
    strategy_defaults: Dict[str, Any]
    
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
    historical_context: Optional[str] = None
