"""
Unit tests for LiteLLMClient fixes:
  - AC2: fallback model fires on schema/validation failure
  - AC1: response_format=json_object only when supported/configured
"""

import json
import logging
import os
import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel, Field

from cio.clients import llm_client as llm_client_module
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
    mock_counter.add.assert_called_once_with(
        1,
        {"prompt_id": "PETROSA_PROMPT_ACTION_CLASSIFIER", "reason": "validation_error"},
    )
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
# #187: recovery — brace-extraction + string-field clamping before falling
# back to the schema-fallback model / SAFE_DEFAULTS. Root cause: #1009's
# emergency LLM model swap (NVIDIA NIM meta/llama-3.1-8b-instruct EOL'd
# 2026-08-26) pinned CIO to an untested vision-instruct model with no live
# verification of strict bare-JSON / char-budget compliance, driving
# sustained LLM_PARSE_FAILURE_SKIP -> SAFE_DEFAULTS(SKIP) dominance.
# ---------------------------------------------------------------------------


class _ActionLikeResponse(BaseModel):
    """Mirrors ActionResult's shape (enum-free) for isolated recovery tests."""

    action: str
    justification: str = Field(max_length=200)
    thought_trace: str = Field(max_length=120)


@pytest.mark.asyncio
async def test_recovery_extracts_json_wrapped_in_prose_and_skips_fallback():
    """A model that ignores 'ONLY a JSON object' and wraps the payload in
    conversational prose is recovered via brace-extraction — no fallback
    model call, no SAFE_DEFAULTS."""
    client = LiteLLMClient()
    wrapped = (
        "Sure, here is my decision:\n"
        '{"value": "ok"}\n'
        "Let me know if you need anything else!"
    )
    client.complete = AsyncMock(return_value=_raw(wrapped))
    client._schema_fallback = AsyncMock(
        side_effect=AssertionError("should not be called")
    )

    result = await client.complete_with_schema(
        prompt_id="PETROSA_PROMPT_ACTION_CLASSIFIER",
        system_prompt="sys",
        user_context={},
        response_model=_FakeResponse,
    )

    assert isinstance(result, _FakeResponse)
    assert result.value == "ok"
    client._schema_fallback.assert_not_called()


@pytest.mark.asyncio
async def test_recovery_clamps_overlong_thought_trace_and_skips_fallback():
    """A structurally valid decision whose thought_trace overruns the
    prompt's 120-char budget is truncated and accepted, instead of being
    thrown away wholesale as a SAFE_DEFAULTS(SKIP)."""
    client = LiteLLMClient()
    overlong_trace = "x" * 200
    payload = json.dumps(
        {
            "action": "execute",
            "justification": "ok",
            "thought_trace": overlong_trace,
        }
    )
    client.complete = AsyncMock(return_value=_raw(payload))
    client._schema_fallback = AsyncMock(
        side_effect=AssertionError("should not be called")
    )

    with patch("cio.core.metrics.LLM_RESPONSE_RECOVERED") as mock_counter:
        result = await client.complete_with_schema(
            prompt_id="PETROSA_PROMPT_ACTION_CLASSIFIER",
            system_prompt="sys",
            user_context={},
            response_model=_ActionLikeResponse,
        )

    assert isinstance(result, _ActionLikeResponse)
    assert result.action == "execute"
    assert len(result.thought_trace) == 120
    assert result.thought_trace == overlong_trace[:120]
    client._schema_fallback.assert_not_called()
    mock_counter.add.assert_called_once_with(
        1, {"prompt_id": "PETROSA_PROMPT_ACTION_CLASSIFIER"}
    )


@pytest.mark.asyncio
async def test_recovery_does_not_mask_genuinely_missing_required_field():
    """Recovery must not invent data: a JSON object missing a required
    field still falls through to the fallback model / SAFE_DEFAULTS."""
    from cio.models import SAFE_DEFAULTS

    client = LiteLLMClient()
    # Valid JSON, but missing the required "value" field for _FakeResponse.
    incomplete = '{"other_field": "irrelevant"}'
    client.complete = AsyncMock(return_value=_raw(incomplete))
    client._schema_fallback = AsyncMock(return_value=None)

    result = await client.complete_with_schema(
        prompt_id="PETROSA_PROMPT_ACTION_CLASSIFIER",
        system_prompt="sys",
        user_context={},
        response_model=_FakeResponse,
    )

    assert result == SAFE_DEFAULTS["PETROSA_PROMPT_ACTION_CLASSIFIER"]
    client._schema_fallback.assert_awaited_once()


@pytest.mark.asyncio
async def test_recovery_applies_to_fallback_leg_too():
    """The fallback model's response is also eligible for brace-extraction
    recovery before giving up to SAFE_DEFAULTS."""
    client = LiteLLMClient()
    client.complete = AsyncMock(return_value=_raw("NOT_JSON"))
    client._schema_fallback = AsyncMock(
        return_value=_raw(
            'Here you go: {"value": "fb_ok"} (hope that helps)',
            model="fallback-model",
        )
    )

    result = await client.complete_with_schema(
        prompt_id="PETROSA_PROMPT_ACTION_CLASSIFIER",
        system_prompt="sys",
        user_context={},
        response_model=_FakeResponse,
    )

    assert isinstance(result, _FakeResponse)
    assert result.value == "fb_ok"


def test_extract_balanced_json_handles_nested_braces_and_strings():
    from cio.clients.llm_client import _extract_balanced_json

    text = 'prefix noise {"a": {"b": 1}, "c": "contains } brace"} suffix noise'
    extracted = _extract_balanced_json(text)
    assert extracted == '{"a": {"b": 1}, "c": "contains } brace"}'
    assert json.loads(extracted) == {"a": {"b": 1}, "c": "contains } brace"}


def test_extract_balanced_json_returns_none_without_braces():
    from cio.clients.llm_client import _extract_balanced_json

    assert _extract_balanced_json("no json here at all") is None


def test_extract_balanced_json_handles_escaped_quotes_inside_strings():
    """Exercises the in-string escape handling: an escaped quote must not
    be mistaken for the string terminator (which would desync brace
    depth counting on a `}` that appears later inside the same string)."""
    from cio.clients.llm_client import _extract_balanced_json

    text = 'prefix {"msg": "she said \\"hello\\" to me"} suffix noise'
    extracted = _extract_balanced_json(text)
    assert extracted == '{"msg": "she said \\"hello\\" to me"}'
    assert json.loads(extracted) == {"msg": 'she said "hello" to me'}


def test_extract_balanced_json_returns_none_when_never_balances():
    """An opening brace with no matching close (e.g. a truncated stream)
    must not be treated as extractable."""
    from cio.clients.llm_client import _extract_balanced_json

    assert _extract_balanced_json('prefix {"a": "unterminated') is None


def test_recover_validated_response_returns_none_for_non_dict_json():
    """Valid JSON that isn't an object (e.g. a bare array) can never
    satisfy a BaseModel and must not be treated as recoverable."""
    from cio.clients.llm_client import _recover_validated_response

    assert _recover_validated_response("[1, 2, 3]", _FakeResponse) is None


# ---------------------------------------------------------------------------
# #189: model self-reports its own documented `{"error": "MISSING_INPUT"}`
# contract sentinel (ABSOLUTE RULE 4 in every CIO prompt). Root cause
# live-reproduced against meta/llama-3.2-11b-vision-instruct: a sparse
# user_context deterministically returns this sentinel, which fails generic
# Pydantic validation on BOTH the primary and fallback legs (same context,
# same model) since none of the response models define an "error" field —
# producing the observed "schema fallback also failed" -> LLM_PARSE_FAILURE_SKIP
# even though the model behaved exactly as instructed.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_input_sentinel_short_circuits_to_safe_defaults_without_fallback_call():
    """Primary leg reporting MISSING_INPUT must skip the fallback call
    entirely — retrying is guaranteed to repeat the same self-reported
    error since the underlying context is unchanged."""
    from cio.models import SAFE_DEFAULTS

    client = LiteLLMClient()
    client.complete = AsyncMock(return_value=_raw('{"error": "MISSING_INPUT"}'))
    client._schema_fallback = AsyncMock(
        side_effect=AssertionError("fallback should not be called")
    )

    with patch("cio.core.metrics.LLM_MISSING_INPUT_SKIPS") as mock_counter:
        result = await client.complete_with_schema(
            prompt_id="PETROSA_PROMPT_ACTION_CLASSIFIER",
            system_prompt="sys",
            user_context={},
            response_model=_FakeResponse,
        )

    assert result == SAFE_DEFAULTS["PETROSA_PROMPT_ACTION_CLASSIFIER"]
    client._schema_fallback.assert_not_called()
    mock_counter.add.assert_called_once_with(
        1,
        {
            "prompt_id": "PETROSA_PROMPT_ACTION_CLASSIFIER",
            "reported_error": "MISSING_INPUT",
        },
    )


@pytest.mark.asyncio
async def test_missing_input_sentinel_emits_distinct_log_not_parse_failure_skip(caplog):
    """The distinct LLM_MISSING_INPUT_SKIP log line must fire instead of
    LLM_PARSE_FAILURE_SKIP for the self-reported contract sentinel."""
    caplog.set_level(logging.WARNING, logger="cio.clients.llm_client")

    client = LiteLLMClient()
    client.complete = AsyncMock(return_value=_raw('{"error": "MISSING_INPUT"}'))
    client._schema_fallback = AsyncMock(
        side_effect=AssertionError("fallback should not be called")
    )

    await client.complete_with_schema(
        prompt_id="PETROSA_PROMPT_ACTION_CLASSIFIER",
        system_prompt="sys",
        user_context={},
        response_model=_FakeResponse,
    )

    assert "LLM_MISSING_INPUT_SKIP" in caplog.text
    assert "LLM_PARSE_FAILURE_SKIP" not in caplog.text


@pytest.mark.asyncio
async def test_missing_input_sentinel_recovered_from_prose_wrapped_content():
    """The sentinel is detected even when wrapped in prose, via the same
    brace-extraction used for the recovery path."""
    from cio.models import SAFE_DEFAULTS

    client = LiteLLMClient()
    wrapped = 'I cannot decide with this data. {"error": "MISSING_INPUT"} Sorry!'
    client.complete = AsyncMock(return_value=_raw(wrapped))
    client._schema_fallback = AsyncMock(
        side_effect=AssertionError("fallback should not be called")
    )

    result = await client.complete_with_schema(
        prompt_id="PETROSA_PROMPT_ACTION_CLASSIFIER",
        system_prompt="sys",
        user_context={},
        response_model=_FakeResponse,
    )

    assert result == SAFE_DEFAULTS["PETROSA_PROMPT_ACTION_CLASSIFIER"]
    client._schema_fallback.assert_not_called()


@pytest.mark.asyncio
async def test_missing_input_sentinel_on_fallback_leg_short_circuits():
    """When the primary fails for an unrelated reason but the fallback leg
    (same model, same context) reports MISSING_INPUT, it must be classified
    the same way — not as a generic double schema-fallback failure."""
    from cio.models import SAFE_DEFAULTS

    client = LiteLLMClient()
    client.complete = AsyncMock(return_value=_raw("NOT_JSON"))
    client._schema_fallback = AsyncMock(
        return_value=_raw('{"error": "MISSING_INPUT"}', model="fallback-model")
    )

    with patch("cio.core.metrics.LLM_MISSING_INPUT_SKIPS") as mock_counter:
        result = await client.complete_with_schema(
            prompt_id="PETROSA_PROMPT_ACTION_CLASSIFIER",
            system_prompt="sys",
            user_context={},
            response_model=_FakeResponse,
        )

    assert result == SAFE_DEFAULTS["PETROSA_PROMPT_ACTION_CLASSIFIER"]
    mock_counter.add.assert_called_once_with(
        1,
        {
            "prompt_id": "PETROSA_PROMPT_ACTION_CLASSIFIER",
            "reported_error": "MISSING_INPUT",
            "leg": "fallback",
        },
    )


def test_extract_reported_error_detects_sentinel():
    from cio.clients.llm_client import _extract_reported_error

    assert _extract_reported_error('{"error": "MISSING_INPUT"}') == "MISSING_INPUT"


def test_extract_reported_error_returns_none_for_valid_decision():
    from cio.clients.llm_client import _extract_reported_error

    assert (
        _extract_reported_error(
            '{"action": "execute", "justification": "ok", "thought_trace": "t"}'
        )
        is None
    )


def test_extract_reported_error_returns_none_for_non_dict_json():
    from cio.clients.llm_client import _extract_reported_error

    assert _extract_reported_error("[1, 2, 3]") is None


def test_extract_reported_error_returns_none_for_empty_error_string():
    """An empty/falsy `error` value must not be treated as a genuine
    self-reported contract violation."""
    from cio.clients.llm_client import _extract_reported_error

    assert _extract_reported_error('{"error": ""}') is None


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


# ---------------------------------------------------------------------------
# Patch coverage: transport error, helpers, circuit breaker, embed, primary→fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_with_schema_transport_error_emits_fallback_skip(caplog):
    """Transport errors return SAFE_DEFAULTS with LLM_PARSE_FAILURE_SKIP (transport)."""
    from cio.models import SAFE_DEFAULTS

    caplog.set_level(logging.ERROR, logger="cio.clients.llm_client")

    client = LiteLLMClient()
    client.complete = AsyncMock(
        return_value=_raw("", error="upstream_timeout", model="x")
    )

    with patch("cio.core.metrics.LLM_FALLBACK_SKIPS") as mock_counter:
        result = await client.complete_with_schema(
            prompt_id="PETROSA_PROMPT_ACTION_CLASSIFIER",
            system_prompt="sys",
            user_context={},
            response_model=_FakeResponse,
        )

    assert result == SAFE_DEFAULTS["PETROSA_PROMPT_ACTION_CLASSIFIER"]
    mock_counter.add.assert_called_once_with(
        1,
        {"prompt_id": "PETROSA_PROMPT_ACTION_CLASSIFIER", "reason": "transport_error"},
    )
    assert "LLM_PARSE_FAILURE_SKIP" in caplog.text


@pytest.mark.asyncio
async def test_response_format_none_when_get_supported_openai_params_raises():
    """_supports_json_mode returns False when litellm raises — no json_object format."""
    client = LiteLLMClient()
    mock_response = _mock_litellm_response('{"x":1}')

    def _boom(_model):
        raise RuntimeError("no params")

    fake_litellm = SimpleNamespace(
        acompletion=AsyncMock(return_value=mock_response),
        get_supported_openai_params=MagicMock(side_effect=_boom),
    )
    fake_exceptions = SimpleNamespace(
        RateLimitError=RuntimeError,
        ServiceUnavailableError=RuntimeError,
    )
    with (
        patch.dict(
            os.environ,
            {
                "LLM_API_BASE": "https://router.requesty.ai/v1",
                "LLM_MODEL": "m",
            },
        ),
        patch.dict(
            sys.modules,
            {"litellm": fake_litellm, "litellm.exceptions": fake_exceptions},
        ),
    ):
        await client.complete(prompt_id="p", system_prompt="s", user_context={})

    assert fake_litellm.acompletion.call_args.kwargs.get("response_format") is None


def test_build_routing_model_no_api_base_unchanged():
    assert llm_client_module._build_routing_model("anthropic/x", None) == "anthropic/x"


def test_build_routing_model_idempotent_when_model_already_has_prefix():
    with patch.dict(
        os.environ,
        {"LLM_MODEL_PREFIX": "openai/", "LLM_API_BASE": "https://x/v1"},
    ):
        assert (
            llm_client_module._build_routing_model("openai/gpt-4o-mini", "https://x/v1")
            == "openai/gpt-4o-mini"
        )
        assert (
            llm_client_module._build_routing_model("novita/llama", "https://x/v1")
            == "openai/novita/llama"
        )


def test_env_bool_variants():
    with patch.dict(os.environ, {"EB": "false"}, clear=False):
        assert llm_client_module._env_bool("EB", default=True) is False
    with patch.dict(os.environ, {"EB": "on"}, clear=False):
        assert llm_client_module._env_bool("EB", default=False) is True


@pytest.mark.asyncio
async def test_circuit_breaker_open_skips_litellm():
    client = LiteLLMClient()
    client._breaker_open_until = __import__("time").time() + 3600

    fake_litellm = SimpleNamespace(
        acompletion=AsyncMock(side_effect=AssertionError("should not call")),
        get_supported_openai_params=MagicMock(),
    )
    fake_exceptions = SimpleNamespace(
        RateLimitError=RuntimeError,
        ServiceUnavailableError=RuntimeError,
    )
    with patch.dict(
        sys.modules,
        {"litellm": fake_litellm, "litellm.exceptions": fake_exceptions},
    ):
        out = await client.complete("p", "s", {})

    assert out.error == "CIRCUIT_BREAKER_OPEN"
    assert out.model == "circuit-breaker"
    fake_litellm.acompletion.assert_not_called()


@pytest.mark.asyncio
async def test_primary_raises_fallback_succeeds():
    """Non-retry primary exception → fallback acompletion succeeds."""
    client = LiteLLMClient()
    ok = _mock_litellm_response('{"ok":true}', model="fb")

    fake_litellm = SimpleNamespace(
        get_supported_openai_params=MagicMock(return_value=[]),
    )
    fake_litellm.acompletion = AsyncMock(side_effect=[ValueError("primary boom"), ok])

    fake_exceptions = SimpleNamespace(
        RateLimitError=RuntimeError,
        ServiceUnavailableError=RuntimeError,
    )
    with (
        patch.dict(os.environ, {}, clear=True),
        patch.dict(
            sys.modules,
            {"litellm": fake_litellm, "litellm.exceptions": fake_exceptions},
        ),
    ):
        out = await client.complete("p", "s", {})

    assert out.error is None
    assert fake_litellm.acompletion.await_count == 2


@pytest.mark.asyncio
async def test_embed_success_returns_vector():
    client = LiteLLMClient()
    emb = [0.1, 0.2]
    resp = SimpleNamespace(data=[{"embedding": emb}])

    fake_litellm = SimpleNamespace(
        aembedding=AsyncMock(return_value=resp),
    )
    with patch.dict(sys.modules, {"litellm": fake_litellm}):
        result = await client.embed("hello")

    assert result == emb
    fake_litellm.aembedding.assert_awaited_once()


@pytest.mark.asyncio
async def test_embed_with_api_base_does_not_double_prefix_embedding_model():
    """Embedding model ids that already include LLM_MODEL_PREFIX are not doubled."""
    client = LiteLLMClient()
    resp = SimpleNamespace(data=[{"embedding": [1.0]}])
    fake_litellm = SimpleNamespace(aembedding=AsyncMock(return_value=resp))
    with (
        patch.dict(
            os.environ,
            {
                "LLM_API_BASE": "https://router.requesty.ai/v1",
                "EMBEDDING_MODEL": "openai/text-embedding-3-small",
                "LLM_MODEL_PREFIX": "openai/",
            },
        ),
        patch.dict(sys.modules, {"litellm": fake_litellm}),
    ):
        await client.embed("x")

    kwargs = fake_litellm.aembedding.await_args.kwargs
    assert kwargs["model"] == "openai/text-embedding-3-small"
    assert kwargs["api_base"] == "https://router.requesty.ai/v1"


def test_process_response_records_cached_prompt_tokens():
    client = LiteLLMClient()
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = '{"a":1}'
    response.model = "m"
    response.usage.prompt_tokens = 3
    response.usage.completion_tokens = 2
    response.usage.prompt_tokens_details = MagicMock(cached_tokens=7)

    with (
        patch("cio.core.metrics.LLM_LATENCY") as lat,
        patch("cio.core.metrics.LLM_TOKENS") as tok,
    ):
        out = client._process_response("pid", response, 100)

    assert out.cached_tokens == 7
    lat.record.assert_called_once()
    assert tok.add.call_count >= 3


@pytest.mark.asyncio
async def test_embed_failure_returns_zero_vector():
    client = LiteLLMClient()
    fake_litellm = SimpleNamespace(
        aembedding=AsyncMock(side_effect=RuntimeError("no embed")),
    )
    with patch.dict(sys.modules, {"litellm": fake_litellm}):
        result = await client.embed("x")

    assert len(result) == 1536
    assert all(v == 0.0 for v in result)


@pytest.mark.asyncio
async def test_record_failure_trips_breaker_after_five_total_failures():
    """Five primary+fallback failures should log circuit breaker open."""
    client = LiteLLMClient()
    fake_litellm = SimpleNamespace(
        acompletion=AsyncMock(side_effect=RuntimeError("fail")),
        get_supported_openai_params=MagicMock(return_value=[]),
    )
    fake_exceptions = SimpleNamespace(
        RateLimitError=RuntimeError,
        ServiceUnavailableError=RuntimeError,
    )
    with (
        patch.dict(os.environ, {}, clear=True),
        patch.dict(
            sys.modules,
            {"litellm": fake_litellm, "litellm.exceptions": fake_exceptions},
        ),
    ):
        for _ in range(5):
            out = await client.complete("p", "s", {})
            assert out.error is not None

    assert client._failure_count >= 5
    assert client._breaker_open_until > 0


# ---------------------------------------------------------------------------
# LLM_FALLBACK_API_BASE — independent fallback routing (fixes #824)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fallback_uses_separate_api_base_when_env_set():
    """
    When LLM_FALLBACK_API_BASE is set, the fallback acompletion call must use
    fallback_api_base, not the primary api_base, so a broken proxy does not
    take down both models simultaneously.
    """
    client = LiteLLMClient()
    primary_base = "https://broken-proxy.example.com/v1"
    fallback_base = "https://direct.openai.com/v1"

    call_log: list[dict] = []

    async def _fake_acompletion(**kwargs):
        call_log.append({"api_base": kwargs.get("api_base"), "model": kwargs["model"]})
        if kwargs.get("api_base") == primary_base:
            raise RuntimeError("proxy down")
        ns = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content='{"action":"execute","justification":"ok","thought_trace":"t"}'
                    )
                )
            ],
            usage=SimpleNamespace(
                prompt_tokens=10, completion_tokens=5, prompt_tokens_details=None
            ),
            model="openai/gpt-4o-mini",
        )
        return ns

    fake_litellm = SimpleNamespace(
        acompletion=AsyncMock(side_effect=_fake_acompletion),
        get_supported_openai_params=MagicMock(return_value=[]),
    )
    fake_exceptions = SimpleNamespace(
        RateLimitError=RuntimeError,
        ServiceUnavailableError=RuntimeError,
    )
    env = {
        "LLM_API_BASE": primary_base,
        "LLM_FALLBACK_API_BASE": fallback_base,
        "LLM_MODEL": "openai/gpt-4o",
        "LLM_FALLBACK_MODEL": "openai/gpt-4o-mini",
    }
    with (
        patch.dict(os.environ, env),
        patch.dict(
            sys.modules,
            {"litellm": fake_litellm, "litellm.exceptions": fake_exceptions},
        ),
    ):
        result = await client.complete("PETROSA_PROMPT_ACTION_CLASSIFIER", "sys", {})

    assert result.error is None, (
        f"Expected success on fallback, got error: {result.error}"
    )
    fallback_calls = [c for c in call_log if c["api_base"] == fallback_base]
    assert len(fallback_calls) >= 1, "Fallback call must use LLM_FALLBACK_API_BASE"


@pytest.mark.asyncio
async def test_fallback_defaults_to_primary_api_base_when_env_unset():
    """
    When LLM_FALLBACK_API_BASE is not set, fallback inherits LLM_API_BASE
    (legacy behaviour unchanged).
    """
    client = LiteLLMClient()
    shared_base = "https://router.requesty.ai/v1"
    call_log: list[dict] = []
    call_count = 0

    async def _fake_acompletion(**kwargs):
        nonlocal call_count
        call_count += 1
        call_log.append({"api_base": kwargs.get("api_base")})
        if call_count == 1:
            raise RuntimeError("primary fail")
        ns = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content='{"action":"skip","justification":"ok","thought_trace":"t"}'
                    )
                )
            ],
            usage=SimpleNamespace(
                prompt_tokens=5, completion_tokens=3, prompt_tokens_details=None
            ),
            model="openai/gpt-4o-mini",
        )
        return ns

    fake_litellm = SimpleNamespace(
        acompletion=AsyncMock(side_effect=_fake_acompletion),
        get_supported_openai_params=MagicMock(return_value=[]),
    )
    fake_exceptions = SimpleNamespace(
        RateLimitError=RuntimeError,
        ServiceUnavailableError=RuntimeError,
    )
    env = {"LLM_API_BASE": shared_base, "LLM_FALLBACK_MODEL": "openai/gpt-4o-mini"}
    # LLM_FALLBACK_API_BASE deliberately absent
    with (
        patch.dict(os.environ, env),
        patch.dict(
            sys.modules,
            {"litellm": fake_litellm, "litellm.exceptions": fake_exceptions},
        ),
    ):
        result = await client.complete("PETROSA_PROMPT_ACTION_CLASSIFIER", "sys", {})

    assert all(c["api_base"] == shared_base for c in call_log), (
        "Without LLM_FALLBACK_API_BASE, all calls must use the primary api_base"
    )
    assert result.error is None
