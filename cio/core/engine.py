import logging

from cio.core.drawdown import (
    drawdown_enforced,
    evaluate_drawdown,
    is_closing_intent,
)
from cio.core.metrics import (
    REGIME_AGE,
    REGIME_UNAVAILABLE,
    RISK_GATE_CONTEXT_FALLBACK,
    RISK_GATE_REAL_BREACH,
)
from cio.core.net_ev import evaluate as evaluate_net_ev
from cio.core.net_ev import log_gate
from cio.core.order_levels import carried_order_distances
from cio.core.regime_policy import regime_availability
from cio.core.sizing import size_order
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

# CAPITULATION and CHOPPY block new entries, but only on a CONFIDENT regime: a low-confidence one is
# unavailable (probe size only, see regime_policy.py), not blocking. data-manager reports `transitional`
# (mapped to CHOPPY) at a constant low confidence and nothing maps to CAPITULATION yet, so these blocks only
# fire once data-manager reports those regimes with confidence (petrosa-cio#294).
REGIME_HARD_BLOCKS = {
    RegimeEnum.CAPITULATION: "regime_block: CAPITULATION — capital preservation mode, no new entries",
    RegimeEnum.CHOPPY: "regime_block: CHOPPY — signal quality too low, skip to avoid noise trades",
}


def _carried_order_distances(
    context: TriggerContext,
) -> tuple[float | None, float | None]:
    """The stop and target distances the order carries (see ``carried_order_distances``)."""
    return carried_order_distances(
        context.trigger_payload, context.market_signals.current_price
    )


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
        pre_decision = context.pre_decision_context
        if pre_decision is not None and pre_decision.portfolio_state_available is False:
            gap_reason = next(
                (gap.reason for gap in pre_decision.gaps if gap.surface == "portfolio"),
                "unknown",
            )
            result.hard_blocked = True
            result.block_context_fallback = True
            result.block_reason = (
                "PORTFOLIO_CONTEXT_UNAVAILABLE: tradeengine /state fetch failed "
                f"({gap_reason}); risk limits could not be evaluated. "
                "Fail-safe BLOCK, not a risk breach."
            )
            RISK_GATE_CONTEXT_FALLBACK.add(1)
            logger.warning(
                "PORTFOLIO_CONTEXT_UNAVAILABLE: risk gate fail-safe BLOCK on "
                "context-fetch FALLBACK defaults (NOT a real risk breach — "
                "tradeengine /state fetch failed) reason=%s",
                gap_reason,
            )
            return result

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
            RISK_GATE_REAL_BREACH.add(1)
            logger.warning(
                "Risk gate triggered (live portfolio/risk data)",
                extra={
                    "correlation_id": context.correlation_id,
                    "block_reason": result.block_reason,
                    "block_context_fallback": False,
                },
            )
            return result

        # 1b. DRAWDOWN STEPS (petrosa-cio#298, rule 5): reduce at z_reduce x sigma, halt new entries at
        # z_halt x sigma of the drawdown from the equity peak. Closes and reduce-only orders pass.
        drawdown = evaluate_drawdown(
            context.drawdown_state,
            context.risk_inputs,
            context.portfolio.net_notional_by_symbol,
        )
        result.drawdown = drawdown
        closing = is_closing_intent(context.trigger_payload)
        if drawdown.action == "halt" and not closing and drawdown_enforced():
            result.hard_blocked = True
            result.block_reason = (
                f"drawdown_halt: drawdown {drawdown.drawdown:.2%} from the equity peak >= "
                f"{drawdown.halt_threshold:.2%} ({drawdown.z_halt:g} sigma, sigma "
                f"{drawdown.sigma if drawdown.sigma is not None else 'n/a'} from {drawdown.sigma_source}, "
                f"thresholds {drawdown.threshold_source}); new entries halted"
            )
            RISK_GATE_REAL_BREACH.add(1)
            logger.warning(
                "DRAWDOWN_HALT %s",
                drawdown.model_dump_json(),
                extra={"correlation_id": context.correlation_id},
            )
            return result
        if drawdown.action in ("reduce", "halt"):
            logger.warning(
                "DRAWDOWN_STEP %s",
                drawdown.model_dump_json(),
                extra={"correlation_id": context.correlation_id},
            )

        # 2. REGIME HARD BLOCKS (Fix 4): only on a fresh, confident regime. A stale, low-confidence or missing
        # regime is unavailable: probe size only (below), never a block (petrosa-cio#294, #326).
        regime_state = regime_availability(context.regime)
        if regime_state.age_seconds is not None:
            REGIME_AGE.record(regime_state.age_seconds)
        if not regime_state.available:
            REGIME_UNAVAILABLE.add(1, {"reason": str(regime_state.reason)})
            logger.info(
                "REGIME_UNAVAILABLE %s: probe size only, no regime block",
                regime_state.reason,
                extra={
                    "correlation_id": context.correlation_id,
                    "regime": str(context.regime.regime),
                    "age_seconds": regime_state.age_seconds,
                },
            )
        if context.regime.regime in REGIME_HARD_BLOCKS and regime_state.available:
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

        # 3. PARAMETER GENERATION (prefer the levels carried by the order)
        defaults = context.strategy_defaults
        carried_stop, carried_target = _carried_order_distances(context)
        if carried_stop is not None:
            result.recommended_sl_pct = carried_stop
        elif defaults.available and defaults.sl_configured:
            vol_multiplier = SL_VOL_MULTIPLIERS.get(context.volatility_level, 1.0)
            result.recommended_sl_pct = defaults.stop_loss_pct * vol_multiplier
        # else: no stop is known; the labelled fallback is never recommended or used for EV.
        if carried_target is not None:
            result.recommended_tp_pct = carried_target
        elif defaults.available and defaults.tp_configured:
            result.recommended_tp_pct = defaults.take_profit_pct
        result.leverage = context.strategy_defaults.leverage

        # 4. REGIME ADJUSTMENTS (Fix 4)
        if carried_target is None and result.recommended_tp_pct is not None:
            # Apply TP regime multiplier (only to a configured take-profit, never to a carried one)
            tp_multiplier = REGIME_TP_MULTIPLIERS.get(context.regime.regime, 1.0)
            result.recommended_tp_pct *= tp_multiplier

        # Apply Leverage regime cap
        lev_cap = REGIME_LEVERAGE_CAPS.get(context.regime.regime, DEFAULT_LEVERAGE_CAP)
        result.leverage = min(context.strategy_defaults.leverage, lev_cap)

        # 5. EV CALCULATION (only when both the stop and the target are known)
        levels_known = (
            result.recommended_sl_pct is not None
            and result.recommended_tp_pct is not None
        )
        stats = context.strategy_stats
        win_rate = (
            None if stats.history_status == "insufficient_history" else stats.win_rate
        )
        if win_rate is None or not levels_known:
            result.ev_unavailable = True
        else:
            # gross_ev = (win_rate * TP) - ((1 - win_rate) * SL)
            # win_rate: float 0-1, TP: float 0-1, SL: float 0-1
            result.gross_ev = (win_rate * result.recommended_tp_pct) - (
                (1 - win_rate) * result.recommended_sl_pct
            )

        # 5b. NET-EV GATE RECORD (petrosa-cio#296): p_be, costs and the posterior at the order's levels;
        # the cost share c/S is logged on every decision.
        result.net_ev_gate = evaluate_net_ev(
            context, result.recommended_sl_pct, result.recommended_tp_pct
        )
        log_gate(context, result.net_ev_gate)

        # 6. POSITION SIZING (rule 2, petrosa-cio#297): size = max(probe, f_q x Kelly(p_post, b_net) x equity
        # x P(net EV > 0)) from the gate's posterior; the probe when there is no posterior or the data is
        # flagged. Only when the levels are known (as before).
        if levels_known and win_rate is not None and result.net_ev_gate is not None:
            factor = (
                drawdown.reduce_factor
                if drawdown.action == "reduce" and not closing and drawdown_enforced()
                else 1.0
            )
            sizing = size_order(
                context,
                result.net_ev_gate,
                factor,
                regime_state.reason,
                regime_state,
            )
            result.sizing = sizing
            result.kelly_fraction = sizing.kelly_fraction
            result.kelly_position_usd = sizing.final_size_usd
            if result.net_ev_gate.integrity is not None:
                integrity = result.net_ev_gate.integrity
                result.risk_warnings.append(
                    "DATA_INTEGRITY: open round "
                    f"{integrity.open_round_age_hours:.1f}h old, over 3x the median holding time "
                    f"({integrity.median_holding_hours:.1f}h, {integrity.holding_source}); "
                    "exits are not being attributed, sized at the probe"
                )
                logger.warning(
                    "DATA_INTEGRITY strategy=%s %s",
                    context.strategy_id,
                    integrity.model_dump_json(),
                    extra={"correlation_id": context.correlation_id},
                )
        return result
