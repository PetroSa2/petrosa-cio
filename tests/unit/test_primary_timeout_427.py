"""petrosa-cio#427: the primary LLM timeout follows the route's measured latency."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import cio.clients.llm_client as llm
from cio.clients.llm_client import LiteLLMClient, PrimaryTimeout

P = "PETROSA_PROMPT_ACTION_CLASSIFIER"


def _filled(latencies: list[float], prompt_id: str = P) -> PrimaryTimeout:
    timeout = PrimaryTimeout()
    for value in latencies:
        timeout.record(prompt_id, value)
    return timeout


def test_fewer_than_min_samples_use_the_labelled_constant():
    timeout = _filled([6.4] * (llm.PRIMARY_TIMEOUT_MIN_SAMPLES - 1))
    assert timeout.current(P) == llm.LLM_CALL_TIMEOUT_SECONDS


def test_with_samples_the_timeout_is_p95_times_margin():
    # 19 calls at 4 s and one at 8 s: p95 (19th of 20 sorted) is 4 s.
    timeout = _filled([4.0] * 19 + [7.0])
    assert timeout.current(P) == pytest.approx(4.0 * llm.PRIMARY_TIMEOUT_MARGIN)
    # 6.4 s median, p95 6.9 s (the 2026-11-25 route): above the fixed 7 s.
    timeout = _filled([6.4] * 18 + [6.9] * 2)
    assert timeout.current(P) == pytest.approx(6.9 * llm.PRIMARY_TIMEOUT_MARGIN)
    assert timeout.current(P) > llm.LLM_CALL_TIMEOUT_SECONDS


def test_a_latency_spike_cannot_raise_it_above_the_budget_maximum():
    upper = llm._primary_timeout_max_seconds()
    timeout = _filled([120.0] * 50)
    assert timeout.current(P) == pytest.approx(upper)
    # 60 s audit x 0.95 / 2 stages, minus 3 s backoff and a 7 s fallback, over 2 attempts.
    assert upper == pytest.approx((0.95 * 60 / 2 - 3 - 7) / 2)


def test_a_fast_route_is_bounded_below():
    assert _filled([0.2] * 50).current(P) == llm.PRIMARY_TIMEOUT_MIN_SECONDS


def test_the_window_forgets_old_samples():
    timeout = _filled([10.0] * 50 + [2.0] * llm.PRIMARY_TIMEOUT_WINDOW)
    assert timeout.current(P) == llm.PRIMARY_TIMEOUT_MIN_SECONDS


def test_each_prompt_has_its_own_window():
    timeout = _filled([2.0] * 50, "PETROSA_PROMPT_REGIME_ANALYST")
    for _ in range(50):
        timeout.record(P, 6.0)
    assert timeout.current("PETROSA_PROMPT_REGIME_ANALYST") == pytest.approx(3.0)
    assert timeout.current(P) == pytest.approx(7.5)
    assert (
        timeout.current("PETROSA_PROMPT_HEALTH_PROBE") == llm.LLM_CALL_TIMEOUT_SECONDS
    )


def test_budget_unavailable_falls_back_with_a_warning(caplog, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def failing_import(name, *args, **kwargs):
        if name == "cio.apps.nurse.enforcer":
            raise ImportError("probe")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", failing_import)
    assert llm._primary_timeout_max_seconds() == llm.LLM_CALL_TIMEOUT_SECONDS
    assert "LLM_PRIMARY_TIMEOUT budget unavailable" in caplog.text


def _fake_litellm(side_effect):
    fake = SimpleNamespace(
        acompletion=AsyncMock(side_effect=side_effect),
        get_supported_openai_params=MagicMock(return_value=[]),
    )
    exceptions = SimpleNamespace(
        RateLimitError=RuntimeError, ServiceUnavailableError=RuntimeError
    )
    return fake, patch.dict(
        sys.modules, {"litellm": fake, "litellm.exceptions": exceptions}
    )


def _ok():
    resp = MagicMock()
    resp.choices = [MagicMock(message=MagicMock(content='{"ok": true}'))]
    resp.model = "m"
    resp.usage.prompt_tokens = 1
    resp.usage.completion_tokens = 1
    resp.usage.prompt_tokens_details = None
    return resp


@pytest.mark.asyncio
async def test_primary_calls_use_the_derived_timeout_and_learn_only_from_successes(
    monkeypatch,
):
    monkeypatch.setenv("LLM_FALLBACK_MODEL", "nvidia_nim/other-model")
    for _ in range(llm.PRIMARY_TIMEOUT_MIN_SAMPLES):
        llm.PRIMARY_TIMEOUT.record("p", 7.0)
    expected = llm.PRIMARY_TIMEOUT.current("p")
    assert expected != llm.LLM_CALL_TIMEOUT_SECONDS

    fake, modules = _fake_litellm([TimeoutError(), _ok(), _ok()])
    with modules:
        out = await LiteLLMClient().complete("p", "s", {})
        assert out.error is None
        primary_call, fallback_call = fake.acompletion.await_args_list
        assert primary_call.kwargs["timeout"] == pytest.approx(expected)
        # The fallback keeps the fixed timeout (out of scope).
        assert fallback_call.kwargs["timeout"] == llm.LLM_CALL_TIMEOUT_SECONDS
        # A timed-out call is not a sample.
        assert len(llm.PRIMARY_TIMEOUT._samples["p"]) == llm.PRIMARY_TIMEOUT_MIN_SAMPLES

        out = await LiteLLMClient().complete("p", "s", {})
    assert out.error is None
    assert len(llm.PRIMARY_TIMEOUT._samples["p"]) == llm.PRIMARY_TIMEOUT_MIN_SAMPLES + 1
