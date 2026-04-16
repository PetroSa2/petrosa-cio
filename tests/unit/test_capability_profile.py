"""LLM_CAPABILITY_PROFILE: minimal vs standard prompts and LiteLLM json_object gating."""

import os
import sys
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cio.clients.llm_client import (
    LiteLLMClient,
    MockLLMClient,
    resolve_llm_capability_profile,
)
from cio.models import ActionResult, RawLLMResponse, RegimeResult, StrategyResult
from cio.models.enums import ActionType
from cio.prompts.loader import select_system_prompt


def test_resolve_llm_capability_profile_accepts_standard():
    with patch.dict(os.environ, {"LLM_CAPABILITY_PROFILE": "standard"}, clear=False):
        assert resolve_llm_capability_profile() == "standard"


def test_resolve_llm_capability_profile_invalid_raises():
    with patch.dict(os.environ, {"LLM_CAPABILITY_PROFILE": "full"}, clear=False):
        with pytest.raises(ValueError) as exc_info:
            resolve_llm_capability_profile()
        assert "LLM_CAPABILITY_PROFILE" in str(exc_info.value)
        assert "full" in str(exc_info.value)


def test_select_system_prompt_prefers_minimal_key():
    data = {
        "system_prompt": "FULL",
        "system_prompt_minimal": "MIN",
    }
    assert select_system_prompt(data, "standard") == "FULL"
    assert select_system_prompt(data, "minimal") == "MIN"


@pytest.mark.asyncio
async def test_lite_llm_minimal_never_sends_response_format_json():
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = '{"value": "x"}'
    mock_response.model = "m"
    mock_response.usage.prompt_tokens = 1
    mock_response.usage.completion_tokens = 1
    mock_response.usage.prompt_tokens_details = None

    fake_litellm = __import__("types").SimpleNamespace(
        acompletion=AsyncMock(return_value=mock_response),
        get_supported_openai_params=MagicMock(return_value=["json_object"]),
    )
    fake_exceptions = __import__("types").SimpleNamespace(
        RateLimitError=RuntimeError,
        ServiceUnavailableError=RuntimeError,
    )
    with (
        patch.dict(
            os.environ,
            {
                "LLM_CAPABILITY_PROFILE": "minimal",
                "LLM_API_BASE": "https://router.requesty.ai/v1",
                "LLM_MODEL": "novita/llama-3.1-8b",
                "LLM_SUPPORTS_JSON_MODE": "true",
            },
        ),
        patch.dict(
            sys.modules,
            {"litellm": fake_litellm, "litellm.exceptions": fake_exceptions},
        ),
    ):
        client = LiteLLMClient()
        await client.complete(prompt_id="p", system_prompt="s", user_context={})

    assert fake_litellm.acompletion.call_args.kwargs.get("response_format") is None


@pytest.mark.asyncio
async def test_lite_llm_standard_respects_json_mode_gate():
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = '{"value": "x"}'
    mock_response.model = "m"
    mock_response.usage.prompt_tokens = 1
    mock_response.usage.completion_tokens = 1
    mock_response.usage.prompt_tokens_details = None

    fake_litellm = __import__("types").SimpleNamespace(
        acompletion=AsyncMock(return_value=mock_response),
        get_supported_openai_params=MagicMock(return_value=["json_object"]),
    )
    fake_exceptions = __import__("types").SimpleNamespace(
        RateLimitError=RuntimeError,
        ServiceUnavailableError=RuntimeError,
    )
    with (
        patch.dict(
            os.environ,
            {
                "LLM_CAPABILITY_PROFILE": "standard",
                "LLM_API_BASE": "https://router.requesty.ai/v1",
                "LLM_MODEL": "novita/llama-3.1-8b",
                "LLM_SUPPORTS_JSON_MODE": "true",
            },
        ),
        patch.dict(
            sys.modules,
            {"litellm": fake_litellm, "litellm.exceptions": fake_exceptions},
        ),
    ):
        client = LiteLLMClient()
        await client.complete(prompt_id="p", system_prompt="s", user_context={})

    assert fake_litellm.acompletion.call_args.kwargs.get("response_format") == {
        "type": "json_object"
    }


@pytest.mark.asyncio
async def test_mock_complete_with_schema_minimal_no_safe_defaults():
    """Minimal mock JSON omits thought_trace; Pydantic still validates."""
    action_ctx = {
        "strategy_id": "s1",
        "regime": "ranging",
        "regime_confidence": "medium",
        "health": "healthy",
        "activation_recommendation": "run",
        "regime_fit": "good",
        "gross_ev": 1.0,
        "ev_unavailable": False,
        "kelly_position_usd": 100.0,
        "hard_blocked": False,
        "risk_warnings": [],
    }
    with patch.dict(os.environ, {"LLM_CAPABILITY_PROFILE": "minimal"}, clear=False):
        client = MockLLMClient()
        out = await client.complete_with_schema(
            prompt_id="PETROSA_PROMPT_ACTION_CLASSIFIER",
            system_prompt="sys",
            user_context=action_ctx,
            response_model=ActionResult,
        )
    assert isinstance(out, ActionResult)
    assert out.thought_trace == ""
    assert out.action in (ActionType.EXECUTE, ActionType.SKIP, ActionType.BLOCK)


@pytest.mark.asyncio
async def test_mock_complete_with_schema_minimal_regime():
    regime_ctx = {
        "signal_summary": "x",
        "volatility_percentile": 0.5,
        "trend_strength": 0.0,
        "price_action_character": "neutral",
    }
    with patch.dict(os.environ, {"LLM_CAPABILITY_PROFILE": "minimal"}, clear=False):
        client = MockLLMClient()
        out = await client.complete_with_schema(
            prompt_id="PETROSA_PROMPT_REGIME_CLASSIFIER",
            system_prompt="sys",
            user_context=regime_ctx,
            response_model=RegimeResult,
        )
    assert isinstance(out, RegimeResult)
    assert out.thought_trace == ""


@pytest.mark.asyncio
async def test_mock_complete_with_schema_minimal_strategy():
    strat_ctx = {
        "strategy_id": "s1",
        "win_rate": 0.5,
        "win_rate_delta": 0.0,
        "consecutive_losses": 0,
        "recent_pnl_trend": "neutral",
        "regime": "ranging",
        "regime_confidence": "medium",
    }
    with patch.dict(os.environ, {"LLM_CAPABILITY_PROFILE": "minimal"}, clear=False):
        client = MockLLMClient()
        out = await client.complete_with_schema(
            prompt_id="PETROSA_PROMPT_STRATEGY_ASSESSOR",
            system_prompt="sys",
            user_context=strat_ctx,
            response_model=StrategyResult,
        )
    assert isinstance(out, StrategyResult)
    assert out.thought_trace == ""
    assert out.param_change is None


def _raw_regime(content: str) -> RawLLMResponse:
    return RawLLMResponse(
        prompt_id="PETROSA_PROMPT_REGIME_CLASSIFIER",
        content=content,
        error=None,
        model="test",
        input_tokens=1,
        output_tokens=1,
        latency_ms=1,
        timestamp=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_standard_missing_thought_trace_still_triggers_safe_defaults():
    """Standard profile: omitted thought_trace must not silently validate."""
    from cio.models import SAFE_DEFAULTS

    content = (
        '{"regime":"choppy","regime_confidence":"low",'
        '"volatility_level":"medium","primary_signal":"x"}'
    )
    with patch.dict(os.environ, {"LLM_CAPABILITY_PROFILE": "standard"}, clear=False):
        client = LiteLLMClient()
        client.complete = AsyncMock(return_value=_raw_regime(content))
        client._schema_fallback = AsyncMock(return_value=None)
        out = await client.complete_with_schema(
            prompt_id="PETROSA_PROMPT_REGIME_CLASSIFIER",
            system_prompt="sys",
            user_context={},
            response_model=RegimeResult,
        )
    assert out == SAFE_DEFAULTS["PETROSA_PROMPT_REGIME_CLASSIFIER"]


@pytest.mark.asyncio
async def test_minimal_injects_empty_thought_trace_for_valid_regime_json():
    """Minimal profile: missing thought_trace is injected before validation."""
    content = (
        '{"regime":"choppy","regime_confidence":"low",'
        '"volatility_level":"medium","primary_signal":"x"}'
    )
    with patch.dict(os.environ, {"LLM_CAPABILITY_PROFILE": "minimal"}, clear=False):
        client = LiteLLMClient()
        client.complete = AsyncMock(return_value=_raw_regime(content))
        client._schema_fallback = AsyncMock(return_value=None)
        out = await client.complete_with_schema(
            prompt_id="PETROSA_PROMPT_REGIME_CLASSIFIER",
            system_prompt="sys",
            user_context={},
            response_model=RegimeResult,
        )
    assert isinstance(out, RegimeResult)
    assert out.thought_trace == ""


@pytest.mark.asyncio
async def test_mock_complete_with_schema_standard_includes_traces():
    regime_ctx = {
        "signal_summary": "x",
        "volatility_percentile": 0.5,
        "trend_strength": 0.0,
        "price_action_character": "neutral",
    }
    with patch.dict(os.environ, {"LLM_CAPABILITY_PROFILE": "standard"}, clear=False):
        client = MockLLMClient()
        out = await client.complete_with_schema(
            prompt_id="PETROSA_PROMPT_REGIME_CLASSIFIER",
            system_prompt="sys",
            user_context=regime_ctx,
            response_model=RegimeResult,
        )
    assert isinstance(out, RegimeResult)
    assert len(out.thought_trace) > 0
