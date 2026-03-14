import logging
from enum import Enum

logger = logging.getLogger(__name__)


class ServiceType(Enum):
    TA_BOT = "ta-bot"
    REALTIME_STRATEGIES = "realtime-strategies"


class TargetServiceResolver:
    """
    Resolves which strategy service (TA-bot or Realtime-Strategies) should handle
    a specific strategy ID.
    """

    # Strategies managed by the petrosa-realtime-strategies service
    REALTIME_SERVICE_STRATEGIES: set[str] = {
        "orderbook_skew",
        "trade_momentum",
        "ticker_velocity",
        "btc_dominance",
        "onchain_metrics",
        "iceberg_detector",
    }

    # Strategies managed by the petrosa-bot-ta-analysis service (27 total)
    TA_BOT_SERVICE_STRATEGIES: set[str] = {
        "band_fade_reversal",
        "bear_trap_buy",
        "bear_trap_sell",
        "bollinger_breakout_signals",
        "bollinger_squeeze_alert",
        "divergence_trap",
        "doji_reversal",
        "ema_alignment_bearish",
        "ema_alignment_bullish",
        "ema_momentum_reversal",
        "ema_pullback_continuation",
        "ema_slope_reversal_sell",
        "fox_trap_reversal",
        "golden_trend_sync",
        "hammer_reversal_pattern",
        "ichimoku_cloud_momentum",
        "inside_bar_breakout",
        "inside_bar_sell",
        "liquidity_grab_reversal",
        "mean_reversion_scalper",
        "minervini_trend_template",
        "momentum_pulse",
        "multi_timeframe_trend_continuation",
        "range_break_pop",
        "rsi_extreme_reversal",
        "shooting_star_reversal",
        "volume_surge_breakout",
    }

    @classmethod
    def resolve(cls, strategy_id: str) -> ServiceType:
        """
        Maps a strategy ID to the service responsible for it.
        Defaults to TA-bot if the ID is unknown.
        """
        if strategy_id in cls.REALTIME_SERVICE_STRATEGIES:
            return ServiceType.REALTIME_STRATEGIES

        if strategy_id not in cls.TA_BOT_SERVICE_STRATEGIES:
            logger.warning(
                "Unknown strategy_id '%s' not found in any service registry. "
                "Defaulting to TA_BOT. Verify this strategy exists.",
                strategy_id,
            )

        return ServiceType.TA_BOT
