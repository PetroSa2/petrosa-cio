import logging
import os
from typing import Any

import yaml

from cio.clients.llm_client import CIO_LLM_Client
from cio.models import StrategyResult, TriggerContext

logger = logging.getLogger(__name__)

PROMPT_ID = "PETROSA_PROMPT_STRATEGY_ASSESSOR"


class StrategyAssessor:
    """
    Persona responsible for evaluating strategy health and regime fit.
    Compresses strategy performance and market regime into specific signals for LLM analysis.
    """

    def __init__(self, llm_client: CIO_LLM_Client):
        self.client = llm_client
        self.system_prompt = self._load_system_prompt()
        if not self.system_prompt:
            logger.warning(
                f"StrategyAssessor initialized with an empty system prompt for {PROMPT_ID}."
            )

    async def assess(self, context: TriggerContext) -> StrategyResult:
        """
        Runs the LLM assessment loop for the strategy's health and fit.
        """
        user_context = self._build_user_context(context)

        # complete_with_schema handles Pydantic validation and SAFE_DEFAULTS fallback
        result = await self.client.complete_with_schema(
            prompt_id=PROMPT_ID,
            system_prompt=self.system_prompt,
            user_context=user_context,
            response_model=StrategyResult,
        )

        return result

    def _build_user_context(self, context: TriggerContext) -> dict[str, Any]:
        """
        Compresses TriggerContext into the minimal schema required by the prompt.
        """
        stats = context.strategy_stats
        regime = context.regime
        return {
            "strategy_id": context.strategy_id,
            "win_rate": stats.win_rate,
            "win_rate_delta": stats.win_rate_delta,
            "consecutive_losses": stats.consecutive_losses,
            "recent_pnl_trend": stats.recent_pnl_trend.value
            if stats.recent_pnl_trend
            else None,
            "regime": regime.regime.value if regime.regime else None,
            "regime_confidence": regime.regime_confidence.value
            if regime.regime_confidence
            else None,
            "historical_context": context.historical_context,
        }

    def _load_system_prompt(self) -> str:
        """
        Loads the system prompt from the central prompt registry.
        Pattern matched from MockLLMClient.
        """
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        prompts_dir = os.path.join(base_dir, "prompts")
        yaml_path = os.path.join(prompts_dir, "strategy_assessor_v1.yaml")

        try:
            with open(yaml_path) as f:
                data = yaml.safe_load(f)
                return data.get("system_prompt", "")
        except Exception as e:
            logger.error(f"Failed to load system prompt from {yaml_path}: {e}")
            return ""
