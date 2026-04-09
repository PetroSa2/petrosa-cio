"""
Unit tests for LiteLLMClient fixes:
  - AC2: fallback model fires on schema/validation failure
  - AC1: response_format=json_object set when api_base is present
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel

from cio.clients.llm_client import LiteLLMClient
from cio.models import RawLLMResponse

# ---------------------------------------------------------------------------
# Minimal response model for testing
# ---------------------------------------------------------------------------


class _FakeResponse(BaseModel):
    value: str


# ---------------------------------------------------------------------------
# Helpers to build RawLLMResponse fixtures
# ---------------------------------------------------------------------------


def _raw(content: str, model: str = "test-model", error: str | None = None):
    return RawLLMResponse(
        prompt_id="PETROSA_PROMPT_ACTION_CLASSIFIER",
        content=content,
        error=error,
        model=model,
        input_tokens=10,
        output_tokens=10,
        latency_ms=50,
        timestamp=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# AC2: schema fallback fires when primary returns invalid JSON
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schema_fallback_called_on_validation_failure():
    """
    When complete() returns non-JSON content, _schema_fallback() must be called
    and its valid response must be returned instead of SAFE_DEFAULTS.
    """
    client = LiteLLMClient()

    valid_json = '{"value": "ok"}'

    # Primary: returns malformed JSON → triggers ValidationError / JSONDecodeError
    client.complete = AsyncMock(return_value=_raw("NOT_JSON"))
    # Fallback: returns valid JSON
    client._schema_fallback = AsyncMock(
        return_value=_raw(valid_json, model="fallback-model")
    )

    result = await client.complete_with_schema(
        prompt_id="PETROSA_PROMPT_ACTION_CLASSIFIER",
        system_prompt="sys",
        user_context={},
        response_model=_FakeResponse,
    )

    client._schema_fallback.assert_awaited_once()
    assert isinstance(result, _FakeResponse)
    assert result.value == "ok"


@pytest.mark.asyncio
async def test_safe_defaults_returned_when_both_models_fail():
    """
    When primary AND fallback both produce invalid JSON, SAFE_DEFAULTS is returned.
    """
    from cio.models import SAFE_DEFAULTS

    client = LiteLLMClient()

    client.complete = AsyncMock(return_value=_raw("BAD_PRIMARY"))
    client._schema_fallback = AsyncMock(
        return_value=_raw("BAD_FALLBACK", model="fallback-model")
    )

    result = await client.complete_with_schema(
        prompt_id="PETROSA_PROMPT_ACTION_CLASSIFIER",
        system_prompt="sys",
        user_context={},
        response_model=_FakeResponse,
    )

    assert result == SAFE_DEFAULTS["PETROSA_PROMPT_ACTION_CLASSIFIER"]


@pytest.mark.asyncio
async def test_safe_defaults_returned_when_fallback_not_available():
    """
    When _schema_fallback returns None (e.g. MockLLMClient), SAFE_DEFAULTS is returned.
    """
    from cio.models import SAFE_DEFAULTS

    client = LiteLLMClient()

    client.complete = AsyncMock(return_value=_raw("NOT_JSON"))
    client._schema_fallback = AsyncMock(return_value=None)

    result = await client.complete_with_schema(
        prompt_id="PETROSA_PROMPT_ACTION_CLASSIFIER",
        system_prompt="sys",
        user_context={},
        response_model=_FakeResponse,
    )

    assert result == SAFE_DEFAULTS["PETROSA_PROMPT_ACTION_CLASSIFIER"]


# ---------------------------------------------------------------------------
# AC1: response_format set unconditionally when api_base is present
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_response_format_json_set_when_api_base_present():
    """
    When LLM_API_BASE is set (Requesty proxy), response_format=json_object must be
    passed to litellm.acompletion regardless of litellm.get_supported_openai_params().
    """
    client = LiteLLMClient()

    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = '{"value": "proxy_ok"}'
    mock_response.model = "openai/novita/llama-3.1-8b"
    mock_response.usage.prompt_tokens = 10
    mock_response.usage.completion_tokens = 10
    mock_response.usage.prompt_tokens_details = None

    with (
        patch.dict(
            "os.environ",
            {
                "LLM_API_BASE": "https://router.requesty.ai/v1",
                "LLM_MODEL": "novita/llama-3.1-8b",
            },
        ),
        patch(
            "litellm.acompletion", new_callable=AsyncMock, return_value=mock_response
        ) as mock_acompletion,
    ):
        await client.complete(
            prompt_id="test",
            system_prompt="sys",
            user_context={},
        )

    call_kwargs = mock_acompletion.call_args.kwargs
    assert call_kwargs.get("response_format") == {"type": "json_object"}, (
        f"Expected json_object response_format, got: {call_kwargs.get('response_format')}"
    )
