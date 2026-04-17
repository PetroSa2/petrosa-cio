"""LLM_CAPABILITY_PROFILE: minimal vs standard prompts and LiteLLM json_object gating."""

import os
from unittest.mock import AsyncMock, patch

import pytest

from cio.clients.llm_client import (
    LiteLLMClient,
    MockLLMClient,
    resolve_llm_capability_profile,
)
from cio.models import ActionResult, RawLLMResponse, RegimeResult, StrategyResult
from cio.models.enums import ActionType


def test_resolve_llm_capability_profile():
    with patch.dict(os.environ, {"LLM_CAPABILITY_PROFILE": "minimal"}):
        assert resolve_llm_capability_profile() == "minimal"
    with patch.dict(os.environ, {"LLM_CAPABILITY_PROFILE": "standard"}):
        assert resolve_llm_capability_profile() == "standard"
    with patch.dict(os.environ, {"LLM_CAPABILITY_PROFILE": "invalid"}):
        with pytest.raises(ValueError, match="Invalid LLM_CAPABILITY_PROFILE"):
            resolve_llm_capability_profile()
    # Default is standard
    with patch.dict(os.environ, {}, clear=True):
        assert resolve_llm_capability_profile() == "standard"


def test_capability_profile_initialization():
    with patch.dict(os.environ, {"LLM_CAPABILITY_PROFILE": "minimal"}):
        client = MockLLMClient()
        assert client.capability_profile == "minimal"


@pytest.mark.asyncio
async def test_minimal_profile_injects_missing_thought_trace():
    """minimal variant of prompts omit thought_trace; client must inject empty string to pass validation."""
    with patch.dict(os.environ, {"LLM_CAPABILITY_PROFILE": "minimal"}):
        client = LiteLLMClient()
        assert client.capability_profile == "minimal"

        # Mock LiteLLM call
        client._call_litellm = AsyncMock(
            return_value='{"action": "execute", "justification": "Minimal test"}'
        )

        # 1. Action Classifier
        out = await client.complete_with_schema(
            prompt_id="PETROSA_PROMPT_ACTION_CLASSIFIER",
            system_prompt="",
            user_context={},
            response_model=ActionResult,
        )
        assert isinstance(out, ActionResult)
        assert out.thought_trace == ""
        assert out.action == ActionType.EXECUTE

        # 2. Regime Classifier
        client._call_litellm.return_value = (
            '{"regime": "trending_bull", "confidence": 0.9, "fit": "good"}'
        )
        out = await client.complete_with_schema(
            prompt_id="PETROSA_PROMPT_REGIME_CLASSIFIER",
            system_prompt="",
            user_context={},
            response_model=RegimeResult,
        )
        assert isinstance(out, RegimeResult)
        assert out.thought_trace == ""

        # 3. Strategy Assessor
        client._call_litellm.return_value = '{"strategy_id": "test", "health": "healthy", "activation_recommendation": "run", "confidence": 1.0, "regime_fit": "good"}'
        out = await client.complete_with_schema(
            prompt_id="PETROSA_PROMPT_STRATEGY_ASSESSOR",
            system_prompt="",
            user_context={},
            response_model=StrategyResult,
        )
        assert isinstance(out, StrategyResult)
        assert out.thought_trace == ""
        assert out.param_change is None


def _raw_regime(content: str) -> RawLLMResponse:
    from datetime import datetime

    try:
        from datetime import UTC
    except ImportError:
        UTC = UTC
    return RawLLMResponse(
        prompt_id="PETROSA_PROMPT_REGIME_CLASSIFIER",
        content=content,
        error=None,
        latency_ms=100.0,
        timestamp=datetime.now(UTC),
    )
