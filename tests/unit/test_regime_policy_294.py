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
from cio.models.regime import _map_confidence, regime_min_confidence

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


def test_an_unknown_computation_time_is_unavailable_not_fresh():
    # data-manager's regime without a timestamp: freshness cannot be shown, so staleness cannot be missed
    state = regime_availability(_regime(age=None))
    assert (state.available, state.reason) == (False, "regime_age_unknown")
    assert state.age_known is False and state.age_seconds is None


def test_a_synthetic_regime_that_never_came_from_data_manager_is_not_judged_by_age():
    synthetic = _regime(age=None, dm=None)
    synthetic.primary_signal = "DETERMINISTIC_BYPASS"
    assert regime_availability(synthetic).available is True


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


# --- what "low confidence" means is an explicit, labelled input ----------------------------------


def _api(confidence, regime="transitional"):
    return RegimeAPIResponse.model_validate(
        {
            "pair": "BTCUSDT",
            "metric": "regime",
            "data": {
                "regime": regime,
                "volatility_level": "medium",
                "volume_level": "normal",
                "trend_direction": "neutral",
                "confidence": confidence,
            },
            "metadata": {
                "timestamp": datetime.now(UTC).isoformat(),
                "collection": "analytics_BTCUSDT_regime",
            },
        }
    )


def test_the_minimum_confidence_is_a_labelled_fallback_of_0_70(monkeypatch):
    monkeypatch.delenv("CIO_REGIME_MIN_CONFIDENCE", raising=False)
    assert regime_min_confidence() == (0.70, "fallback")
    assert _map_confidence(0.69) == ConfidenceLevel.LOW
    assert _map_confidence(0.70) == ConfidenceLevel.MEDIUM
    assert _map_confidence(0.80) == ConfidenceLevel.HIGH


def test_the_minimum_confidence_can_be_set_and_moves_the_low_cut(monkeypatch):
    monkeypatch.setenv("CIO_REGIME_MIN_CONFIDENCE", "0.75")
    assert regime_min_confidence() == (0.75, "env")
    assert _map_confidence(0.72) == ConfidenceLevel.LOW  # was MEDIUM at 0.70
    assert _map_confidence(0.75) == ConfidenceLevel.MEDIUM
    monkeypatch.setenv("CIO_REGIME_MIN_CONFIDENCE", "0.5")
    assert _map_confidence(0.55) == ConfidenceLevel.MEDIUM  # was LOW at 0.70
    assert _map_confidence(0.85) == ConfidenceLevel.HIGH


def test_a_minimum_above_the_high_cut_still_orders_the_levels(monkeypatch):
    monkeypatch.setenv("CIO_REGIME_MIN_CONFIDENCE", "0.9")
    assert _map_confidence(0.85) == ConfidenceLevel.LOW
    assert _map_confidence(0.9) == ConfidenceLevel.HIGH


@pytest.mark.parametrize("bad", ["", "abc", "0", "-0.2", "1.5", "nan"])
def test_an_unreadable_or_out_of_range_minimum_is_the_fallback(monkeypatch, bad):
    monkeypatch.setenv("CIO_REGIME_MIN_CONFIDENCE", bad)
    assert regime_min_confidence() == (0.70, "fallback")


def test_the_regime_keeps_the_numeric_confidence_it_was_given():
    assert RegimeResult.from_api_response(_api("0.74")).confidence_value == 0.74


def test_availability_reports_the_confidence_seen_and_the_minimum_applied(monkeypatch):
    monkeypatch.delenv("CIO_REGIME_MIN_CONFIDENCE", raising=False)
    low = RegimeResult.from_api_response(_api("0.6"))
    state = regime_availability(low)
    assert state.available is False and state.reason == "regime_low_confidence"
    assert (state.confidence_value, state.min_confidence) == (0.6, 0.70)
    assert state.min_confidence_source == "fallback"
    monkeypatch.setenv("CIO_REGIME_MIN_CONFIDENCE", "0.55")
    state = regime_availability(RegimeResult.from_api_response(_api("0.6")))
    assert state.available is True and state.min_confidence_source == "env"


def test_the_sizing_record_carries_the_minimum_with_the_regime_reason(monkeypatch):
    monkeypatch.delenv("CIO_REGIME_MIN_CONFIDENCE", raising=False)
    regime = RegimeResult.from_api_response(_api("0.6"))
    regime.computed_at = datetime.now(UTC) - timedelta(minutes=5)
    sizing = CodeEngine.run(_context(regime)).sizing
    assert sizing.binding == "regime_probe"
    assert sizing.regime_reason == "regime_low_confidence"
    assert sizing.regime_confidence_value == 0.6
    assert sizing.regime_min_confidence == 0.70
    assert sizing.regime_min_confidence_source == "fallback"


def test_a_confident_regime_leaves_the_sizing_record_without_a_regime_minimum():
    sizing = CodeEngine.run(_context()).sizing
    assert sizing.regime_reason is None
    assert sizing.regime_min_confidence is None


def test_the_context_gap_names_the_minimum_and_its_source(monkeypatch):
    monkeypatch.setenv("CIO_REGIME_MIN_CONFIDENCE", "0.8")
    gaps: list[ContextGap] = []
    ContextBuilder._note_regime_unavailable(
        RegimeResult.from_api_response(_api("0.75")), gaps
    )
    assert "min_confidence=0.8(env)" in gaps[0].reason
    assert "value=0.75" in gaps[0].reason


# --- petrosa-cio#326: staleness is enforced and degrades gracefully -------------------------------
def _missing(signal="error"):
    """What the context builder returns when the regime fetch fails or data-manager has none."""
    return RegimeResult(
        regime=RegimeEnum.CHOPPY,
        regime_confidence=ConfidenceLevel.LOW,
        volatility_level=VolatilityLevel.MEDIUM,
        primary_signal=signal,
        thought_trace="t",
    )


@pytest.mark.parametrize("kind", [RegimeEnum.CHOPPY, RegimeEnum.CAPITULATION])
def test_a_stale_confident_choppy_or_capitulation_regime_does_not_block(kind):
    # the hard blocks act only on a fresh, confident regime: stale data goes at probe size, never blocked
    result = CodeEngine.run(_context(_regime(kind=kind, age=timedelta(hours=5))))
    assert result.hard_blocked is False
    assert result.sizing.binding == "regime_probe"
    assert result.sizing.regime_reason == "regime_stale"
    assert result.sizing.final_size_usd == result.sizing.probe_usd


def test_a_stale_regime_is_probe_size_not_blocked_and_keeps_what_the_posterior_gave():
    fresh = CodeEngine.run(_context(_regime()))
    stale = CodeEngine.run(_context(_regime(age=timedelta(hours=5))))
    assert stale.hard_blocked is False
    assert stale.sizing.binding == "regime_probe"
    assert stale.sizing.size_before_regime_usd == fresh.sizing.final_size_usd


def test_a_fresh_low_confidence_regime_is_probe_size():
    result = CodeEngine.run(
        _context(_regime(confidence=ConfidenceLevel.LOW, age=timedelta(minutes=2)))
    )
    assert result.hard_blocked is False
    assert (result.sizing.binding, result.sizing.regime_reason) == (
        "regime_probe",
        "regime_low_confidence",
    )


def test_a_fresh_confident_choppy_regime_keeps_the_existing_block():
    result = CodeEngine.run(
        _context(_regime(kind=RegimeEnum.CHOPPY, age=timedelta(minutes=2)))
    )
    assert result.hard_blocked is True
    assert result.block_reason.startswith("regime_block: CHOPPY")


@pytest.mark.parametrize("signal", ["error", "timeout", "data_manager_empty"])
def test_a_missing_regime_is_neutral_probe_size_never_a_block_or_a_crash(signal):
    state = regime_availability(_missing(signal))
    assert (state.available, state.reason) == (False, "regime_missing")
    result = CodeEngine.run(_context(_missing(signal)))
    assert result.hard_blocked is False
    assert (result.sizing.binding, result.sizing.regime_reason) == (
        "regime_probe",
        "regime_missing",
    )


def test_data_manager_having_no_regime_for_the_pair_is_a_missing_regime():
    response = RegimeAPIResponse.model_validate(
        {
            "pair": "SOLUSDT",
            "metric": "regime",
            "data": None,
            "metadata": {
                "timestamp": "2026-10-09T10:33:41.358688+00:00",
                "collection": "analytics_SOLUSDT_regime",
            },
        }
    )
    regime = RegimeResult.from_api_response(response)
    assert regime_availability(regime).reason == "regime_missing"
    assert CodeEngine.run(_context(regime)).hard_blocked is False


def test_a_naive_data_manager_timestamp_is_read_as_utc():
    # data-manager's metadata.timestamp is stored without a zone ("2026-10-09T10:30:43.094000"), UTC
    stamp = (datetime.now(UTC) - timedelta(hours=5)).replace(tzinfo=None).isoformat()
    response = RegimeAPIResponse.model_validate(
        {
            "pair": "BTCUSDT",
            "metric": "regime",
            "data": {
                "regime": "balanced_market",
                "volatility_level": "medium",
                "volume_level": "medium",
                "trend_direction": "neutral",
                "confidence": "0.7",
            },
            "metadata": {"timestamp": stamp, "collection": "c"},
        }
    )
    state = regime_availability(RegimeResult.from_api_response(response))
    assert (state.available, state.reason) == (False, "regime_stale")
    assert state.age_seconds == pytest.approx(5 * 3600, abs=10)


def test_the_stale_limit_is_derived_from_the_interval_and_labelled(monkeypatch):
    monkeypatch.delenv("CIO_REGIME_ANALYZER_INTERVAL_SECONDS", raising=False)
    assert regime_availability(_regime()).stale_after_source == "fallback"
    monkeypatch.setenv("CIO_REGIME_ANALYZER_INTERVAL_SECONDS", "1800")
    state = regime_availability(_regime(age=timedelta(minutes=80)))
    assert (state.stale_after_seconds, state.stale_after_source) == (5400.0, "env")
    assert state.available is True  # 80 min < 3 x 30 min
    gaps: list[ContextGap] = []
    ContextBuilder._note_regime_unavailable(_regime(age=timedelta(hours=3)), gaps)
    assert "stale_after_s=5400(env)" in gaps[0].reason


def test_regime_freshness_is_monitored(monkeypatch):
    from cio.core import engine as engine_module

    ages, unavailable = [], []
    monkeypatch.setattr(engine_module.REGIME_AGE, "record", ages.append)
    monkeypatch.setattr(
        engine_module.REGIME_UNAVAILABLE,
        "add",
        lambda value, attributes: unavailable.append(attributes["reason"]),
    )
    CodeEngine.run(_context(_regime(age=timedelta(hours=5))))
    CodeEngine.run(_context(_regime()))
    CodeEngine.run(_context(_missing()))
    assert len(ages) == 2 and ages[0] == pytest.approx(5 * 3600, abs=10)
    assert unavailable == ["regime_stale", "regime_missing"]


# --- review of #309: the regime override is a cap, never a floor; unknown age; clock skew -------------
def _sized(context, factor=1.0, state=None):
    from cio.core.sizing import size_order

    gate = CodeEngine.run(context).net_ev_gate
    state = state or regime_availability(context.regime)
    return size_order(context, gate, factor, state.reason, state)


def test_a_size_above_the_probe_is_capped_to_it():
    sizing = _sized(_context(_regime(confidence=ConfidenceLevel.LOW)))
    assert sizing.final_size_usd == sizing.probe_usd
    assert sizing.binding == "regime_probe"
    assert sizing.size_before_regime_usd > sizing.probe_usd


def test_a_size_below_the_probe_is_never_raised_by_the_regime(monkeypatch):
    from cio.core import sizing as sizing_module

    context = _context(_regime(confidence=ConfidenceLevel.LOW))
    real = sizing_module._size_order

    def smaller(ctx, gate):
        record = real(ctx, gate)
        record.final_size_usd = record.probe_usd / 4  # e.g. a smaller cap upstream
        return record

    monkeypatch.setattr(sizing_module, "_size_order", smaller)
    sizing = _sized(context)
    assert sizing.final_size_usd == sizing.probe_usd / 4  # cap, not floor
    assert sizing.binding != "regime_probe"  # the cap did not bind
    assert sizing.regime_reason == "regime_low_confidence"  # still recorded
    assert sizing.size_before_regime_usd is None


def test_a_zero_size_stays_zero_under_the_regime_cap(monkeypatch):
    from cio.core import sizing as sizing_module

    real = sizing_module._size_order

    def zero(ctx, gate):
        record = real(ctx, gate)
        record.final_size_usd = 0.0
        return record

    monkeypatch.setattr(sizing_module, "_size_order", zero)
    sizing = _sized(_context(_regime(age=timedelta(hours=5))))
    assert sizing.final_size_usd == 0.0 and sizing.binding != "regime_probe"


def test_the_drawdown_reduce_factor_still_applies_under_an_unavailable_regime():
    context = _context(_regime(confidence=ConfidenceLevel.LOW))
    sizing = _sized(context, factor=0.5)
    assert sizing.drawdown_factor == 0.5 and sizing.size_before_drawdown_usd is not None
    assert (
        sizing.final_size_usd == sizing.probe_usd
    )  # the floor of the reduce step, also the cap


def test_the_regime_cap_is_the_last_step_of_sizing():
    import inspect

    from cio.core import sizing as sizing_module

    source = inspect.getsource(sizing_module.size_order)
    assert source.index("drawdown_factor < 1.0") < source.index(
        "min(record.final_size_usd"
    )
    assert "return record" in source.split("min(record.final_size_usd")[1]
    assert source.count("return record") == 1  # no early exit that skips a later cap


def test_an_unknown_age_is_probe_size_never_a_block_and_is_counted(monkeypatch):
    from cio.core import engine as engine_module

    counted = []
    monkeypatch.setattr(
        engine_module.REGIME_UNAVAILABLE,
        "add",
        lambda value, attributes: counted.append(attributes["reason"]),
    )
    result = CodeEngine.run(_context(_regime(kind=RegimeEnum.CHOPPY, age=None)))
    assert (
        result.hard_blocked is False
    )  # a confident CHOPPY of unknown age must not block
    assert (result.sizing.binding, result.sizing.regime_reason) == (
        "regime_probe",
        "regime_age_unknown",
    )
    assert counted == ["regime_age_unknown"]


def test_the_unavailable_count_does_not_depend_on_the_levels_being_known(monkeypatch):
    from cio.core import engine as engine_module

    counted = []
    monkeypatch.setattr(
        engine_module.REGIME_UNAVAILABLE,
        "add",
        lambda value, attributes: counted.append(attributes["reason"]),
    )
    context = _context(_regime(age=timedelta(hours=5)))
    context.trigger_payload = {
        "side": "BUY",
        "entry_price": 100.0,
    }  # no stop, no target
    CodeEngine.run(context)
    assert counted == ["regime_stale"]


def test_a_future_timestamp_warns_once_per_interval(caplog, monkeypatch):
    from cio.core import regime_policy

    monkeypatch.setattr(regime_policy, "_last_skew_warning", None)
    future = _regime(age=-timedelta(minutes=20))
    with caplog.at_level("WARNING", logger="cio.core.regime_policy"):
        first = regime_availability(future)
        regime_availability(future)
    warnings = [r for r in caplog.records if "REGIME_TIMESTAMP_IN_FUTURE" in r.message]
    assert len(warnings) == 1
    assert first.age_seconds == 0.0  # clamped, still fresh
    # a small skew is not worth a warning
    caplog.clear()
    monkeypatch.setattr(regime_policy, "_last_skew_warning", None)
    with caplog.at_level("WARNING", logger="cio.core.regime_policy"):
        regime_availability(_regime(age=-timedelta(minutes=2)))
    assert not caplog.records


def test_the_warning_returns_after_an_interval(caplog, monkeypatch):
    from cio.core import regime_policy

    monkeypatch.setattr(regime_policy, "_last_skew_warning", None)
    ticks = iter([1000.0, 1000.0 + 901.0])
    monkeypatch.setattr(regime_policy.time, "monotonic", lambda: next(ticks))
    monkeypatch.delenv("CIO_REGIME_ANALYZER_INTERVAL_SECONDS", raising=False)
    future = _regime(age=-timedelta(minutes=20))
    with caplog.at_level("WARNING", logger="cio.core.regime_policy"):
        regime_availability(future)
        regime_availability(future)
    assert len([r for r in caplog.records if "IN_FUTURE" in r.message]) == 2
