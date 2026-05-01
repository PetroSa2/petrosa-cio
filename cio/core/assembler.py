import logging
from datetime import UTC, datetime

from cio.models import (
    ActionType,
    AppliedParamChange,
    CodeEngineResult,
    DecisionResult,
    ParamChangeDirection,
    RegimeResult,
    StrategyResult,
    TriggerContext,
)

logger = logging.getLogger(__name__)


class DecisionAssembler:
    """
    Synthesizes Code Engine results, LLM classifications, and persona assessments.
    Produces the final DecisionResult for the Output Router.
    """

    @staticmethod
    def assemble(
        context: TriggerContext,
        code_result: CodeEngineResult,
        regime_result: RegimeResult,
        strategy_result: StrategyResult,
        llm_action: ActionType | None = None,
        llm_justification: str | None = None,
    ) -> DecisionResult:
        """
        Pure synchronous assembly logic.
        1. Handle hard blocks.
        2. Synthesize parameter changes.
        3. Select final position size.
        4. Assemble final DecisionResult.
        """
        correlation_id = context.correlation_id

        # 1. HARD BLOCK PASSTHROUGH
        if code_result.hard_blocked:
            logger.info(
                "Final decision: BLOCK",
                extra={
                    "correlation_id": correlation_id,
                    "reason": code_result.block_reason,
                },
            )
            return DecisionResult(
                hard_blocked=True,
                hard_block_reason=code_result.block_reason,
                ev_passes=code_result.ev_unavailable is False,  # Simplified for block
                cost_viable=False,
                regime_confidence=regime_result.regime_confidence,
                regime_fit=strategy_result.regime_fit,
                strategy_health=strategy_result.health,
                activation_recommendation=strategy_result.activation_recommendation,
                computed_position_size_usd=0.0,
                action=ActionType.BLOCK,
                justification=f"Hard blocked by engine: {code_result.block_reason}",
                thought_trace="Code Engine safety gate triggered. Bypassing all LLM logic.",
            )

        # 2. PARAMETER SYNTHESIS
        sl_pct = code_result.recommended_sl_pct
        tp_pct = code_result.recommended_tp_pct
        applied_change: AppliedParamChange | None = None

        if strategy_result.param_change:
            sig = strategy_result.param_change
            multiplier = 1.1 if sig.direction == ParamChangeDirection.INCREASE else 0.9

            old_val = 0.0
            new_val = 0.0

            if sig.param == "stop_loss_pct":
                old_val = sl_pct
                sl_pct *= multiplier
                new_val = sl_pct
            elif sig.param == "take_profit_pct":
                old_val = tp_pct
                tp_pct *= multiplier
                new_val = tp_pct

            if old_val > 0:
                applied_change = AppliedParamChange(
                    strategy_id=context.strategy_id,
                    timestamp=datetime.now(UTC),
                    param=sig.param,
                    old_value=old_val,
                    new_value=new_val,
                    direction=sig.direction,
                    reason=sig.reason,
                )

        # 3. POSITION SIZE SELECTION
        final_size_usd = code_result.kelly_position_usd
        if final_size_usd is None:
            # Fallback for ev_unavailable
            final_size_usd = min(500.0, context.risk_limits.max_position_size_usd * 0.1)
            logger.debug(
                f"EV unavailable; using fallback position size: ${final_size_usd}"
            )

        # 4. FINAL ASSEMBLY
        # reasoning_summary: Concatenate thought traces from regime and strategy results
        reasoning_summary = (
            f"Regime: {regime_result.thought_trace} | "
            f"Strategy: {strategy_result.thought_trace}"
        )

        decision = DecisionResult(
            hard_blocked=False,
            ev_passes=code_result.ev_unavailable is False,
            cost_viable=True,  # Simplified for S3
            net_ev_usd=code_result.gross_ev,  # Simplified mapping
            regime_confidence=regime_result.regime_confidence,
            regime_fit=strategy_result.regime_fit,
            strategy_health=strategy_result.health,
            activation_recommendation=strategy_result.activation_recommendation,
            param_change=applied_change,
            computed_position_size_usd=final_size_usd,
            stop_loss_pct=sl_pct,
            take_profit_pct=tp_pct,
            leverage=code_result.leverage,
            risk_warnings=code_result.risk_warnings,
            action=llm_action or ActionType.SKIP,
            justification=llm_justification or "Assembled without explicit LLM action.",
            thought_trace=reasoning_summary,
        )

        logger.info(
            f"Final decision: {decision.action}",
            extra={
                "correlation_id": correlation_id,
                "position_size": final_size_usd,
                "strategy_id": context.strategy_id,
            },
        )

        return decision
