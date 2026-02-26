from datetime import datetime
from enum import Enum, StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class SignalType(StrEnum):
    """Signal types for trading actions"""

    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"
    CLOSE = "close"


class SignalStrength(StrEnum):
    """Signal strength levels"""

    WEAK = "weak"
    MEDIUM = "medium"
    STRONG = "strong"
    EXTREME = "extreme"


class TimeFrame(StrEnum):
    """Trading timeframes for signal analysis"""

    TICK = "tick"  # Real-time tick data
    MINUTE_1 = "1m"  # 1 minute
    MINUTE_3 = "3m"  # 3 minutes
    MINUTE_5 = "5m"  # 5 minutes
    MINUTE_15 = "15m"  # 15 minutes
    MINUTE_30 = "30m"  # 30 minutes
    HOUR_1 = "1h"  # 1 hour
    HOUR_2 = "2h"  # 2 hours
    HOUR_4 = "4h"  # 4 hours
    HOUR_6 = "6h"  # 6 hours
    HOUR_8 = "8h"  # 8 hours
    HOUR_12 = "12h"  # 12 hours
    DAY_1 = "1d"  # 1 day
    DAY_3 = "3d"  # 3 days
    WEEK_1 = "1w"  # 1 week
    MONTH_1 = "1M"  # 1 month


class OrderType(StrEnum):
    """Supported order types"""

    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"
    TAKE_PROFIT = "take_profit"
    TAKE_PROFIT_LIMIT = "take_profit_limit"
    CONDITIONAL_LIMIT = "conditional_limit"  # Execute if price crosses level
    CONDITIONAL_STOP = "conditional_stop"  # Execute if price crosses stop level


class TimeInForce(StrEnum):
    """Order time in force options"""

    GTC = "GTC"  # Good Till Canceled
    IOC = "IOC"  # Immediate or Cancel
    FOK = "FOK"  # Fill or Kill
    GTX = "GTX"  # Good Till Crossing


class StrategyMode(StrEnum):
    """Strategy processing modes"""

    DETERMINISTIC = "deterministic"  # Rule-based processing
    ML_LIGHT = "ml_light"  # Light ML models
    LLM_REASONING = "llm_reasoning"  # Full LLM reasoning


class Signal(BaseModel):
    """Enhanced trading signal with advanced features"""

    # Core signal information
    id: str | None = Field(None, description="Unique identifier for this signal")
    strategy_id: str = Field(..., description="Unique identifier for the strategy")
    signal_id: str | None = Field(None, description="Unique identifier for this signal")
    strategy_mode: StrategyMode = Field(
        StrategyMode.DETERMINISTIC, description="Processing mode for this signal"
    )

    # Trading parameters
    symbol: str = Field(..., description="Trading symbol (e.g., BTCUSDT)")
    signal_type: SignalType | None = Field(
        None, description="Signal type (buy/sell/hold/close) - deprecated, use action"
    )
    action: Literal["buy", "sell", "hold", "close"] = Field(
        ..., description="Trading action"
    )
    confidence: float = Field(..., ge=0, le=1, description="Signal confidence (0-1)")
    strength: SignalStrength = Field(
        SignalStrength.MEDIUM, description="Signal strength level"
    )

    # Price and quantity information
    price: float = Field(..., description="Signal price")
    quantity: float = Field(..., description="Signal quantity")
    current_price: float = Field(..., description="Current market price")
    target_price: float | None = Field(None, description="Target execution price")

    # Source and metadata
    source: str = Field(..., description="Signal source")
    strategy: str = Field(..., description="Strategy name")
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="Additional metadata"
    )

    # Timeframe information
    timeframe: str = Field("1h", description="Timeframe used for signal analysis")

    # Order configuration
    order_type: OrderType = Field(OrderType.MARKET, description="Order type to execute")
    time_in_force: TimeInForce = Field(
        TimeInForce.GTC, description="Order time in force"
    )
    position_size_pct: float | None = Field(
        None, ge=0, le=1, description="Position size as percentage of portfolio"
    )
    quote_quantity: float | None = Field(
        None, description="Quote quantity for quote-based orders"
    )

    # Risk management
    stop_loss: float | None = Field(None, description="Stop loss price")
    stop_loss_pct: float | None = Field(
        None, ge=0, le=1, description="Stop loss as percentage"
    )
    take_profit: float | None = Field(None, description="Take profit price")
    take_profit_pct: float | None = Field(
        None, ge=0, le=1, description="Take profit as percentage"
    )

    # Conditional orders
    conditional_price: float | None = Field(
        None, description="Price level for conditional execution"
    )
    conditional_direction: Literal["above", "below"] | None = Field(
        None, description="Direction for conditional execution"
    )
    conditional_timeout: int | None = Field(
        None, description="Timeout in seconds for conditional orders"
    )

    # Advanced order features
    iceberg_quantity: float | None = Field(
        None, description="Iceberg quantity for iceberg orders"
    )
    client_order_id: str | None = Field(None, description="Client-provided order ID")

    # ML/LLM specific fields
    model_confidence: float | None = Field(
        None, ge=0, le=1, description="ML model confidence score"
    )
    model_features: dict[str, Any] | None = Field(
        None, description="Features used by ML model"
    )
    llm_reasoning: str | None = Field(None, description="LLM reasoning for the signal")
    llm_alternatives: list[dict[str, Any]] | None = Field(
        None, description="Alternative actions considered by LLM"
    )

    # Market indicators
    indicators: dict[str, Any] | None = Field(
        None, description="Technical indicators and market data"
    )

    # Metadata
    rationale: str | None = Field(
        None, description="Human-readable rationale for the signal"
    )
    meta: dict[str, Any] = Field(
        default_factory=dict, description="Additional metadata"
    )

    # Timestamp
    timestamp: datetime = Field(
        default_factory=datetime.utcnow, description="Signal timestamp"
    )

    @field_validator("timestamp", mode="before")
    @classmethod
    def validate_timestamp(cls, v: Any) -> datetime:
        """Ensure timestamp is timezone-aware"""
        if isinstance(v, str):
            # Parse ISO format string
            try:
                dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
                return dt
            except ValueError:
                # Try parsing as Unix timestamp
                try:
                    timestamp_float = float(v)
                    # Validate it's a reasonable Unix timestamp (after year 2000, before year 2100)
                    if 946684800 <= timestamp_float <= 4102444800:
                        return datetime.fromtimestamp(timestamp_float)
                    else:
                        # Invalid timestamp, log warning and use current time
                        import logging

                        logger = logging.getLogger(__name__)
                        logger.warning(
                            f"Invalid timestamp value '{v}' - using current time. "
                            f"Timestamp should be ISO format string or Unix timestamp."
                        )
                        return datetime.utcnow()
                except (ValueError, TypeError):
                    # Can't parse as float either, log warning and use current time
                    import logging

                    logger = logging.getLogger(__name__)
                    logger.warning(
                        f"Invalid timestamp format '{v}' - using current time. "
                        f"Timestamp should be ISO format string or Unix timestamp."
                    )
                    return datetime.utcnow()
        elif isinstance(v, int | float):
            # Unix timestamp - validate range
            if 946684800 <= v <= 4102444800:
                return datetime.fromtimestamp(v)
            else:
                import logging

                logger = logging.getLogger(__name__)
                logger.warning(
                    f"Unix timestamp {v} out of valid range - using current time"
                )
                return datetime.utcnow()
        elif isinstance(v, datetime):
            return v
        else:
            import logging

            logger = logging.getLogger(__name__)
            logger.warning(f"Invalid timestamp type {type(v)} - using current time")
            return datetime.utcnow()

    @field_validator("confidence", "model_confidence")
    @classmethod
    def validate_confidence(cls, v: Any) -> float | None:
        """Validate confidence values"""
        if v is not None and (v < 0 or v > 1):
            raise ValueError("Confidence must be between 0 and 1")
        return float(v) if v is not None else None

    @field_validator("position_size_pct", "stop_loss_pct", "take_profit_pct")
    @classmethod
    def validate_percentages(cls, v: Any) -> float | None:
        """Validate percentage values"""
        if v is not None and (v < 0 or v > 1):
            raise ValueError("Percentage must be between 0 and 1")
        return float(v) if v is not None else None

    model_config = {
        "json_encoders": {datetime: lambda v: v.isoformat()},
        "protected_namespaces": (),
    }
