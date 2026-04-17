import logging
import os
from typing import Any

import yaml

from cio.clients.llm_client import CIO_LLM_Client
from cio.core.assembler import DecisionAssembler
from cio.models import (
    ActionResult,
    ActionType,
    CodeEngineResult,
    DecisionResult,
    RegimeResult,
    StrategyResult,
    TriggerContext,
)
from cio.prompts.loader import select_system_prompt

logger = logging.getLogger(__name__)

PROMPT_ID = "PETROSA_PROMPT_ACTION_CLASSIFIER"


class ActionClassifier:
    """
    Persona responsible for the final arbitration of trading decisions.
    Synthesizes signals from Code Engine and other personas into a final ActionType.
    """

    def __init__(self, llm_client: CIO_LLM_Client):
        self.client = llm_client
        self.system_prompt = self._load_system_prompt()
        if not self.system_prompt:
            logger.warning(
                f"ActionClassifier initialized with an empty system prompt for {PROMPT_ID}."
            )

    async def classify(
        self,
        context: TriggerContext,
        code_result: CodeEngineResult,
        regime_result: RegimeResult,
        strategy_result: StrategyResult,
        bypass_mode: bool = False,
    ) -> DecisionResult:
        """
        Runs the final LLM classification loop to determine the ActionType.
        Assembles the final DecisionResult.
        """
        if bypass_mode:
            # Ticket #334/337: Deterministic bypass
            # If not hard blocked, we default to EXECUTE
            action = (
                ActionType.EXECUTE if not code_result.hard_blocked else ActionType.BLOCK
            )

            if action == ActionType.BLOCK:
                justification = (
                    "Deterministic bypass: Blocking based on Code Engine hard block"
                    f" (NURSE_USE_LLM_REASONING=false). Reason: {code_result.block_reason}"
                    if code_result.block_reason
                    else "Deterministic bypass: Blocking based on Code Engine hard block "
                    "(NURSE_USE_LLM_REASONING=false)."
                )
            else:
                justification = "Deterministic bypass: Executing based on Code Engine approval (NURSE_USE_LLM_REASONING=false)."

            return DecisionAssembler.assemble(
                context=context,
                code_result=code_result,
                regime_result=regime_result,
                strategy_result=strategy_result,
                llm_action=action,
                llm_justification=justification,
            )

        user_context = self._build_user_context(
            context, code_result, regime_result, strategy_result
        )

        # complete_with_schema handles Pydantic validation and SAFE_DEFAULTS fallback
        action_result = await self.client.complete_with_schema(
            prompt_id=PROMPT_ID,
            system_prompt=self.system_prompt,
            user_context=user_context,
            response_model=ActionResult,
        )

        # Assembles the raw action from LLM into the final full DecisionResult
        return DecisionAssembler.assemble(
            context=context,
            code_result=code_result,
            regime_result=regime_result,
            strategy_result=strategy_result,
            llm_action=action_result.action,
            llm_justification=action_result.justification,
        )

    def _build_user_context(
        self,
        context: TriggerContext,
        code_result: CodeEngineResult,
        regime_result: RegimeResult,
        strategy_result: StrategyResult,
    ) -> dict[str, Any]:
        """
        Compresses input into the minimal schema required by the prompt.
        """
        return {
            "strategy_id": context.strategy_id,
            "regime": regime_result.regime.value if regime_result.regime else None,
            "regime_confidence": (
                regime_result.regime_confidence.value
                if regime_result.regime_confidence
                else None
            ),
            "health": strategy_result.health.value if strategy_result.health else None,
            "activation_recommendation": (
                strategy_result.activation_recommendation.value
                if strategy_result.activation_recommendation
                else None
            ),
            "regime_fit": (
                strategy_result.regime_fit.value if strategy_result.regime_fit else None
            ),
            "gross_ev": code_result.gross_ev,
            "ev_unavailable": code_result.ev_unavailable,
            "kelly_position_usd": code_result.kelly_position_usd,
            "hard_blocked": code_result.hard_blocked,
            "risk_warnings": code_result.risk_warnings,
            "historical_context": context.historical_context,
        }

    def _load_system_prompt(self) -> str:
        """
        Loads the system prompt from the central prompt registry.
        """
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        prompts_dir = os.path.join(base_dir, "prompts")
        yaml_path = os.path.join(prompts_dir, "action_classifier_v1.yaml")

        try:
            with open(yaml_path) as f:
                data = yaml.safe_load(f)
                return select_system_prompt(data, self.client.capability_profile)
        except Exception as e:
            logger.error(f"Failed to load system prompt from {yaml_path}: {e}")
            return ""
