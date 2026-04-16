"""
Unit tests for LiteLLMClient fixes:
  - AC2: fallback model fires on schema/validation failure
  - AC1: response_format=json_object only when supported/configured
"""

import sys
from datetime import UTC, datetime
from types import SimpleNamespace
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
async def test_safe_default_emits_parse_failure_skip_metric_and_log(caplog):
    from cio.models import SAFE_DEFAULTS

    client = LiteLLMClient()
    client.complete = AsyncMock(return_value=_raw("BAD_PRIMARY"))
    client._schema_fallback = AsyncMock(return_value=_raw("BAD_FALLBACK"))

    with patch("cio.core.metrics.LLM_FALLBACK_SKIPS") as mock_counter:
        result = await client.complete_with_schema(
            prompt_id="PETROSA_PROMPT_ACTION_CLASSIFIER",
            system_prompt="sys",
            user_context={},
            response_model=_FakeResponse,
        )

    assert result == SAFE_DEFAULTS["PETROSA_PROMPT_ACTION_CLASSIFIER"]
    mock_counter.labels.assert_called_with(
        prompt_id="PETROSA_PROMPT_ACTION_CLASSIFIER", reason="validation_error"
    )
    mock_counter.labels.return_value.inc.assert_called_once()
    assert "LLM_PARSE_FAILURE_SKIP" in caplog.text


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
# AC1: response_format requires model support + env toggle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_response_format_json_set_when_supported():
    """
    When JSON mode is enabled and model supports it, json_object format is requested.
    """
    client = LiteLLMClient()

    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = '{"value": "proxy_ok"}'
    mock_response.model = "openai/novita/llama-3.1-8b"
    mock_response.usage.prompt_tokens = 10
    mock_response.usage.completion_tokens = 10
    mock_response.usage.prompt_tokens_details = None

    litellm_patch, fake_litellm = _mock_litellm_runtime(
        acompletion_return=mock_response,
        supported_params=["json_object"],
    )
    with (
        patch.dict(
            "os.environ",
            {
                "LLM_API_BASE": "https://router.requesty.ai/v1",
                "LLM_MODEL": "novita/llama-3.1-8b",
            },
        ),
        litellm_patch,
    ):
        await client.complete(
            prompt_id="test",
            system_prompt="sys",
            user_context={},
        )

    call_kwargs = fake_litellm.acompletion.call_args.kwargs
    assert call_kwargs.get("response_format") == {"type": "json_object"}, (
        f"Expected json_object response_format, got: {call_kwargs.get('response_format')}"
    )


@pytest.mark.asyncio
async def test_response_format_json_not_set_when_env_disables_json_mode():
    client = LiteLLMClient()

    mock_response = _mock_litellm_response('{"value": "proxy_ok"}')

    litellm_patch, fake_litellm = _mock_litellm_runtime(
        acompletion_return=mock_response,
        supported_params=["json_object"],
    )
    with (
        patch.dict(
            "os.environ",
            {
                "LLM_API_BASE": "https://router.requesty.ai/v1",
                "LLM_MODEL": "novita/llama-3.1-8b",
                "LLM_SUPPORTS_JSON_MODE": "false",
            },
        ),
        litellm_patch,
    ):
        await client.complete(
            prompt_id="test",
            system_prompt="sys",
            user_context={},
        )

    call_kwargs = fake_litellm.acompletion.call_args.kwargs
    assert call_kwargs.get("response_format") is None


@pytest.mark.asyncio
async def test_model_prefix_can_be_disabled_for_requesty_routes():
    client = LiteLLMClient()
    mock_response = _mock_litellm_response(
        '{"value": "ok"}', model="novita/llama-3.1-8b"
    )

    litellm_patch, fake_litellm = _mock_litellm_runtime(
        acompletion_return=mock_response,
        supported_params=[],
    )
    with (
        patch.dict(
            "os.environ",
            {
                "LLM_API_BASE": "https://router.requesty.ai/v1",
                "LLM_MODEL": "novita/llama-3.1-8b",
                "LLM_MODEL_PREFIX": "",
            },
        ),
        litellm_patch,
    ):
        await client.complete(
            prompt_id="test",
            system_prompt="sys",
            user_context={},
        )

    call_kwargs = fake_litellm.acompletion.call_args.kwargs
    assert call_kwargs.get("model") == "novita/llama-3.1-8b"


# ---------------------------------------------------------------------------
# LiteLLMClient._schema_fallback: direct implementation coverage
# ---------------------------------------------------------------------------


def _mock_litellm_response(content: str, model: str = "openai/fallback"):
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    resp.model = model
    resp.usage.prompt_tokens = 10
    resp.usage.completion_tokens = 10
    resp.usage.prompt_tokens_details = None
    return resp


def _mock_litellm_runtime(
    *,
    acompletion_return: MagicMock | None = None,
    acompletion_side_effect: Exception | None = None,
    supported_params: list[str] | None = None,
):
    fake_litellm = SimpleNamespace(
        acompletion=AsyncMock(
            return_value=acompletion_return, side_effect=acompletion_side_effect
        ),
        get_supported_openai_params=MagicMock(return_value=supported_params or []),
    )
    fake_exceptions = SimpleNamespace(
        RateLimitError=RuntimeError,
        ServiceUnavailableError=RuntimeError,
    )
    return patch.dict(
        sys.modules,
        {"litellm": fake_litellm, "litellm.exceptions": fake_exceptions},
    ), fake_litellm


@pytest.mark.asyncio
async def test_schema_fallback_returns_raw_response_via_litellm():
    """
    LiteLLMClient._schema_fallback calls litellm.acompletion with the
    fallback model and returns a RawLLMResponse with the content.
    """
    client = LiteLLMClient()
    mock_resp = _mock_litellm_response('{"value": "fallback_ok"}')

    litellm_patch, _fake_litellm = _mock_litellm_runtime(acompletion_return=mock_resp)
    with (
        patch.dict(
            "os.environ",
            {
                "LLM_API_BASE": "https://router.requesty.ai/v1",
                "LLM_FALLBACK_MODEL": "novita/llama-3.1-8b",
            },
        ),
        litellm_patch,
    ):
        result = await client._schema_fallback(
            prompt_id="PETROSA_PROMPT_ACTION_CLASSIFIER",
            system_prompt="sys",
            user_context={},
        )

    assert result is not None
    assert result.error is None
    assert '{"value": "fallback_ok"}' in result.content


@pytest.mark.asyncio
async def test_schema_fallback_returns_none_on_litellm_exception():
    """
    If litellm raises during _schema_fallback, the method returns None
    rather than propagating the exception.
    """
    client = LiteLLMClient()

    litellm_patch, _fake_litellm = _mock_litellm_runtime(
        acompletion_side_effect=RuntimeError("boom")
    )
    with litellm_patch:
        result = await client._schema_fallback(
            prompt_id="PETROSA_PROMPT_ACTION_CLASSIFIER",
            system_prompt="sys",
            user_context={},
        )

    assert result is None


# ---------------------------------------------------------------------------
# Fence stripping: with and without closing fence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fence_stripping_with_closing_fence():
    """Content wrapped in ```json ... ``` is correctly unwrapped."""
    client = LiteLLMClient()
    fenced = '```json\n{"value": "fenced"}\n```'
    client.complete = AsyncMock(return_value=_raw(fenced))
    client._schema_fallback = AsyncMock(return_value=None)

    result = await client.complete_with_schema(
        prompt_id="PETROSA_PROMPT_ACTION_CLASSIFIER",
        system_prompt="sys",
        user_context={},
        response_model=_FakeResponse,
    )

    assert isinstance(result, _FakeResponse)
    assert result.value == "fenced"


@pytest.mark.asyncio
async def test_fence_stripping_without_closing_fence():
    """Content with opening ``` but no closing fence still parses correctly."""
    client = LiteLLMClient()
    # No closing fence — last line is part of JSON, must NOT be stripped
    fenced = '```json\n{"value": "no_close"}'
    client.complete = AsyncMock(return_value=_raw(fenced))
    client._schema_fallback = AsyncMock(return_value=None)

    result = await client.complete_with_schema(
        prompt_id="PETROSA_PROMPT_ACTION_CLASSIFIER",
        system_prompt="sys",
        user_context={},
        response_model=_FakeResponse,
    )

    assert isinstance(result, _FakeResponse)
    assert result.value == "no_close"
