import logging
import os
import time

from cio.clients.factory import ClientFactory
from cio.core.characterization_stale_gate import is_characterization_stale
from cio.core.engine import CodeEngine
from cio.core.leverage_arbiter import arbitrate_leverage
from cio.core.portfolio_tracker import PortfolioTracker
from cio.core.portfolio_tracker import portfolio_tracker as _default_portfolio_tracker
from cio.core.spend_tracker import LlmSpendTracker
from cio.models import (
    SAFE_DECISION_RESULT,
    ActionType,
    ActivationRecommendation,
    ConfidenceLevel,
    DecisionResult,
    HealthStatus,
    RegimeEnum,
    RegimeFit,
    RegimeResult,
    StrategyResult,
    TriggerContext,
    TriggerType,
    VolatilityLevel,
)
from cio.models.enums import RejectionSource
from cio.personas.action_classifier import ActionClassifier
from cio.personas.regime_analyst import RegimeAnalyst
from cio.personas.strategy_assessor import StrategyAssessor

logger = logging.getLogger(__name__)


class Orchestrator:
    """
    The central intelligence unit for the Petrosa CIO.
    Coordinates the Reasoning Loop, Code Engine, and Persona calls.
    """

    def __init__(
        self,
        llm_client=None,
        cache=None,
        portfolio_tracker: PortfolioTracker | None = None,
    ):
        self.client = llm_client or ClientFactory.create()
        self.cache = cache
        self.regime_analyst = RegimeAnalyst(self.client)
        self.strategy_assessor = StrategyAssessor(self.client)
        self.action_classifier = ActionClassifier(self.client)
        # P1.5-AC5 (#138) — module singleton by default so a default-
        # constructed Orchestrator participates in the same aggregate
        # ledger as the rest of the process. Tests inject a fresh
        # tracker to isolate state.
        self.portfolio_tracker = (
            portfolio_tracker
            if portfolio_tracker is not None
            else _default_portfolio_tracker
        )

        # Read governance flags from environment (Ticket #334/337)
        self.use_llm_reasoning = (
            os.getenv("NURSE_USE_LLM_REASONING", "true").lower() == "true"
        )
        if not self.use_llm_reasoning:
            logger.warning(
                "⚠️ NURSE_USE_LLM_REASONING is disabled. CIO will operate in deterministic bypass mode."
            )
        # FR63: track whether ceiling triggered the bypass so period-reset can restore it.
        self._ceiling_triggered_bypass = False

    async def run(self, context: TriggerContext) -> DecisionResult:
        """
        Executes the reasoning loop for a given trigger context.
        Follows the HOT / WARM / COLD path logic.
        """
        start_time = time.perf_counter()
        provider_name = self.client.__class__.__name__

        logger.info(
            f"🧠 STARTING REASONING LOOP | Provider: {provider_name} | CID: {context.correlation_id} | Use LLM: {self.use_llm_reasoning}",
            extra={
                "correlation_id": context.correlation_id,
                "trigger_type": context.trigger_type.value,
                "strategy_id": context.strategy_id,
                "llm_provider": provider_name,
                "use_llm_reasoning": self.use_llm_reasoning,
            },
        )

        # FR53 / P3.4 (#130) — stale-characterization refusal gate. Only runs
        # on trade-intent triggers that actually carry a revision id; legacy
        # intents (no revision id) and non-intent triggers pass through.
        if (
            context.trigger_type == TriggerType.TRADE_INTENT
            and context.strategy_revision_id
        ):
            try:
                stale = await is_characterization_stale(
                    strategy_id=context.strategy_id,
                    strategy_revision_id=context.strategy_revision_id,
                )
            except Exception as gate_exc:  # noqa: BLE001 — fail-open, never crash
                logger.warning(
                    "stale-characterization gate raised — failing open",
                    extra={
                        "correlation_id": context.correlation_id,
                        "error": str(gate_exc),
                    },
                )
                stale = False
            if stale:
                reason = (
                    f"stale_characterization: no Characterization for "
                    f"strategy_id={context.strategy_id} "
                    f"strategy_revision_id={context.strategy_revision_id}"
                )
                logger.warning(
                    "🛑 REJECTING INTENT: stale characterization",
                    extra={
                        "correlation_id": context.correlation_id,
                        "strategy_id": context.strategy_id,
                        "strategy_revision_id": context.strategy_revision_id,
                    },
                )
                return DecisionResult(
                    hard_blocked=True,
                    hard_block_reason=reason,
                    ev_passes=False,
                    cost_viable=False,
                    regime_confidence=ConfidenceLevel.LOW,
                    regime_fit=RegimeFit.NEUTRAL,
                    strategy_health=HealthStatus.HEALTHY,
                    activation_recommendation=ActivationRecommendation.PAUSE,
                    action=ActionType.REJECT,
                    justification=reason,
                    thought_trace="STALE_CHARACTERIZATION",
                    rejection_source=RejectionSource.STALE_CHARACTERIZATION,
                )

        try:
            # Placeholder results for deterministic bypass (Ticket #334/337)
            # Use explicit "bypass" placeholders instead of SAFE_DEFAULTS to avoid "PARSE_FAILURE" trace
            bypass_regime = RegimeResult(
                regime=RegimeEnum.RANGING,
                regime_confidence=ConfidenceLevel.HIGH,
                volatility_level=VolatilityLevel.MEDIUM,
                primary_signal="DETERMINISTIC_BYPASS",
                confidence=1.0,
                fit=RegimeFit.GOOD,
                thought_trace="DETERMINISTIC_BYPASS",
            )
            bypass_strategy = StrategyResult(
                strategy_id=context.strategy_id,
                health=HealthStatus.HEALTHY,
                activation_recommendation=ActivationRecommendation.RUN,
                regime_fit=RegimeFit.GOOD,
                confidence=1.0,
                thought_trace="DETERMINISTIC_BYPASS",
            )

            # 1. CODE ENGINE: Hard Limits (S2)
            # In bypass mode, substitute bypass_regime so that policy-based regime hard
            # blocks (CHOPPY / CAPITULATION) do not fire. Risk-gate hard limits (drawdown,
            # open orders) still apply because they are derived from env_stats/risk_limits,
            # not from the regime field.
            if not self.use_llm_reasoning:
                engine_context = context.model_copy(
                    update={
                        "regime": bypass_regime,
                        "volatility_level": bypass_regime.volatility_level,
                    }
                )
            else:
                engine_context = context

            code_result = CodeEngine.run(engine_context)

            # P1.5-AC5 (#138) — portfolio aggregate leverage ceiling. Runs
            # AFTER the code engine (so we know kelly_position_usd) but
            # BEFORE the persona LLM work (so a guaranteed-REJECT doesn't
            # burn LLM spend). Hard-blocks fall through this gate — the
            # block flow below already short-circuits to the action
            # classifier with the existing reason.
            if not code_result.hard_blocked:
                new_position_size_usd = float(code_result.kelly_position_usd or 0.0)
                if new_position_size_usd > 0:
                    leverage_decision = arbitrate_leverage(
                        recommended_leverage=getattr(
                            context, "recommended_leverage", None
                        ),
                        strategy_envelope=getattr(
                            context, "strategy_leverage_envelope", None
                        ),
                    )
                    equity = float(context.available_capital_usd or 0.0)
                    ceiling_check = await self.portfolio_tracker.would_breach_ceiling(
                        new_position_size_usd=new_position_size_usd,
                        new_leverage=leverage_decision.decided_leverage,
                        equity=equity,
                    )
                    logger.info(
                        "portfolio_ceiling check: %s",
                        ceiling_check.reason,
                        extra={"correlation_id": context.correlation_id},
                    )
                    if ceiling_check.would_breach:
                        reason = (
                            "AGGREGATE_LEVERAGE_CEILING: admission rejected. "
                            f"current={ceiling_check.current_aggregate:.4f}, "
                            f"projected={ceiling_check.projected_aggregate:.4f}, "
                            f"ceiling={ceiling_check.ceiling:.4f}, "
                            f"new_size_usd={new_position_size_usd}, "
                            f"new_leverage={leverage_decision.decided_leverage}"
                        )
                        logger.warning(
                            "🛑 REJECTING INTENT: aggregate leverage ceiling",
                            extra={
                                "correlation_id": context.correlation_id,
                                "strategy_id": context.strategy_id,
                                "current_aggregate": (ceiling_check.current_aggregate),
                                "projected_aggregate": (
                                    ceiling_check.projected_aggregate
                                ),
                                "ceiling": ceiling_check.ceiling,
                            },
                        )
                        return DecisionResult(
                            hard_blocked=True,
                            hard_block_reason=reason,
                            ev_passes=False,
                            cost_viable=False,
                            regime_confidence=ConfidenceLevel.LOW,
                            regime_fit=RegimeFit.NEUTRAL,
                            strategy_health=HealthStatus.HEALTHY,
                            activation_recommendation=(ActivationRecommendation.PAUSE),
                            action=ActionType.REJECT,
                            justification=reason,
                            thought_trace="AGGREGATE_LEVERAGE_CEILING",
                            rejection_source=(
                                RejectionSource.AGGREGATE_LEVERAGE_CEILING
                            ),
                        )
                    # Within ceiling — record the admission so subsequent
                    # checks include this position. Recorded here (not in
                    # router) so the ledger stays consistent even if a
                    # later step in the persona pipeline aborts: the
                    # admission decision *has* been committed by the
                    # time we move on.
                    await self.portfolio_tracker.record_admit(
                        strategy_id=context.strategy_id,
                        position_size_usd=new_position_size_usd,
                        leverage=leverage_decision.decided_leverage,
                    )

            if code_result.hard_blocked:
                # Bypassing persona analysis for hard blocks
                logger.warning(
                    f"⛔ HARD BLOCK DETECTED | Reason: {code_result.block_reason}",
                    extra={
                        "correlation_id": context.correlation_id,
                        "block_reason": code_result.block_reason,
                    },
                )

                logger.info(
                    "Executing final Action Classifier for hard-blocked trade",
                    extra={"correlation_id": context.correlation_id},
                )
                return await self.action_classifier.classify(
                    context,
                    code_result,
                    bypass_regime,
                    bypass_strategy,
                    bypass_mode=not self.use_llm_reasoning,
                )

            # NEW: DETERMINISTIC BYPASS (Ticket #334/337)
            if not self.use_llm_reasoning:
                logger.info(
                    "Deterministic bypass active. Skipping LLM personas.",
                    extra={"correlation_id": context.correlation_id},
                )
                # In bypass mode we "blindly" trust the intent when risk gates pass.
                return await self.action_classifier.classify(
                    context,
                    code_result,
                    bypass_regime,
                    bypass_strategy,
                    bypass_mode=True,
                )

            # 2. REGIME ANALYSIS (S3-S5)
            # Check cache first for HOT path
            regime = None
            if self.cache:
                cached_regime = await self.cache.get(f"regime:{context.strategy_id}")
                if cached_regime:
                    try:
                        regime = RegimeResult.model_validate_json(cached_regime)
                        logger.debug("Regime cache hit. Using cached result.")
                    except Exception as e:
                        logger.warning(f"Failed to validate cached regime: {e}")

            if not regime:
                logger.info(
                    "Running Regime Classifier (LLM)...",
                    extra={"correlation_id": context.correlation_id},
                )
                regime = await self.regime_analyst.classify(context)
                if self.cache:
                    await self.cache.set(
                        f"regime:{context.strategy_id}",
                        regime.model_dump_json(),
                        ttl=900,
                    )

            # 3. STRATEGY ASSESSMENT (S3-S5)
            strategy = None
            if self.cache:
                cached_strategy = await self.cache.get(
                    f"strategy:{context.strategy_id}"
                )
                if cached_strategy:
                    try:
                        strategy = StrategyResult.model_validate_json(cached_strategy)
                        logger.debug("Strategy cache hit. Using cached result.")
                    except Exception as e:
                        logger.warning(f"Failed to validate cached strategy: {e}")

            if not strategy:
                logger.info(
                    "Running Strategy Assessor (LLM)...",
                    extra={"correlation_id": context.correlation_id},
                )
                strategy = await self.strategy_assessor.assess(context)
                if self.cache:
                    await self.cache.set(
                        f"strategy:{context.strategy_id}",
                        strategy.model_dump_json(),
                        ttl=900,
                    )

            # 4. ACTION CLASSIFICATION
            logger.info(
                "Running Final Action Classifier (LLM)...",
                extra={"correlation_id": context.correlation_id},
            )
            decision = await self.action_classifier.classify(
                context, code_result, regime, strategy
            )

            # FR63 / AC4 — ceiling check after each LLM decision cycle.
            await self._check_spend_ceiling(context.correlation_id)

            latency_ms = int((time.perf_counter() - start_time) * 1000)
            logger.info(
                f"✅ REASONING LOOP COMPLETE | Action: {decision.action} | Latency: {latency_ms}ms",
                extra={
                    "correlation_id": context.correlation_id,
                    "latency_ms": latency_ms,
                    "action": str(decision.action),
                    "llm_provider": provider_name,
                },
            )

            return decision

        except Exception as e:
            logger.exception(
                f"Critical failure in reasoning loop: {str(e)}",
                extra={"correlation_id": context.correlation_id},
            )
            return SAFE_DECISION_RESULT

    async def _check_spend_ceiling(self, correlation_id: str) -> None:
        """FR63 / AC4: check LLM spend ceiling; transition to deterministic bypass on breach.

        On period roll (new UTC day), restore LLM reasoning if the ceiling previously
        triggered the bypass (AC5 recovery path).
        """
        tracker = LlmSpendTracker.instance()
        breached, total_cost, projected = tracker.check_ceiling()

        if not breached and self._ceiling_triggered_bypass:
            # New period: projected spend reset below ceiling — restore LLM mode.
            self._ceiling_triggered_bypass = False
            self.use_llm_reasoning = True
            logger.info(
                "FR63: New UTC period — LLM reasoning re-enabled after ceiling reset.",
                extra={"total_cost_usd": total_cost, "correlation_id": correlation_id},
            )
            return

        if breached and self.use_llm_reasoning:
            # Transition to deterministic fallback for the rest of the period.
            self.use_llm_reasoning = False
            self._ceiling_triggered_bypass = True
            logger.warning(
                "FR63: LLM spend ceiling breached — switching to deterministic bypass (FR13).",
                extra={
                    "projected_daily_usd": projected,
                    "ceiling_usd": tracker._current.ceiling_usd_per_day,
                    "correlation_id": correlation_id,
                },
            )
            try:
                from cio.core.alerting.manager import AlertManager

                await AlertManager.dispatch_critical_alert(
                    "LLM daily spend ceiling breached — CIO switched to deterministic-fallback mode.",
                    context={
                        "alert_type": "RED",
                        "correlation_id": correlation_id,
                        "projected_daily_usd": projected,
                        "ceiling_usd": tracker._current.ceiling_usd_per_day,
                        "fr": "FR63+FR66",
                    },
                )
            except Exception as alert_err:
                logger.error(
                    "FR63: Failed to dispatch ceiling-breach alert: %s", alert_err
                )
