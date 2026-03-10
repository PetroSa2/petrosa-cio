import logging

from cio.models import CodeEngineResult, RegimeEnum, TriggerContext, VolatilityLevel

logger = logging.getLogger(__name__)

# Volatility Multipliers (From architecture/decision_framework.md)
SL_VOL_MULTIPLIERS = {
    VolatilityLevel.LOW: 1.0,
    VolatilityLevel.MEDIUM: 1.2,
    VolatilityLevel.HIGH: 1.5,
    VolatilityLevel.EXTREME: 2.0,
}

# Regime Multipliers and Caps (Fix 4)
REGIME_TP_MULTIPLIERS = {
    RegimeEnum.TRENDING_BULL: 1.3,
    RegimeEnum.TRENDING_BEAR: 1.3,
    RegimeEnum.BREAKOUT_PHASE: 1.5,
    RegimeEnum.RANGING: 0.8,
    RegimeEnum.CHOPPY: 0.6,
    RegimeEnum.HIGH_VOLATILITY: 0.7,
    RegimeEnum.CAPITULATION: 0.6,
    RegimeEnum.RECOVERY: 1.0,
}

REGIME_LEVERAGE_CAPS = {
    RegimeEnum.TRENDING_BULL: 2.0,
    RegimeEnum.TRENDING_BEAR: 2.0,
    RegimeEnum.BREAKOUT_PHASE: 1.5,
}
DEFAULT_LEVERAGE_CAP = 1.0

REGIME_HARD_BLOCKS = {
    RegimeEnum.CAPITULATION: "regime_block: CAPITULATION — capital preservation mode, no new entries",
    RegimeEnum.CHOPPY: "regime_block: CHOPPY — signal quality too low, skip to avoid noise trades",
}


class CodeEngine:
    """
    Deterministic quantitative engine for risk, EV, and position sizing.
    Purely functional math with no side effects or async calls.
    """

    @staticmethod
    def run(context: TriggerContext) -> CodeEngineResult:
        """
        Executes the full quantitative analysis pipeline.
        1. Risk Gates
        2. Parameter Generation
        3. EV Calculation
        4. Position Sizing
        """
        result = CodeEngineResult()

        # 1. RISK GATES
        # Hard block if drawdown, global orders, or symbol orders exceed limits
        if context.global_drawdown_pct >= context.risk_limits.max_drawdown_pct:
            result.hard_blocked = True
            result.block_reason = (
                f"Global drawdown {context.global_drawdown_pct:.2%} exceeds "
                f"limit {context.risk_limits.max_drawdown_pct:.2%}."
            )
        elif context.open_orders_global >= context.risk_limits.max_orders_global:
            result.hard_blocked = True
            result.block_reason = (
                f"Global open orders {context.open_orders_global} exceeds "
                f"limit {context.risk_limits.max_orders_global}."
            )
        elif context.open_orders_symbol >= context.risk_limits.max_orders_per_symbol:
            result.hard_blocked = True
            result.block_reason = (
                f"Symbol open orders {context.open_orders_symbol} exceeds "
                f"limit {context.risk_limits.max_orders_per_symbol}."
            )

        if result.hard_blocked:
            logger.warning(
                "Risk gate triggered",
                extra={
                    "correlation_id": context.correlation_id,
                    "block_reason": result.block_reason,
                },
            )
            return result

        # 2. REGIME HARD BLOCKS (Fix 4)
        if context.regime.regime in REGIME_HARD_BLOCKS:
            result.hard_blocked = True
            result.block_reason = REGIME_HARD_BLOCKS[context.regime.regime]
            logger.warning(
                "Regime hard block triggered",
                extra={
                    "correlation_id": context.correlation_id,
                    "regime": context.regime.regime,
                    "block_reason": result.block_reason,
                },
            )
            return result

        # 3. PARAMETER GENERATION (Initial volatility-adjusted SL/TP)
        vol_multiplier = SL_VOL_MULTIPLIERS.get(context.volatility_level, 1.0)
        result.recommended_sl_pct = (
            context.strategy_defaults.stop_loss_pct * vol_multiplier
        )
        result.recommended_tp_pct = context.strategy_defaults.take_profit_pct
        result.leverage = context.strategy_defaults.leverage

        # 4. EV CALCULATION
        win_rate = context.strategy_stats.win_rate
        if win_rate is None:
            result.ev_unavailable = True
        else:
            # gross_ev = (win_rate * TP) - ((1 - win_rate) * SL)
            # win_rate: float 0-1, TP: float 0-1, SL: float 0-1
            result.gross_ev = (win_rate * result.recommended_tp_pct) - (
                (1 - win_rate) * result.recommended_sl_pct
            )

        # 5. POSITION SIZING (Kelly Criterion)
        if win_rate is not None and result.recommended_sl_pct > 0:
            # Kelly Fraction f* = (p/a) - (q/b) where:
            # p = probability of win (win_rate)
            # q = probability of loss (1 - win_rate)
            # b = odds (TP / SL)
            # f* = p - q/b
            odds = result.recommended_tp_pct / result.recommended_sl_pct

            if odds > 0:
                kelly_f = win_rate - (1 - win_rate) / odds

                # Cap Kelly at 0.25 (1/4 Kelly)
                result.kelly_fraction = max(0.0, min(0.25, kelly_f))

                # Multiply by available capital
                result.kelly_position_usd = (
                    result.kelly_fraction * context.available_capital_usd
                )
            else:
                result.kelly_fraction = 0.0
                result.kelly_position_usd = 0.0
                result.risk_warnings.append(
                    "Kelly calculation skipped: zero odds (TP=0)."
                )

            # Final check: Don't exceed max position size from risk limits
            if (
                result.kelly_position_usd
                and result.kelly_position_usd
                > context.risk_limits.max_position_size_usd
            ):
                result.kelly_position_usd = context.risk_limits.max_position_size_usd
                result.risk_warnings.append("Kelly size capped by risk limit.")

        # 6. REGIME ADJUSTMENTS (Fix 4)
        # Apply TP regime multiplier
        tp_multiplier = REGIME_TP_MULTIPLIERS.get(context.regime.regime, 1.0)
        result.recommended_tp_pct *= tp_multiplier

        # Apply Leverage regime cap
        lev_cap = REGIME_LEVERAGE_CAPS.get(context.regime.regime, DEFAULT_LEVERAGE_CAP)
        result.leverage = min(context.strategy_defaults.leverage, lev_cap)

        return result
