"""Regime policy: low-confidence or stale regime means probe size; turbulent cost uplift (petrosa-cio#294)."""

from datetime import UTC, datetime, timedelta

import pytest

from cio.core.context_builder import ContextBuilder
from cio.core.engine import CodeEngine
from cio.core.net_ev import evaluate
from cio.core.regime_policy import (
    regime_availability,
    stale_after_seconds,
)
from cio.models import (
    ConfidenceLevel,
    ContextGap,
    MarketSignals,
    PnlTrend,
    PortfolioSummary,
    PriorStrength,
    RegimeAPIResponse,
    RegimeEnum,
    RegimeResult,
    RiskLimits,
    SlippageEstimate,
    StrategyDefaults,
    StrategyStats,
    TriggerContext,
    TriggerType,
    VolatilityLevel,
)

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
PAYLOAD = {
    "side": "BUY",
    "entry_price": 100.0,
    "stop_loss": 98.68,
    "take_profit": 101.98,
}


def _regime(
    confidence=ConfidenceLevel.HIGH,
    age=timedelta(minutes=5),
    kind=RegimeEnum.RANGING,
    dm="balanced_market",
):
    return RegimeResult(
        regime=kind,
        regime_confidence=confidence,
        volatility_level=VolatilityLevel.MEDIUM,
        primary_signal="t",
        thought_trace="t",
        data_manager_regime=dm,
        computed_at=None if age is None else datetime.now(UTC) - age,
    )


def _context(regime=None, slippage=None, wins=160, losses=40):
    return TriggerContext(
        correlation_id="cid",
        source_subject="test",
        trigger_type=TriggerType.TRADE_INTENT,
        trigger_payload=PAYLOAD,
        regime=regime or _regime(),
        volatility_level=VolatilityLevel.MEDIUM,
        market_signals=MarketSignals(
            signal_summary="t",
            current_price=100.0,
            volatility_percentile=0.5,
            trend_strength=0.0,
            price_action_character="t",
        ),
        strategy_id="s1",
        strategy_stats=StrategyStats(
            win_rate=wins / (wins + losses),
            wins=wins,
            losses=losses,
            recent_pnl_trend=PnlTrend.NEUTRAL,
        ),
        strategy_defaults=StrategyDefaults(
            stop_loss_pct=0.02, take_profit_pct=0.04, max_hold_hours=24
        ),
        global_drawdown_pct=0.0,
        open_orders_global=0,
        open_orders_symbol=0,
        available_capital_usd=20_000.0,
        portfolio=PortfolioSummary(
            gross_exposure=0.0, same_asset_pct=0.0, open_positions_count=0
        ),
        risk_limits=RiskLimits(
            max_drawdown_pct=0.1,
            max_orders_global=50,
            max_orders_per_symbol=5,
            max_position_size_usd=5000.0,
        ),
        slippage=slippage,
        prior_strength=PriorStrength(value=30.0, source="fallback"),
    )


# --- availability --------------------------------------------------------------------------------


def test_the_stale_limit_is_the_larger_of_three_intervals_and_an_hour(monkeypatch):
    monkeypatch.delenv("CIO_REGIME_ANALYZER_INTERVAL_SECONDS", raising=False)
    assert stale_after_seconds() == 3600.0  # 3 x 900 s = 45 min, below the hour
    monkeypatch.setenv("CIO_REGIME_ANALYZER_INTERVAL_SECONDS", "1800")
    assert stale_after_seconds() == 5400.0
    monkeypatch.setenv("CIO_REGIME_ANALYZER_INTERVAL_SECONDS", "nonsense")
    assert stale_after_seconds() == 3600.0


def test_a_confident_fresh_regime_is_available():
    state = regime_availability(_regime())
    assert state.available is True and state.reason is None
    assert state.age_known is True and state.age_seconds == pytest.approx(300, abs=5)


def test_a_low_confidence_regime_is_unavailable():
    state = regime_availability(_regime(confidence=ConfidenceLevel.LOW))
    assert (state.available, state.reason) == (False, "regime_low_confidence")


def test_a_stale_regime_is_unavailable_even_when_confident(monkeypatch):
    monkeypatch.delenv("CIO_REGIME_ANALYZER_INTERVAL_SECONDS", raising=False)
    state = regime_availability(_regime(age=timedelta(minutes=90)))
    assert (state.available, state.reason) == (False, "regime_stale")
    assert state.stale_after_seconds == 3600.0
    assert regime_availability(_regime(age=timedelta(minutes=59))).available is True


def test_an_unknown_computation_time_is_not_called_stale():
    state = regime_availability(_regime(age=None))
    assert (
        state.available is True
        and state.age_known is False
        and state.age_seconds is None
    )


def test_the_regime_carries_the_time_data_manager_computed_it():
    stamp = datetime(2026, 10, 7, 11, 30, tzinfo=UTC)
    response = RegimeAPIResponse.model_validate(
        {
            "pair": "BTCUSDT",
            "metric": "regime",
            "data": {
                "regime": "balanced_market",
                "volatility_level": "medium",
                "volume_level": "normal",
                "trend_direction": "neutral",
                "confidence": "0.85",
            },
            "metadata": {
                "timestamp": stamp.isoformat(),
                "collection": "analytics_BTCUSDT_regime",
            },
        }
    )
    result = RegimeResult.from_api_response(response)
    assert result.computed_at == stamp
    assert result.data_manager_regime == "balanced_market"


# --- probe size only -----------------------------------------------------------------------------


def test_a_confident_fresh_regime_sizes_from_the_posterior():
    result = CodeEngine.run(_context())
    assert result.sizing.binding in {"kelly", "max_position"}
    assert result.sizing.final_size_usd > result.sizing.probe_usd
    assert result.sizing.regime_reason is None


@pytest.mark.parametrize(
    ("regime", "reason"),
    [
        (_regime(confidence=ConfidenceLevel.LOW), "regime_low_confidence"),
        (_regime(age=timedelta(hours=3)), "regime_stale"),
    ],
)
def test_an_unavailable_regime_gives_probe_size_only_with_the_reason(regime, reason):
    result = CodeEngine.run(_context(regime))
    sizing = result.sizing
    assert sizing.binding == "regime_probe" and sizing.regime_reason == reason
    assert sizing.final_size_usd == sizing.probe_usd == result.kelly_position_usd
    assert (
        sizing.size_before_regime_usd > sizing.probe_usd
    )  # what the posterior would have sized


def test_a_low_confidence_regime_is_recorded_as_a_context_gap():
    gaps: list[ContextGap] = []
    ContextBuilder._note_regime_unavailable(
        _regime(confidence=ConfidenceLevel.LOW), gaps
    )
    assert len(gaps) == 1 and gaps[0].surface == "market"
    assert gaps[0].reason.startswith("regime_low_confidence")
    stale: list[ContextGap] = []
    ContextBuilder._note_regime_unavailable(_regime(age=timedelta(hours=3)), stale)
    assert stale[0].reason.startswith("regime_stale")
    fine: list[ContextGap] = []
    ContextBuilder._note_regime_unavailable(_regime(), fine)
    assert fine == []


# --- the hard blocks stay, on a confident regime only --------------------------------------------


@pytest.mark.parametrize("kind", [RegimeEnum.CHOPPY, RegimeEnum.CAPITULATION])
def test_a_confident_choppy_or_capitulation_regime_still_blocks(kind):
    result = CodeEngine.run(_context(_regime(kind=kind)))
    assert result.hard_blocked is True
    assert result.block_reason.startswith("regime_block")


@pytest.mark.parametrize("kind", [RegimeEnum.CHOPPY, RegimeEnum.CAPITULATION])
def test_a_low_confidence_one_does_not_block_it_goes_at_probe_size(kind):
    # data-manager reports `transitional` (mapped to CHOPPY) at a constant low confidence: unavailable,
    # not blocking
    result = CodeEngine.run(
        _context(_regime(kind=kind, confidence=ConfidenceLevel.LOW))
    )
    assert result.hard_blocked is False
    assert result.sizing.binding == "regime_probe"


# --- turbulent_illiquidity: a cost uplift, never a block ----------------------------------------


def _turbulent(slippage=None):
    return _context(
        _regime(kind=RegimeEnum.HIGH_VOLATILITY, dm="turbulent_illiquidity"), slippage
    )


def test_turbulent_illiquidity_is_not_blocked():
    assert CodeEngine.run(_turbulent()).hard_blocked is False


def test_unmeasured_turbulent_slippage_adds_the_documented_uplift_to_the_required_ev():
    calm = evaluate(_context(), 0.0132, 0.0198)
    turbulent = evaluate(_turbulent(), 0.0132, 0.0198)
    parts = {c.name: c for c in turbulent.costs}
    # twice the slippage of the fallback, and +0.05R on the required EV (a cost of 0.05 x the stop)
    calm_slippage = next(c for c in calm.costs if c.name == "slippage").value
    assert parts["slippage"].value == pytest.approx(2 * calm_slippage)
    assert parts["slippage"].source == "fallback"
    assert parts["turbulent_ev_uplift"].value == pytest.approx(0.05 * turbulent.s_eff)
    assert parts["turbulent_ev_uplift"].source == "fallback"
    assert "turbulent_ev_uplift" in turbulent.fallbacks
    assert turbulent.p_be > calm.p_be
    assert "turbulent_ev_uplift" not in {c.name for c in calm.costs}


def test_measured_turbulent_slippage_is_the_uplift_so_no_fixed_one_is_added():
    measured = SlippageEstimate(regime="turbulent_illiquidity", median_bp=6.0, count=40)
    gate = evaluate(_turbulent(measured), 0.0132, 0.0198)
    assert "turbulent_ev_uplift" not in {c.name for c in gate.costs}
    slippage = next(c for c in gate.costs if c.name == "slippage")
    assert slippage.source == "measured" and slippage.value == pytest.approx(
        2 * 6.0 / 10_000
    )


def test_the_pooled_median_also_replaces_the_fixed_uplift():
    pooled = SlippageEstimate(
        regime="turbulent_illiquidity",
        median_bp=None,
        count=3,
        pooled_median_bp=2.5,
        pooled_count=60,
    )
    gate = evaluate(_turbulent(pooled), 0.0132, 0.0198)
    assert (
        next(c for c in gate.costs if c.name == "slippage").source == "measured_pooled"
    )
    assert "turbulent_ev_uplift" not in {c.name for c in gate.costs}
