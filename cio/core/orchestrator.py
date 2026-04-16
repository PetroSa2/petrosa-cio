import logging
import os
import time

from cio.clients.factory import ClientFactory
from cio.core.engine import CodeEngine
from cio.models import (
    SAFE_DECISION_RESULT,
    SAFE_DEFAULTS,
    DecisionResult,
    RegimeResult,
    StrategyResult,
    TriggerContext,
)
from cio.personas.action_classifier import ActionClassifier
from cio.personas.regime_analyst import RegimeAnalyst
from cio.personas.strategy_assessor import StrategyAssessor

logger = logging.getLogger(__name__)


class Orchestrator:
    """
    The central intelligence unit for the Petrosa CIO.
    Coordinates the Reasoning Loop, Code Engine, and Persona calls.
    """

    def __init__(self, llm_client=None, cache=None):
        self.client = llm_client or ClientFactory.create()
        self.cache = cache
        self.regime_analyst = RegimeAnalyst(self.client)
        self.strategy_assessor = StrategyAssessor(self.client)
        self.action_classifier = ActionClassifier(self.client)

        # Read governance flags from environment (Ticket #334/337)
        self.use_llm_reasoning = (
            os.getenv("NURSE_USE_LLM_REASONING", "true").lower() == "true"
        )
        if not self.use_llm_reasoning:
            logger.warning(
                "⚠️ NURSE_USE_LLM_REASONING is disabled. CIO will operate in deterministic bypass mode."
            )

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

        try:
            # 1. CODE ENGINE: Hard Limits (S2)
            code_result = CodeEngine.run(context)

            # Placeholder regime/strategy results for the assembler if we bypass
            regime_fallback = SAFE_DEFAULTS["PETROSA_PROMPT_REGIME_CLASSIFIER"]
            strategy_fallback = SAFE_DEFAULTS["PETROSA_PROMPT_STRATEGY_ASSESSOR"]

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
                    context, code_result, regime_fallback, strategy_fallback
                )

            # NEW: DETERMINISTIC BYPASS (Ticket #334/337)
            if not self.use_llm_reasoning:
                logger.info(
                    "Deterministic bypass active. Skipping LLM personas.",
                    extra={"correlation_id": context.correlation_id},
                )
                # In bypass mode, we "blindly" trust the intent IF code engine passes.
                # The ActionClassifier still handles the final assembly into DecisionResult.
                return await self.action_classifier.classify(
                    context,
                    code_result,
                    regime_fallback,
                    strategy_fallback,
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
