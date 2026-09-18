import logging
from enum import Enum

logger = logging.getLogger(__name__)


class ServiceType(Enum):
    TA_BOT = "ta-bot"
    REALTIME_STRATEGIES = "realtime-strategies"
    # petrosa-cio#200: explicit "no known owner" outcome. Callers MUST handle
    # this distinctly from TA_BOT — silently defaulting an unrecognised
    # strategy to a concrete service can route freeze/pause actions at the
    # wrong service.
    UNKNOWN = "unknown"


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

    # petrosa-cio#200: display-name / alias -> canonical strategy id.
    # Normalisation (lowercase, strip, collapse whitespace to "_") is applied
    # BEFORE this lookup, so e.g. "Iceberg Order Detector" normalises to
    # "iceberg_order_detector" and is then aliased to "iceberg_detector".
    # Add new entries here rather than relying on normalisation alone —
    # normalisation cannot fix ids that don't already textually resemble
    # their canonical form (e.g. abbreviations or renames).
    STRATEGY_ID_ALIASES: dict[str, str] = {
        "iceberg_order_detector": "iceberg_detector",
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

    @staticmethod
    def _normalize(strategy_id: str) -> str:
        """Lowercase, strip, and collapse internal whitespace to '_'.

        petrosa-cio#200: producers sometimes emit a human display name
        ("Iceberg Order Detector") instead of the canonical snake_case id
        ("iceberg_detector"). Normalising first means exact-match lookups
        against already-canonical ids (which are already lowercase
        snake_case) are unaffected — this is a no-op for them.
        """
        return "_".join(strategy_id.strip().lower().split())

    @classmethod
    def resolve(cls, strategy_id: str) -> ServiceType:
        """
        Maps a strategy ID to the service responsible for it.

        The incoming identifier is normalised (lowercase, strip, whitespace
        collapsed to '_') and passed through the alias map before lookup, so
        display names and known aliases resolve to the same ServiceType as
        their canonical id (petrosa-cio#200).

        Returns ServiceType.UNKNOWN — never a silent default — when the
        (normalised, aliased) id is not registered in either service's
        strategy set. Callers MUST handle UNKNOWN explicitly rather than
        assuming a concrete service.
        """
        normalized = cls._normalize(strategy_id) if strategy_id else ""
        canonical = cls.STRATEGY_ID_ALIASES.get(normalized, normalized)

        if canonical in cls.REALTIME_SERVICE_STRATEGIES:
            return ServiceType.REALTIME_STRATEGIES

        if canonical in cls.TA_BOT_SERVICE_STRATEGIES:
            return ServiceType.TA_BOT

        logger.warning(
            "Unknown strategy_id '%s' (normalized '%s') not found in any "
            "service registry. Returning ServiceType.UNKNOWN — caller must "
            "handle explicitly. Verify this strategy exists.",
            strategy_id,
            canonical,
        )
        return ServiceType.UNKNOWN
