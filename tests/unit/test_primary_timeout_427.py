"""petrosa-cio#427: the primary LLM timeout follows the route's measured latency."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import cio.clients.llm_client as llm
from cio.clients.llm_client import LiteLLMClient, PrimaryTimeout


def _filled(latencies: list[float]) -> PrimaryTimeout:
    timeout = PrimaryTimeout()
    for value in latencies:
        timeout.record(value)
    return timeout


def test_fewer_than_min_samples_use_the_labelled_constant():
    timeout = _filled([6.4] * (llm.PRIMARY_TIMEOUT_MIN_SAMPLES - 1))
    assert timeout.current() == llm.LLM_CALL_TIMEOUT_SECONDS


def test_with_samples_the_timeout_is_p95_times_margin():
    # 19 calls at 4 s and one at 8 s: p95 (19th of 20 sorted) is 4 s.
    timeout = _filled([4.0] * 19 + [8.0])
    assert timeout.current() == pytest.approx(4.0 * llm.PRIMARY_TIMEOUT_MARGIN)
    # 6.4 s median, p95 6.9 s (the 2026-11-25 route): above the fixed 7 s.
    timeout = _filled([6.4] * 18 + [6.9] * 2)
    assert timeout.current() == pytest.approx(6.9 * llm.PRIMARY_TIMEOUT_MARGIN)
    assert timeout.current() > llm.LLM_CALL_TIMEOUT_SECONDS


def test_a_latency_spike_cannot_raise_it_above_the_budget_maximum():
    upper = llm._primary_timeout_max_seconds()
    timeout = _filled([120.0] * 50)
    assert timeout.current() == pytest.approx(upper)
    # 60 s audit budget, 2 sequential stages, 0.9 share, 2 x 7 s + 0.5 s worst fallback.
    assert upper == pytest.approx(0.9 * 60 / 2 - (2 * 7 + 0.5))


def test_a_fast_route_is_bounded_below():
    assert _filled([0.2] * 50).current() == llm.PRIMARY_TIMEOUT_MIN_SECONDS


def test_frequent_timeouts_raise_it_step_by_step_until_they_stop():
    """Timed-out calls count at their timeout, so >5% timeouts push the p95 (and the next
    timeout) up by the margin each window until the route answers in time or the max binds."""
    timeout = _filled([5.0] * 50)
    first = timeout.current()
    for _ in range(10):  # 20% of the window times out at the current value
        timeout.record(first)
    second = timeout.current()
    assert second == pytest.approx(
        min(first * 1.25, llm._primary_timeout_max_seconds())
    )


def test_the_window_forgets_old_samples():
    timeout = _filled([10.0] * 50 + [2.0] * llm.PRIMARY_TIMEOUT_WINDOW)
    assert timeout.current() == llm.PRIMARY_TIMEOUT_MIN_SECONDS


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
async def test_primary_calls_use_and_feed_the_derived_timeout(monkeypatch):
    monkeypatch.setenv("LLM_FALLBACK_MODEL", "nvidia_nim/other-model")
    for _ in range(llm.PRIMARY_TIMEOUT_MIN_SAMPLES):
        llm.PRIMARY_TIMEOUT.record(8.0)
    expected = llm.PRIMARY_TIMEOUT.current()
    assert expected != llm.LLM_CALL_TIMEOUT_SECONDS

    fake, modules = _fake_litellm([TimeoutError(), _ok()])
    with modules:
        out = await LiteLLMClient().complete("p", "s", {})

    assert out.error is None
    primary_call, fallback_call = fake.acompletion.await_args_list
    assert primary_call.kwargs["timeout"] == pytest.approx(expected)
    # The fallback keeps the fixed timeout (out of scope).
    assert fallback_call.kwargs["timeout"] == llm.LLM_CALL_TIMEOUT_SECONDS
    # The timed-out primary call was recorded at its timeout.
    assert llm.PRIMARY_TIMEOUT._samples[-1] == pytest.approx(expected)
