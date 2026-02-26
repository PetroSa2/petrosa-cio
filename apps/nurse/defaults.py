"""
Trading Configuration Defaults, Schemas, and Validation.

This module defines all default trading parameters, their validation rules,
and schemas for LLM-friendly configuration discovery.

All parameters are documented with:
- Name and type
- Description (LLM-friendly)
- Default value
- Validation rules
- Examples
- Impact description
"""

from typing import Any

# =============================================================================
# DEFAULT TRADING PARAMETERS
# =============================================================================

DEFAULT_TRADING_PARAMETERS = {
    # -------------------------------------------------------------------------
    # Order Execution Parameters
    # -------------------------------------------------------------------------
    "leverage": 10,
    "margin_type": "isolated",
    "default_order_type": "market",
    "time_in_force": "GTC",
    "position_mode": "hedge",
    # -------------------------------------------------------------------------
    # Position Sizing Parameters
    # -------------------------------------------------------------------------
    "position_size_pct": 0.1,  # 10% of portfolio
    "max_position_size_usd": 1000.0,
    "min_position_size_usd": 10.0,
    "quantity_multiplier": 1.0,
    "use_exchange_minimums": True,
    "override_min_notional": None,
    "override_min_qty": None,
    "override_step_size": None,
    # -------------------------------------------------------------------------
    # Risk Management Parameters
    # -------------------------------------------------------------------------
    "stop_loss_pct": 2.0,
    "take_profit_pct": 5.0,
    "max_daily_loss_pct": 0.05,
    "max_portfolio_exposure_pct": 0.8,
    "max_daily_trades": 100,
    "max_concurrent_positions": 10,
    "risk_management_enabled": True,
    # -------------------------------------------------------------------------
    # Signal Processing Parameters
    # -------------------------------------------------------------------------
    "signal_conflict_resolution": "strongest_wins",
    "timeframe_conflict_resolution": "higher_timeframe_wins",
    "max_signal_age_seconds": 300,
    "min_confidence_threshold": 0.5,
    # -------------------------------------------------------------------------
    # Strategy Weights (default weights for conflict resolution)
    # -------------------------------------------------------------------------
    "strategy_weights": {
        "momentum_strategy": 1.0,
        "mean_reversion_strategy": 0.8,
        "ml_strategy": 1.2,
        "llm_strategy": 1.5,
        "default": 1.0,
    },
    # -------------------------------------------------------------------------
    # Timeframe Weights (for timeframe-based conflict resolution)
    # -------------------------------------------------------------------------
    "timeframe_weights": {
        "tick": 0.3,
        "1m": 0.5,
        "3m": 0.6,
        "5m": 0.7,
        "15m": 0.8,
        "30m": 0.9,
        "1h": 1.0,
        "2h": 1.1,
        "4h": 1.2,
        "6h": 1.3,
        "8h": 1.4,
        "12h": 1.5,
        "1d": 1.6,
        "3d": 1.7,
        "1w": 1.8,
        "1M": 2.0,
    },
    # -------------------------------------------------------------------------
    # Advanced Options
    # -------------------------------------------------------------------------
    "enabled": True,
    "enable_shorts": True,
    "enable_longs": True,
    "slippage_tolerance_pct": 0.1,
    "max_retries": 3,
}


# =============================================================================
# PARAMETER SCHEMA (LLM-Friendly Documentation)
# =============================================================================

PARAMETER_SCHEMA = {
    # -------------------------------------------------------------------------
    # Order Execution Parameters
    # -------------------------------------------------------------------------
    "leverage": {
        "type": "integer",
        "description": (
            "Leverage multiplier for futures trading. Higher leverage amplifies "
            "both profits and losses. Use lower leverage (1-5x) for conservative "
            "trading, medium leverage (5-20x) for balanced risk, and high leverage "
            "(20-125x) for aggressive strategies. **Note**: Binance may limit "
            "maximum leverage based on position size and account tier."
        ),
        "default": 10,
        "min": 1,
        "max": 125,
        "example": 10,
        "impact": (
            "Directly affects position size and risk. Higher leverage = larger "
            "positions with same capital, but higher liquidation risk."
        ),
        "when_to_change": (
            "Reduce during high volatility or uncertain market conditions. "
            "Increase during strong trending markets with clear signals."
        ),
    },
    "margin_type": {
        "type": "string",
        "description": (
            "Type of margin mode for futures positions. 'isolated' means each "
            "position has its own margin (limited risk to that position only). "
            "'cross' means all positions share account balance (lower liquidation "
            "risk but can lose entire account). **Recommended**: Use 'isolated' "
            "for most trading to limit risk."
        ),
        "default": "isolated",
        "allowed_values": ["isolated", "cross"],
        "example": "isolated",
        "impact": (
            "Determines how margin is allocated and liquidation is calculated. "
            "Isolated protects other positions if one gets liquidated."
        ),
        "when_to_change": (
            "Use 'isolated' for high-risk trades or testing. Use 'cross' only "
            "when managing portfolio-level risk with high confidence."
        ),
    },
    "default_order_type": {
        "type": "string",
        "description": (
            "Default order type for trade execution. 'market' executes immediately "
            "at best available price (guaranteed fill, possible slippage). 'limit' "
            "executes only at specified price or better (no slippage, may not fill). "
            "'stop' and 'take_profit' are conditional orders."
        ),
        "default": "market",
        "allowed_values": [
            "market",
            "limit",
            "stop",
            "stop_limit",
            "take_profit",
            "take_profit_limit",
        ],
        "example": "market",
        "impact": (
            "Affects order execution speed and price. Market orders fill faster "
            "but may have slippage. Limit orders have better price control."
        ),
        "when_to_change": (
            "Use 'market' for fast-moving markets or urgent trades. Use 'limit' "
            "when precise entry price is critical and time is not urgent."
        ),
    },
    "time_in_force": {
        "type": "string",
        "description": (
            "How long limit orders remain active. 'GTC' (Good Till Cancel) stays "
            "active until filled or manually cancelled. 'IOC' (Immediate Or Cancel) "
            "fills immediately and cancels remainder. 'FOK' (Fill Or Kill) must "
            "fill entirely or cancel completely."
        ),
        "default": "GTC",
        "allowed_values": ["GTC", "IOC", "FOK", "GTX"],
        "example": "GTC",
        "impact": (
            "Controls order persistence. GTC gives flexibility, IOC/FOK are for "
            "immediate execution needs."
        ),
        "when_to_change": (
            "Use GTC for normal trading. Use IOC when you want partial fills. "
            "Use FOK when you need all-or-nothing execution."
        ),
    },
    "position_mode": {
        "type": "string",
        "description": (
            "Position mode for futures trading. 'hedge' allows simultaneous LONG "
            "and SHORT positions on same symbol (for hedging strategies). 'one-way' "
            "allows only one direction at a time. **Note**: This is account-level "
            "setting on Binance, must match exchange configuration."
        ),
        "default": "hedge",
        "allowed_values": ["hedge", "one-way"],
        "example": "hedge",
        "impact": (
            "Determines if you can hold both LONG and SHORT positions. Hedge mode "
            "enables more complex strategies but requires careful management."
        ),
        "when_to_change": (
            "Use 'hedge' for sophisticated strategies or market-neutral approaches. "
            "Use 'one-way' for simpler directional trading."
        ),
    },
    # -------------------------------------------------------------------------
    # Position Sizing Parameters
    # -------------------------------------------------------------------------
    "position_size_pct": {
        "type": "float",
        "description": (
            "Percentage of total portfolio to allocate for each position. "
            "0.1 = 10% of portfolio per trade. Smaller values (0.01-0.05) are "
            "conservative, medium values (0.05-0.15) are balanced, large values "
            "(0.15+) are aggressive. **Critical**: Lower values = better "
            "risk management and diversification."
        ),
        "default": 0.1,
        "min": 0.001,
        "max": 1.0,
        "example": 0.1,
        "impact": (
            "Directly determines position size. Higher percentage = larger positions "
            "= more risk and potential reward per trade."
        ),
        "when_to_change": (
            "Reduce during uncertain markets or when testing new strategies. "
            "Increase during high-confidence setups or strong trending markets."
        ),
    },
    "max_position_size_usd": {
        "type": "float",
        "description": (
            "Maximum position size in USD regardless of portfolio percentage. "
            "Acts as safety cap to prevent oversized positions. Set based on your "
            "risk tolerance and account size. **Example**: $1000 cap prevents any "
            "single position from exceeding $1000 notional value."
        ),
        "default": 1000.0,
        "min": 1.0,
        "max": 1000000.0,
        "example": 1000.0,
        "impact": (
            "Hard limit on position size. Protects against calculation errors or "
            "extreme portfolio percentage allocations."
        ),
        "when_to_change": (
            "Increase as account grows or for high-conviction trades. Decrease "
            "when reducing risk or trading volatile assets."
        ),
    },
    "min_position_size_usd": {
        "type": "float",
        "description": (
            "Minimum position size in USD (user preference). This is YOUR minimum, "
            "separate from exchange minimums. Helps avoid tiny positions that aren't "
            "worth trading fees. Set above exchange minimums for efficiency. "
            "**Note**: Exchange MIN_NOTIONAL is fetched automatically."
        ),
        "default": 10.0,
        "min": 1.0,
        "max": 10000.0,
        "example": 10.0,
        "impact": (
            "Prevents opening positions too small to be profitable after fees. "
            "Higher values reduce noise trades."
        ),
        "when_to_change": (
            "Increase to focus on larger, more significant trades. Decrease to "
            "allow more granular position sizing."
        ),
    },
    "quantity_multiplier": {
        "type": "float",
        "description": (
            "Multiplier applied to calculated position quantity. 1.0 = normal size, "
            "0.5 = half size, 2.0 = double size. Useful for quickly scaling all "
            "positions up or down without changing percentage parameters. **Example**: "
            "Use 0.5 to trade more conservatively, 1.5 for more aggressive."
        ),
        "default": 1.0,
        "min": 0.1,
        "max": 10.0,
        "example": 1.0,
        "impact": (
            "Scales all calculated position sizes. Quick way to adjust global "
            "risk exposure."
        ),
        "when_to_change": (
            "Reduce during high volatility or drawdowns. Increase during strong "
            "performance periods or favorable market conditions."
        ),
    },
    "use_exchange_minimums": {
        "type": "boolean",
        "description": (
            "Whether to automatically fetch and use real-time exchange minimums "
            "(MIN_NOTIONAL, LOT_SIZE, step_size) from Binance API. **Recommended**: "
            "Keep as true to ensure compliance with current exchange rules. Set to "
            "false only if you want to use manual overrides."
        ),
        "default": True,
        "example": True,
        "impact": (
            "When true, system fetches live exchange data before each trade. "
            "When false, uses override values (must be set manually)."
        ),
        "when_to_change": (
            "Keep as true for normal operation. Set to false only for testing or "
            "if you have specific minimum requirements."
        ),
    },
    "override_min_notional": {
        "type": "float",
        "description": (
            "Manual override for minimum notional value (position size in USD). "
            "Only used if use_exchange_minimums=false. **Warning**: Setting too low "
            "will cause order rejections. Check Binance for current MIN_NOTIONAL per "
            "symbol (typically $5-$100)."
        ),
        "default": None,
        "min": 1.0,
        "max": 1000.0,
        "example": 100.0,
        "impact": (
            "Overrides automatic minimum. Can cause trade failures if set incorrectly."
        ),
        "when_to_change": (
            "Only set if you know the correct value and have disabled automatic "
            "fetching. Not recommended for normal use."
        ),
    },
    "override_min_qty": {
        "type": "float",
        "description": (
            "Manual override for minimum order quantity (base asset). Only used if "
            "use_exchange_minimums=false. Must match Binance's LOT_SIZE filter. "
            "**Example**: BTCUSDT typically has min_qty = 0.001 BTC."
        ),
        "default": None,
        "min": 0.000001,
        "max": 1000.0,
        "example": 0.001,
        "impact": (
            "Sets minimum order quantity. Incorrect value causes order rejections."
        ),
        "when_to_change": (
            "Only set if you know the correct value and have disabled automatic "
            "fetching. Not recommended for normal use."
        ),
    },
    "override_step_size": {
        "type": "float",
        "description": (
            "Manual override for quantity step size (precision). Only used if "
            "use_exchange_minimums=false. Must match Binance's LOT_SIZE step_size. "
            "**Example**: BTCUSDT typically has step_size = 0.001 (3 decimal places)."
        ),
        "default": None,
        "min": 0.000001,
        "max": 1.0,
        "example": 0.001,
        "impact": (
            "Controls quantity precision. Incorrect value causes order rejections "
            "due to precision errors."
        ),
        "when_to_change": (
            "Only set if you know the correct value and have disabled automatic "
            "fetching. Not recommended for normal use."
        ),
    },
    # -------------------------------------------------------------------------
    # Risk Management Parameters
    # -------------------------------------------------------------------------
    "stop_loss_pct": {
        "type": "float",
        "description": (
            "Default stop loss percentage from entry price. 2.0 = 2% loss triggers "
            "stop. Applies automatically to positions unless overridden by signal. "
            "Lower values (0.5-1%) are tight stops, medium (1-3%) are balanced, "
            "higher (3-5%+) give positions more room. **Critical**: Always use "
            "stop losses to limit downside risk."
        ),
        "default": 2.0,
        "min": 0.1,
        "max": 50.0,
        "example": 2.0,
        "impact": (
            "Determines maximum loss per position. Tighter stops limit losses but "
            "increase chance of premature exit."
        ),
        "when_to_change": (
            "Tighten (reduce) for volatile assets or high leverage. Widen (increase) "
            "for less volatile assets or when using technical support/resistance."
        ),
    },
    "take_profit_pct": {
        "type": "float",
        "description": (
            "Default take profit percentage from entry price. 5.0 = 5% gain triggers "
            "exit. Applies automatically unless overridden. Should be larger than "
            "stop_loss_pct for positive risk/reward ratio. **Example**: 2% stop "
            "with 5% take profit = 2.5:1 reward/risk ratio."
        ),
        "default": 5.0,
        "min": 0.1,
        "max": 100.0,
        "example": 5.0,
        "impact": (
            "Determines profit target per position. Higher targets capture bigger "
            "moves but may not fill. Lower targets lock in profits faster."
        ),
        "when_to_change": (
            "Increase during strong trends to capture larger moves. Decrease during "
            "choppy markets to lock in profits faster."
        ),
    },
    "max_daily_loss_pct": {
        "type": "float",
        "description": (
            "Maximum allowable portfolio loss per day. 0.05 = 5% daily loss limit. "
            "When reached, system stops opening new positions for the day. **Critical**: "
            "Protects against catastrophic daily losses and emotional over-trading. "
            "Typical values: 2-5% for conservative, 5-10% for aggressive."
        ),
        "default": 0.05,
        "min": 0.001,
        "max": 0.5,
        "example": 0.05,
        "impact": (
            "Acts as daily circuit breaker. Prevents compounding losses during bad "
            "trading days."
        ),
        "when_to_change": (
            "Reduce during learning phases or high volatility. Increase only with "
            "proven strategy and strong risk management."
        ),
    },
    "max_portfolio_exposure_pct": {
        "type": "float",
        "description": (
            "Maximum total portfolio exposure across all positions. 0.8 = 80% of "
            "portfolio can be in active positions. Prevents over-allocation and "
            "maintains cash reserves. **Example**: With $10,000 portfolio and 0.8 limit, "
            "maximum $8,000 can be in positions, $2,000 stays as reserve."
        ),
        "default": 0.8,
        "min": 0.1,
        "max": 1.0,
        "example": 0.8,
        "impact": (
            "Controls total market exposure. Lower values maintain more cash reserves "
            "for opportunities or margin calls."
        ),
        "when_to_change": (
            "Reduce during uncertain markets or high volatility. Increase during "
            "strong trending markets with many opportunities."
        ),
    },
    "max_daily_trades": {
        "type": "integer",
        "description": (
            "Maximum number of trades allowed per day. Prevents over-trading and "
            "excessive transaction costs. **Example**: 100 trades/day allows active "
            "trading but prevents runaway execution. Lower values (10-50) encourage "
            "selective trading, higher values (100-500) allow high-frequency approaches."
        ),
        "default": 100,
        "min": 1,
        "max": 10000,
        "example": 100,
        "impact": (
            "Limits trading frequency. Lower values reduce costs and force selectivity. "
            "Higher values enable more responsive trading."
        ),
        "when_to_change": (
            "Reduce for longer-term strategies or to control costs. Increase for "
            "high-frequency or scalping strategies."
        ),
    },
    "max_concurrent_positions": {
        "type": "integer",
        "description": (
            "Maximum number of open positions at any time. Controls portfolio "
            "diversification and management complexity. Lower values (3-5) allow "
            "focused trading, medium values (5-15) balance diversification, higher "
            "values (15+) enable portfolio approaches. **Note**: More positions = "
            "more management overhead."
        ),
        "default": 10,
        "min": 1,
        "max": 100,
        "example": 10,
        "impact": (
            "Controls diversification vs. focus. Fewer positions = concentrated risk, "
            "more positions = spread risk."
        ),
        "when_to_change": (
            "Reduce for high-conviction strategies or large position sizes. Increase "
            "for diversified portfolio approaches."
        ),
    },
    "risk_management_enabled": {
        "type": "boolean",
        "description": (
            "Master switch for all risk management checks (stop loss, take profit, "
            "daily loss limits, exposure limits). **Warning**: Disabling removes all "
            "safety guardrails. Only disable for testing or if implementing custom "
            "risk management. **Recommended**: Always keep enabled in production."
        ),
        "default": True,
        "example": True,
        "impact": (
            "When enabled, enforces all risk limits. When disabled, all risk checks "
            "are bypassed."
        ),
        "when_to_change": (
            "Keep enabled for all production trading. Only disable for backtesting "
            "or custom risk implementations."
        ),
    },
    # -------------------------------------------------------------------------
    # Signal Processing Parameters
    # -------------------------------------------------------------------------
    "signal_conflict_resolution": {
        "type": "string",
        "description": (
            "How to resolve conflicting signals from multiple strategies. "
            "'strongest_wins' uses signal with highest confidence. 'first_come_first_served' "
            "uses earliest signal. 'weighted_average' combines signals using strategy "
            "weights. 'manual_review' pauses for manual decision. **Recommended**: "
            "Use 'strongest_wins' for most scenarios."
        ),
        "default": "strongest_wins",
        "allowed_values": [
            "strongest_wins",
            "first_come_first_served",
            "weighted_average",
            "manual_review",
        ],
        "example": "strongest_wins",
        "impact": (
            "Determines which signal is executed when multiple signals conflict. "
            "Affects strategy performance and behavior."
        ),
        "when_to_change": (
            "Use 'strongest_wins' for confidence-based approaches. Use 'weighted_average' "
            "for ensemble strategies. Use 'first_come_first_served' for time-priority."
        ),
    },
    "timeframe_conflict_resolution": {
        "type": "string",
        "description": (
            "How to resolve signals from different timeframes. 'higher_timeframe_wins' "
            "prioritizes daily over hourly over minute signals. 'lower_timeframe_wins' "
            "does opposite. 'weighted_average' combines using timeframe weights. "
            "**Recommended**: Use 'higher_timeframe_wins' as higher timeframes typically "
            "have stronger trends."
        ),
        "default": "higher_timeframe_wins",
        "allowed_values": [
            "higher_timeframe_wins",
            "lower_timeframe_wins",
            "weighted_average",
        ],
        "example": "higher_timeframe_wins",
        "impact": (
            "Determines priority when signals from different timeframes conflict. "
            "Higher timeframes = longer-term view, lower timeframes = short-term."
        ),
        "when_to_change": (
            "Use 'higher_timeframe_wins' for trend-following. Use 'lower_timeframe_wins' "
            "for scalping or quick trades."
        ),
    },
    "max_signal_age_seconds": {
        "type": "integer",
        "description": (
            "Maximum age of signal before it's considered stale and ignored. "
            "300 seconds = 5 minutes. Prevents acting on outdated market analysis. "
            "Lower values (60-300) for fast markets, higher values (300-600) for "
            "slower strategies. **Critical**: Stale signals can lead to bad entries."
        ),
        "default": 300,
        "min": 1,
        "max": 3600,
        "example": 300,
        "impact": (
            "Filters out old signals. Lower values require faster signal generation "
            "and processing."
        ),
        "when_to_change": (
            "Reduce for high-frequency strategies or volatile markets. Increase for "
            "longer-term strategies or slower signal generation."
        ),
    },
    "min_confidence_threshold": {
        "type": "float",
        "description": (
            "Minimum signal confidence required to execute trade. 0.5 = 50% confidence "
            "minimum. Signals below threshold are ignored. Higher values (0.7-0.9) "
            "are selective, lower values (0.3-0.5) are permissive. **Balance**: "
            "Higher threshold = fewer but higher-quality trades."
        ),
        "default": 0.5,
        "min": 0.0,
        "max": 1.0,
        "example": 0.5,
        "impact": (
            "Filters signals by quality. Higher threshold reduces trade frequency but "
            "improves average quality."
        ),
        "when_to_change": (
            "Increase to be more selective and reduce noise trades. Decrease to "
            "capture more opportunities or when testing strategies."
        ),
    },
    "position_mode_aware_conflicts": {
        "type": "boolean",
        "description": (
            "Enable position mode awareness in conflict resolution. When enabled, "
            "conflict detection respects the position_mode setting (hedge vs one-way). "
            "In hedge mode, opposite directions (BUY/SELL) on same symbol are NOT "
            "treated as conflicts since both LONG and SHORT positions can exist "
            "simultaneously. In one-way mode, opposite directions remain conflicts. "
            "**Recommended**: Enable this for proper hedge mode support."
        ),
        "default": True,
        "example": True,
        "impact": (
            "When enabled with hedge mode, allows simultaneous LONG and SHORT positions. "
            "When disabled, treats opposite directions as conflicts regardless of position mode."
        ),
        "when_to_change": (
            "Keep enabled for hedge mode trading. Disable only if you want to "
            "force conflict resolution even in hedge mode."
        ),
    },
    "same_direction_conflict_resolution": {
        "type": "string",
        "description": (
            "How to handle multiple signals in the SAME direction (e.g., two BUY signals). "
            "'accumulate' allows position building from multiple strategies. "
            "'strongest_wins' only executes the highest confidence signal. "
            "'reject_duplicates' rejects subsequent signals in same direction. "
            "**Note**: Only applies to signals from different strategies; same strategy "
            "duplicates are always rejected. **Recommended**: 'accumulate' for "
            "multi-strategy portfolios, 'strongest_wins' for single strategy with variants."
        ),
        "default": "accumulate",
        "allowed_values": [
            "accumulate",
            "strongest_wins",
            "reject_duplicates",
        ],
        "example": "accumulate",
        "impact": (
            "Controls whether multiple strategies can build positions together or "
            "compete for execution."
        ),
        "when_to_change": (
            "Use 'accumulate' to combine multiple strategies' conviction. "
            "Use 'strongest_wins' to avoid over-leveraging. "
            "Use 'reject_duplicates' for conservative single-entry strategies."
        ),
    },
    # -------------------------------------------------------------------------
    # Strategy Weights
    # -------------------------------------------------------------------------
    "strategy_weights": {
        "type": "dict",
        "description": (
            "Relative weights for different strategies in weighted conflict resolution. "
            "Higher weight = more influence. Used when signal_conflict_resolution = "
            "'weighted_average'. **Example**: {'momentum': 1.5, 'mean_reversion': 0.8} "
            "gives momentum strategies 1.5x weight vs 0.8x for mean reversion."
        ),
        "default": {
            "momentum_strategy": 1.0,
            "mean_reversion_strategy": 0.8,
            "ml_strategy": 1.2,
            "llm_strategy": 1.5,
            "default": 1.0,
        },
        "example": {"momentum_strategy": 1.0, "default": 1.0},
        "impact": (
            "Influences signal selection in weighted resolution. Higher weights "
            "increase strategy influence."
        ),
        "when_to_change": (
            "Adjust based on strategy performance. Increase weights for better "
            "performing strategies, decrease for underperforming ones."
        ),
    },
    # -------------------------------------------------------------------------
    # Timeframe Weights
    # -------------------------------------------------------------------------
    "timeframe_weights": {
        "type": "dict",
        "description": (
            "Relative weights for different timeframes in weighted conflict resolution. "
            "Higher weight = more influence. Used when timeframe_conflict_resolution = "
            "'weighted_average'. **Default**: Daily (1d) has highest weight (1.6), "
            "minute (1m) has lowest (0.5). Reflects that longer timeframes typically "
            "have stronger, more reliable signals."
        ),
        "default": {
            "1m": 0.5,
            "5m": 0.7,
            "15m": 0.8,
            "1h": 1.0,
            "4h": 1.2,
            "1d": 1.6,
        },
        "example": {"1h": 1.0, "4h": 1.2, "1d": 1.6},
        "impact": (
            "Influences timeframe priority in weighted resolution. Higher weights "
            "give timeframe more influence."
        ),
        "when_to_change": (
            "Increase weights for timeframes that work better for your strategy. "
            "Reduce weights for noisy timeframes."
        ),
    },
    # -------------------------------------------------------------------------
    # Advanced Options
    # -------------------------------------------------------------------------
    "enabled": {
        "type": "boolean",
        "description": (
            "Master on/off switch for this configuration scope. When false, this "
            "symbol/side combination will not trade even if signals are received. "
            "Useful for temporarily disabling specific pairs or sides without "
            "deleting configuration. **Example**: Disable LONG positions on BTCUSDT "
            "while keeping SHORT enabled."
        ),
        "default": True,
        "example": True,
        "impact": (
            "When false, completely disables trading for this scope. Signals are "
            "received but not executed."
        ),
        "when_to_change": (
            "Disable to pause trading specific symbols or sides without losing "
            "configuration. Enable to resume."
        ),
    },
    "enable_shorts": {
        "type": "boolean",
        "description": (
            "Whether SHORT positions are allowed for this scope. When false, all "
            "SHORT signals are ignored. Useful if you want to trade only long direction "
            "due to market conditions or strategy preference. **Note**: Only affects "
            "this specific scope (global/symbol/symbol-side)."
        ),
        "default": True,
        "example": True,
        "impact": (
            "When false, prevents all SHORT position entries. Does not affect "
            "existing SHORT positions."
        ),
        "when_to_change": (
            "Disable during strong bull markets or if your strategy performs poorly "
            "on shorts. Enable for normal two-way trading."
        ),
    },
    "enable_longs": {
        "type": "boolean",
        "description": (
            "Whether LONG positions are allowed for this scope. When false, all "
            "LONG signals are ignored. Useful if you want to trade only short direction "
            "due to market conditions or strategy preference. **Note**: Only affects "
            "this specific scope (global/symbol/symbol-side)."
        ),
        "default": True,
        "example": True,
        "impact": (
            "When false, prevents all LONG position entries. Does not affect "
            "existing LONG positions."
        ),
        "when_to_change": (
            "Disable during strong bear markets or if your strategy performs poorly "
            "on longs. Enable for normal two-way trading."
        ),
    },
    "slippage_tolerance_pct": {
        "type": "float",
        "description": (
            "Maximum acceptable slippage percentage for market orders. 0.1 = 0.1% "
            "slippage tolerance. If market price moves more than this from signal price, "
            "order may be rejected. **Purpose**: Prevents executing trades at prices "
            "far worse than expected. **Note**: Set higher for volatile pairs."
        ),
        "default": 0.1,
        "min": 0.0,
        "max": 10.0,
        "example": 0.1,
        "impact": (
            "Controls acceptable price deviation. Lower values prevent bad fills but "
            "may increase rejection rate."
        ),
        "when_to_change": (
            "Increase for volatile assets or illiquid markets. Decrease for stable "
            "assets or when precise execution is critical."
        ),
    },
    "max_retries": {
        "type": "integer",
        "description": (
            "Maximum number of retry attempts for failed orders. After this many "
            "failures, order is abandoned. Higher values increase chance of execution "
            "but may lead to stale orders. Lower values fail faster but may miss "
            "opportunities. **Typical**: 3 retries is good balance."
        ),
        "default": 3,
        "min": 0,
        "max": 10,
        "example": 3,
        "impact": (
            "Determines retry behavior. More retries = higher success rate but "
            "potential for stale execution."
        ),
        "when_to_change": (
            "Increase for unreliable network or exchange issues. Decrease for "
            "fast-moving markets where stale orders are risky."
        ),
    },
}


# =============================================================================
# VALIDATION FUNCTIONS
# =============================================================================


def validate_parameters(parameters: dict[str, Any]) -> tuple[bool, list[str]]:
    """
    Validate trading parameters against schema.

    Args:
        parameters: Dictionary of parameters to validate

    Returns:
        Tuple of (is_valid, list_of_errors)
    """
    errors: list[str] = []

    for param_name, param_value in parameters.items():
        if param_name not in PARAMETER_SCHEMA:
            errors.append(f"Unknown parameter: {param_name}")
            continue

        schema: dict[str, Any] = PARAMETER_SCHEMA[param_name]
        param_type: str = schema["type"]

        # Type validation
        if param_type == "integer" and not isinstance(param_value, int):
            errors.append(f"{param_name} must be integer, got {type(param_value)}")
            continue

        if param_type == "float" and not isinstance(param_value, int | float):
            errors.append(f"{param_name} must be float, got {type(param_value)}")
            continue

        if param_type == "string" and not isinstance(param_value, str):
            errors.append(f"{param_name} must be string, got {type(param_value)}")
            continue

        if param_type == "boolean" and not isinstance(param_value, bool):
            errors.append(f"{param_name} must be boolean, got {type(param_value)}")
            continue

        if param_type == "dict" and not isinstance(param_value, dict):
            errors.append(f"{param_name} must be dict, got {type(param_value)}")
            continue

        # Range validation
        if "min" in schema and param_value < schema["min"]:
            errors.append(f"{param_name} must be >= {schema['min']}, got {param_value}")

        if "max" in schema and param_value > schema["max"]:
            errors.append(f"{param_name} must be <= {schema['max']}, got {param_value}")

        # Allowed values validation
        if "allowed_values" in schema and param_value not in schema["allowed_values"]:
            errors.append(
                f"{param_name} must be one of {schema['allowed_values']}, "
                f"got {param_value}"
            )

    return len(errors) == 0, errors


def get_parameter_schema() -> dict[str, Any]:
    """Get complete parameter schema for API documentation."""
    return PARAMETER_SCHEMA


def get_default_parameters() -> dict[str, Any]:
    """Get default trading parameters."""
    return DEFAULT_TRADING_PARAMETERS.copy()


def merge_parameters(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """
    Merge override parameters into base parameters.

    Args:
        base: Base parameters
        override: Override parameters

    Returns:
        Merged parameters
    """
    result = base.copy()
    result.update(override)
    return result
