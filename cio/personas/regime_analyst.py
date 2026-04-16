import logging
import os
from typing import Any

import yaml

from cio.clients.llm_client import CIO_LLM_Client
from cio.models import RegimeResult, TriggerContext
from cio.prompts.loader import select_system_prompt

logger = logging.getLogger(__name__)

PROMPT_ID = "PETROSA_PROMPT_REGIME_CLASSIFIER"


class RegimeAnalyst:
    """
    Persona responsible for market regime classification.
    Compresses broad context into specific market signals for LLM analysis.
    """

    def __init__(self, llm_client: CIO_LLM_Client):
        self.client = llm_client
        self.system_prompt = self._load_system_prompt()

    async def classify(self, context: TriggerContext) -> RegimeResult:
        """
        Runs the LLM classification loop for the current market regime.
        """
        user_context = self._build_user_context(context)

        # complete_with_schema handles Pydantic validation and SAFE_DEFAULTS fallback
        result = await self.client.complete_with_schema(
            prompt_id=PROMPT_ID,
            system_prompt=self.system_prompt,
            user_context=user_context,
            response_model=RegimeResult,
        )

        return result

    def _build_user_context(self, context: TriggerContext) -> dict[str, Any]:
        """
        Compresses TriggerContext into the minimal schema required by the prompt.
        """
        signals = context.market_signals
        return {
            "signal_summary": signals.signal_summary,
            "volatility_percentile": signals.volatility_percentile,
            "trend_strength": signals.trend_strength,
            "price_action_character": signals.price_action_character,
        }

    def _load_system_prompt(self) -> str:
        """
        Loads the system prompt from the central prompt registry.
        Pattern matched from MockLLMClient.
        """
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        prompts_dir = os.path.join(base_dir, "prompts")
        yaml_path = os.path.join(prompts_dir, "regime_classifier_v1.yaml")

        try:
            with open(yaml_path) as f:
                data = yaml.safe_load(f)
                return select_system_prompt(data, self.client.capability_profile)
        except Exception as e:
            logger.error(f"Failed to load system prompt from {yaml_path}: {e}")
            return ""
