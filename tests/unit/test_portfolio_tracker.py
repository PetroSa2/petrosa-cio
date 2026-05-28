"""Tests for the portfolio aggregate-leverage tracker (#138, FR61 / AC5.d).

Pure-function-level tests of `PortfolioTracker` + `ceiling_from_env`.
Orchestrator-wiring tests for the REJECT path live in
`tests/unit/test_orchestrator_portfolio_ceiling.py`.
"""

from __future__ import annotations

import math

import pytest

from cio.core.portfolio_tracker import (
    DEFAULT_PORTFOLIO_LEVERAGE_CEILING,
    PortfolioTracker,
    ceiling_from_env,
)

# ---------------------------------------------------------------------------
# AC5.c — env-var resolution


def test_ceiling_from_env_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("CIO_PORTFOLIO_LEVERAGE_CEILING", raising=False)
    assert ceiling_from_env() == DEFAULT_PORTFOLIO_LEVERAGE_CEILING


def test_ceiling_from_env_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CIO_PORTFOLIO_LEVERAGE_CEILING", "3.5")
    assert ceiling_from_env() == 3.5


def test_ceiling_from_env_falls_back_on_garbage(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("CIO_PORTFOLIO_LEVERAGE_CEILING", "not-a-float")
    assert ceiling_from_env() == DEFAULT_PORTFOLIO_LEVERAGE_CEILING


def test_ceiling_from_env_clamps_negative_to_zero(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("CIO_PORTFOLIO_LEVERAGE_CEILING", "-1.0")
    assert ceiling_from_env() == 0.0


# ---------------------------------------------------------------------------
# AC5.a — aggregate computation


@pytest.mark.asyncio
async def test_aggregate_is_zero_when_no_positions_tracked():
    tracker = PortfolioTracker()
    assert await tracker.compute_aggregate(equity=10_000) == 0.0


@pytest.mark.asyncio
async def test_aggregate_sums_size_times_leverage_over_equity():
    tracker = PortfolioTracker()
    # Two positions: 1000 × 3 + 2000 × 5 = 13_000. Equity 10_000.
    await tracker.record_admit(strategy_id="alpha", position_size_usd=1000, leverage=3)
    await tracker.record_admit(strategy_id="beta", position_size_usd=2000, leverage=5)
    aggregate = await tracker.compute_aggregate(equity=10_000)
    assert aggregate == pytest.approx(1.3)


@pytest.mark.asyncio
async def test_record_admit_replaces_prior_admission_for_strategy():
    tracker = PortfolioTracker()
    await tracker.record_admit(strategy_id="alpha", position_size_usd=1000, leverage=10)
    await tracker.record_admit(strategy_id="alpha", position_size_usd=500, leverage=2)
    assert tracker.tracked_strategy_count == 1
    aggregate = await tracker.compute_aggregate(equity=10_000)
    assert aggregate == pytest.approx(0.1)  # 500 * 2 / 10_000


@pytest.mark.asyncio
async def test_record_exit_drops_strategy():
    tracker = PortfolioTracker()
    await tracker.record_admit(strategy_id="alpha", position_size_usd=1000, leverage=5)
    await tracker.record_exit(strategy_id="alpha")
    assert tracker.tracked_strategy_count == 0
    assert await tracker.compute_aggregate(equity=10_000) == 0.0


@pytest.mark.asyncio
async def test_aggregate_returns_inf_when_equity_is_zero():
    tracker = PortfolioTracker()
    await tracker.record_admit(strategy_id="alpha", position_size_usd=1000, leverage=2)
    assert math.isinf(await tracker.compute_aggregate(equity=0.0))


# ---------------------------------------------------------------------------
# AC5.b — would_breach_ceiling


@pytest.mark.asyncio
async def test_would_breach_under_ceiling_does_not_breach():
    tracker = PortfolioTracker()
    # Empty tracker. New admission contributes 1000 * 3 / 10_000 = 0.3.
    # Ceiling 5.0 → projected 0.3 ≤ 5.0 → no breach.
    result = await tracker.would_breach_ceiling(
        new_position_size_usd=1000,
        new_leverage=3,
        equity=10_000,
        ceiling=5.0,
    )
    assert result.would_breach is False
    assert result.current_aggregate == 0.0
    assert result.projected_aggregate == pytest.approx(0.3)
    assert "OK" in result.reason


@pytest.mark.asyncio
async def test_would_breach_exactly_at_ceiling_does_not_breach():
    """Strict > semantics: equal to ceiling is OK, exceeding is breach."""
    tracker = PortfolioTracker()
    await tracker.record_admit(
        strategy_id="alpha", position_size_usd=10_000, leverage=4
    )  # current aggregate = 4.0
    # New contribution = 10_000 * 1 / 10_000 = 1.0 → projected 5.0 == ceiling.
    result = await tracker.would_breach_ceiling(
        new_position_size_usd=10_000,
        new_leverage=1,
        equity=10_000,
        ceiling=5.0,
    )
    assert result.would_breach is False
    assert result.projected_aggregate == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_would_breach_when_projected_exceeds_ceiling():
    tracker = PortfolioTracker()
    await tracker.record_admit(
        strategy_id="alpha", position_size_usd=10_000, leverage=4
    )  # aggregate = 4.0
    # New contribution = 20_000 * 1 / 10_000 = 2.0 → projected 6.0 > 5.0.
    result = await tracker.would_breach_ceiling(
        new_position_size_usd=20_000,
        new_leverage=1,
        equity=10_000,
        ceiling=5.0,
    )
    assert result.would_breach is True
    assert result.current_aggregate == pytest.approx(4.0)
    assert result.projected_aggregate == pytest.approx(6.0)
    assert result.ceiling == 5.0
    assert "BREACH" in result.reason


@pytest.mark.asyncio
async def test_would_breach_when_equity_is_zero_treats_as_infinite_aggregate():
    """Equity ≤ 0 is conservative: any new admission breaches."""
    tracker = PortfolioTracker()
    result = await tracker.would_breach_ceiling(
        new_position_size_usd=1.0,
        new_leverage=1,
        equity=0.0,
        ceiling=5.0,
    )
    assert result.would_breach is True
    assert math.isinf(result.projected_aggregate)


@pytest.mark.asyncio
async def test_would_breach_uses_env_var_when_ceiling_arg_omitted(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("CIO_PORTFOLIO_LEVERAGE_CEILING", "2.0")
    tracker = PortfolioTracker()
    # New contribution 1000 * 3 / 10_000 = 0.3 < 2.0 → OK at env ceiling.
    result = await tracker.would_breach_ceiling(
        new_position_size_usd=1000,
        new_leverage=3,
        equity=10_000,
    )
    assert result.would_breach is False
    assert result.ceiling == 2.0


@pytest.mark.asyncio
async def test_would_breach_with_admitted_then_replaced_uses_replaced_value():
    """A second record_admit replaces the first; aggregate reflects the new value."""
    tracker = PortfolioTracker()
    await tracker.record_admit(
        strategy_id="alpha", position_size_usd=100_000, leverage=10
    )
    await tracker.record_admit(strategy_id="alpha", position_size_usd=1000, leverage=2)
    result = await tracker.would_breach_ceiling(
        new_position_size_usd=0,
        new_leverage=0,
        equity=10_000,
        ceiling=5.0,
    )
    # Aggregate after replacement: 1000 * 2 / 10_000 = 0.2.
    assert result.current_aggregate == pytest.approx(0.2)
    assert result.would_breach is False
