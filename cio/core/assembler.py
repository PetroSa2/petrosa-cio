import logging
from datetime import UTC, datetime

from cio.core.net_ev import evaluate as evaluate_net_ev
from cio.core.net_ev import gate_enforced, log_gate, probe_notional
from cio.models import (
    ActionType,
    ActivationRecommendation,
    AppliedParamChange,
    CodeEngineResult,
    ConfidenceLevel,
    DecisionResult,
    HealthStatus,
    ParamChangeDirection,
    RegimeFit,
    RegimeResult,
    StrategyResult,
    TriggerContext,
)
from cio.models.decision import LLM_UNAVAILABLE_TRACE
from cio.models.enums import RejectionSource
from cio.models.net_ev import SizingRecord

logger = logging.getLogger(__name__)
PORTFOLIO_CONTEXT_UNAVAILABLE_TRACE = "PORTFOLIO_CONTEXT_UNAVAILABLE"


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
            if code_result.block_context_fallback is True:
                logger.info(
                    "Final decision: BLOCK (PORTFOLIO_CONTEXT_UNAVAILABLE)",
                    extra={
                        "correlation_id": correlation_id,
                        "reason": code_result.block_reason,
                    },
                )
                return DecisionResult(
                    hard_blocked=True,
                    hard_block_reason=code_result.block_reason,
                    ev_passes=False,
                    cost_viable=False,
                    regime_confidence=ConfidenceLevel.LOW,
                    regime_fit=RegimeFit.NEUTRAL,
                    strategy_health=HealthStatus.HEALTHY,
                    activation_recommendation=ActivationRecommendation.RUN,
                    computed_position_size_usd=0.0,
                    action=ActionType.BLOCK,
                    justification=code_result.block_reason,
                    thought_trace=PORTFOLIO_CONTEXT_UNAVAILABLE_TRACE,
                    rejection_source=RejectionSource.PORTFOLIO_CONTEXT_UNAVAILABLE,
                )
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

            if sig.param == "stop_loss_pct" and sl_pct is not None:
                old_val = sl_pct
                sl_pct *= multiplier
                new_val = sl_pct
            elif sig.param == "take_profit_pct" and tp_pct is not None:
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

        # 2b. NET-EV GATE (petrosa-cio#296, rule 1): deterministic, after the LLM, on the levels the
        # order will really carry (after any parameter change). The LLM may only downgrade a decision:
        # an execute or modify_params that fails the gate becomes a skip. The position size always comes
        # from the code (below), so a modify_params never carries more than the computed size.
        action = llm_action or ActionType.SKIP
        justification = llm_justification or "Assembled without explicit LLM action."
        gate = evaluate_net_ev(context, sl_pct, tp_pct)
        log_gate(context, gate)
        probe_override = False
        if gate_enforced() and action in (
            ActionType.EXECUTE,
            ActionType.MODIFY_PARAMS,
        ):
            if gate.outcome == "veto":
                justification = (
                    f"{gate.reason}: LLM {action.value} vetoed by the net-EV gate "
                    f"(phase={gate.phase}, p_be={gate.p_be:.3f}, "
                    + (
                        f"P(win rate > p_be)={gate.prob_edge:.3f} < {1 - (gate.alpha or 0):.2f}, "
                        if gate.prob_edge is not None
                        else ""
                    )
                    + f"n={gate.n}, n_req={gate.n_req}, S_eff={gate.s_eff:.4f}, "
                    f"c={gate.cost_total:.5f})"
                )
                action = ActionType.SKIP
            elif gate.outcome == "probe":
                # Cold start: a failing order is not vetoed; it goes at the probe notional.
                probe_override = True
                justification = (
                    f"{justification} [net-EV gate failed during cold start (n={gate.n}, "
                    f"n_req={gate.n_req:.0f}): sized at the probe notional]"
                )

        # 3. POSITION SIZE SELECTION
        final_size_usd = code_result.kelly_position_usd
        if final_size_usd is None:
            # EV unavailable: in probe mode the size is the probe notional that tradeengine reports
            # as max_position_size_usd (the symbol's smallest valid order).
            if context.risk_limits.probe_mode:
                final_size_usd = context.risk_limits.max_position_size_usd
            else:
                final_size_usd = min(
                    500.0, context.risk_limits.max_position_size_usd * 0.1
                )
            logger.debug(
                f"EV unavailable; using fallback position size: ${final_size_usd}"
            )

        if probe_override:
            final_size_usd = probe_notional(context)
        sizing = (
            code_result.sizing if isinstance(code_result.sizing, SizingRecord) else None
        )
        if sizing is not None and probe_override:
            sizing = sizing.model_copy(
                update={"final_size_usd": final_size_usd, "binding": "cold_start_probe"}
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
            cost_viable=gate.result != "fail",
            net_ev_usd=code_result.gross_ev,  # Simplified mapping
            net_ev_gate=gate,
            sizing=sizing,
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
            action=action,
            justification=justification,
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

    @staticmethod
    def assemble_llm_unavailable(
        context: TriggerContext,
        code_result: CodeEngineResult,
        regime_result: RegimeResult,
        strategy_result: StrategyResult,
        failed_stages: list[str],
    ) -> DecisionResult:
        stages = ",".join(failed_stages)
        resolvable = bool(context.strategy_id) and context.strategy_id != "unknown"

        if resolvable:
            action = ActionType.PAUSE_STRATEGY
            justification = (
                f"LLM_UNAVAILABLE: {stages} fell back to SAFE_DEFAULTS; "
                "pausing strategy (policy: pause on LLM outage)"
            )
            logger.warning(
                f"LLM_UNAVAILABLE_PAUSE strategy_id={context.strategy_id} "
                f"stages={stages} correlation_id={context.correlation_id}"
            )
        else:
            action = ActionType.SKIP
            justification = (
                f"LLM_UNAVAILABLE: {stages} fell back to SAFE_DEFAULTS; "
                "strategy_id unresolvable, skipping signal"
            )
            logger.error(
                f"LLM_UNAVAILABLE_UNRESOLVABLE_STRATEGY "
                f"strategy_id={context.strategy_id!r} stages={stages} "
                f"correlation_id={context.correlation_id}"
            )

        try:
            from cio.core.metrics import LLM_UNAVAILABLE_DECISIONS

            LLM_UNAVAILABLE_DECISIONS.add(1, {"stage": stages, "action": action.value})
        except ImportError:
            pass

        return DecisionResult(
            hard_blocked=False,
            ev_passes=code_result.ev_unavailable is False,
            cost_viable=False,
            regime_confidence=regime_result.regime_confidence,
            regime_fit=strategy_result.regime_fit,
            strategy_health=strategy_result.health,
            activation_recommendation=ActivationRecommendation.PAUSE,
            computed_position_size_usd=0.0,
            leverage=code_result.leverage,
            risk_warnings=code_result.risk_warnings,
            action=action,
            justification=justification,
            thought_trace=LLM_UNAVAILABLE_TRACE,
            rejection_source=RejectionSource.LLM_UNAVAILABLE,
        )
