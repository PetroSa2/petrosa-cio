"""Unit tests for the PreDecisionContext bundle (P1.4-AC1 / FR55-FR58).

Scope (per [petrosa-cio#131](https://github.com/PetroSa2/petrosa-cio/issues/131)):

  * **AC1.a** — the typed `PreDecisionContext` + its four field models are
    importable, constructible, and round-trip through Pydantic's
    ``model_dump`` / ``model_validate`` without losing field shape.
  * **AC1.b** — ``ContextBuilder.assemble_pre_decision_context`` builds the
    bundle from the already-fetched subsystem components plus an
    ``EvaluatorSubscriber.snapshot``-shaped dict, and probes data-manager
    for the characterization reference.
  * **AC1.c** — when a ``TriggerContext`` carries a populated bundle,
    ``ActionClassifier._build_user_context`` exposes it under the
    ``pre_decision_context`` key so AC3 (separate child) can lock the
    prompt-contract shape.
  * **AC1.d (this file)** — happy-path coverage only; missing-context
    branches (no evaluator subscriber wired, characterization 404,
    data-manager unreachable) are owned by EPIC child 122.2 and are
    deliberately out of scope here. A few light degradation checks are
    included to prove the happy-path assertions do not coincidentally
    pass on an empty bundle.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from cio.core.context_builder import ContextBuilder
from cio.models import (
    CharacterizationRef,
    EvaluatorVerdict,
    MarketSignals,
    MarketState,
    PnlTrend,
    PortfolioState,
    PortfolioSummary,
    PreDecisionContext,
    RegimeResult,
    RiskLimits,
    StrategyDefaults,
    StrategyStats,
    TriggerContext,
)
from cio.models.enums import (
    ConfidenceLevel,
    RegimeEnum,
    TriggerType,
    VolatilityLevel,
)
from cio.personas.action_classifier import ActionClassifier

# ----------------------------------------------------------------------
# AC1.a — typed model + roundtrip
# ----------------------------------------------------------------------


def _build_bundle() -> PreDecisionContext:
    """Local fixture-as-helper used by multiple AC1.a tests."""
    return PreDecisionContext(
        market_state=MarketState(
            regime=RegimeEnum.RANGING,
            regime_confidence=ConfidenceLevel.MEDIUM,
            volatility_level=VolatilityLevel.MEDIUM,
            current_price=50000.0,
            primary_signal="unit-test-signal",
        ),
        portfolio_state=PortfolioState(
            gross_exposure=0.3,
            same_asset_pct=0.1,
            open_positions_count=2,
            global_drawdown_pct=0.05,
            available_capital_usd=10000.0,
            open_orders_global=3,
            open_orders_symbol=1,
        ),
        evaluator_verdicts={
            "ingest": EvaluatorVerdict(
                subsystem="ingest", verdict="healthy", reason="streaming ok"
            ),
            "strategies": EvaluatorVerdict(
                subsystem="strategies", verdict="healthy", reason=""
            ),
        },
        characterization=CharacterizationRef(
            strategy_id="strat-x",
            strategy_revision_id="srev_abc123abc123_def456def456",
        ),
    )


def test_pre_decision_context_has_four_typed_fields():
    """AC1.a — the bundle carries the exact field set the EPIC pins."""
    bundle = _build_bundle()
    assert isinstance(bundle.market_state, MarketState)
    assert isinstance(bundle.portfolio_state, PortfolioState)
    assert isinstance(bundle.evaluator_verdicts, dict)
    assert all(
        isinstance(v, EvaluatorVerdict) for v in bundle.evaluator_verdicts.values()
    )
    assert isinstance(bundle.characterization, CharacterizationRef)


def test_pre_decision_context_roundtrip_via_model_dump():
    """AC1.a — bundle survives ``model_dump`` → ``model_validate`` without drift."""
    original = _build_bundle()
    dumped = original.model_dump(mode="json")
    restored = PreDecisionContext.model_validate(dumped)
    assert restored.market_state.regime == RegimeEnum.RANGING
    assert restored.portfolio_state.global_drawdown_pct == 0.05
    assert set(restored.evaluator_verdicts.keys()) == {"ingest", "strategies"}
    assert restored.characterization is not None
    assert (
        restored.characterization.strategy_revision_id
        == "srev_abc123abc123_def456def456"
    )


def test_pre_decision_context_allows_none_characterization():
    """AC1.a — `characterization` is the only nullable bundle field
    (FR58: brand-new strategy with no admitted characterization yet)."""
    bundle = PreDecisionContext(
        market_state=_build_bundle().market_state,
        portfolio_state=_build_bundle().portfolio_state,
        evaluator_verdicts={},
        characterization=None,
    )
    assert bundle.characterization is None
    assert bundle.evaluator_verdicts == {}


def test_trigger_context_embeds_pre_decision_context_field():
    """AC1.a — `TriggerContext` carries the bundle via the new optional
    `pre_decision_context` field; the EPIC says "extends, do not collapse",
    so the existing flat fields stay intact alongside it."""
    bundle = _build_bundle()
    ctx = _build_trigger_context(pre_decision_context=bundle)
    assert ctx.pre_decision_context is bundle
    # Existing flat fields must still be present (do-not-collapse contract).
    assert ctx.regime is not None
    assert ctx.portfolio is not None


# ----------------------------------------------------------------------
# AC1.b — assembly path
# ----------------------------------------------------------------------


def _build_trigger_context(
    *, pre_decision_context: PreDecisionContext | None = None
) -> TriggerContext:
    return TriggerContext(
        correlation_id="cid-test",
        source_subject="test.subject",
        trigger_type=TriggerType.TRADE_INTENT,
        trigger_payload={"symbol": "BTCUSDT"},
        regime=RegimeResult(
            regime=RegimeEnum.RANGING,
            regime_confidence=ConfidenceLevel.MEDIUM,
            volatility_level=VolatilityLevel.MEDIUM,
            primary_signal="unit-test",
            thought_trace="unit-test",
        ),
        volatility_level=VolatilityLevel.MEDIUM,
        market_signals=MarketSignals(
            signal_summary="manual",
            current_price=50000.0,
            volatility_percentile=0.5,
            trend_strength=0.1,
            price_action_character="Neutral",
        ),
        strategy_id="strat-x",
        strategy_stats=StrategyStats(recent_pnl_trend=PnlTrend.NEUTRAL),
        strategy_defaults=StrategyDefaults(
            stop_loss_pct=0.02,
            take_profit_pct=0.04,
            leverage=1.0,
            max_hold_hours=24.0,
        ),
        global_drawdown_pct=0.05,
        open_orders_global=3,
        open_orders_symbol=1,
        available_capital_usd=10000.0,
        portfolio=PortfolioSummary(
            gross_exposure=0.3,
            same_asset_pct=0.1,
            open_positions_count=2,
        ),
        risk_limits=RiskLimits(
            max_drawdown_pct=0.2,
            max_orders_global=10,
            max_orders_per_symbol=3,
            max_position_size_usd=5000.0,
        ),
        pre_decision_context=pre_decision_context,
    )


@pytest.fixture
def fake_evaluator_subscriber():
    """Stand-in for ``EvaluatorSubscriber`` — exposes a ``snapshot()``
    in the exact shape ``cio/core/evaluator_subscriber.py`` produces."""
    sub = MagicMock()
    sub.snapshot.return_value = {
        "verdicts": [
            {
                "subsystem": "ingest",
                "verdict": "healthy",
                "reason": "ok",
                "observed_at": datetime(2026, 1, 1, 12, 0, 0).isoformat(),
                "override": None,
            },
            {
                "subsystem": "strategies",
                "verdict": "unknown",
                "reason": "",
                "observed_at": datetime(2026, 1, 1, 12, 0, 5).isoformat(),
                "override": None,
            },
        ],
        "paused": [],
        "pause_audit_log": [],
    }
    return sub


def _make_builder_with_mocked_http(
    *,
    characterization_status: int = 200,
    evaluator_subscriber: Any | None = None,
) -> ContextBuilder:
    builder = ContextBuilder(
        data_manager_url="http://data-manager",
        tradeengine_url="http://tradeengine",
        vector_client=None,
        evaluator_subscriber=evaluator_subscriber,
    )
    # Replace the live httpx.AsyncClient with an async mock so the
    # characterization probe is contained to the test process.
    mock_client = AsyncMock()
    response = MagicMock()
    response.status_code = characterization_status
    mock_client.get = AsyncMock(return_value=response)
    builder.client = mock_client
    return builder


@pytest.mark.asyncio
async def test_assemble_pre_decision_context_happy_path(
    fake_evaluator_subscriber,
):
    """AC1.b — every field is built from its named subsystem source."""
    builder = _make_builder_with_mocked_http(
        characterization_status=200,
        evaluator_subscriber=fake_evaluator_subscriber,
    )
    regime = RegimeResult(
        regime=RegimeEnum.TRENDING_BULL,
        regime_confidence=ConfidenceLevel.HIGH,
        volatility_level=VolatilityLevel.MEDIUM,
        primary_signal="ema-cross",
        thought_trace="up",
    )
    market_signals = MarketSignals(
        signal_summary="m",
        current_price=42000.0,
        volatility_percentile=0.5,
        trend_strength=0.6,
        price_action_character="trend",
    )
    portfolio = PortfolioSummary(
        gross_exposure=0.4,
        same_asset_pct=0.2,
        open_positions_count=5,
    )
    env_stats = {
        "global_drawdown_pct": 0.08,
        "available_capital_usd": 7500.0,
        "open_orders_global": 4,
        "open_orders_symbol": 2,
    }

    bundle = await builder.assemble_pre_decision_context(
        correlation_id="cid-assembly",
        regime=regime,
        market_signals=market_signals,
        portfolio=portfolio,
        env_stats=env_stats,
        strategy_id="strat-y",
        strategy_revision_id="srev_aaaaaaaaaaaa_bbbbbbbbbbbb",
    )

    # market_state
    assert bundle.market_state.regime == RegimeEnum.TRENDING_BULL
    assert bundle.market_state.current_price == 42000.0
    assert bundle.market_state.primary_signal == "ema-cross"

    # portfolio_state
    assert bundle.portfolio_state.gross_exposure == 0.4
    assert bundle.portfolio_state.global_drawdown_pct == 0.08
    assert bundle.portfolio_state.available_capital_usd == 7500.0
    assert bundle.portfolio_state.open_orders_symbol == 2

    # evaluator_verdicts — typed dict from the snapshot
    assert set(bundle.evaluator_verdicts.keys()) == {"ingest", "strategies"}
    assert bundle.evaluator_verdicts["ingest"].verdict == "healthy"
    assert bundle.evaluator_verdicts["strategies"].verdict == "unknown"

    # characterization ref — 200 from data-manager → record observed
    assert bundle.characterization is not None
    assert bundle.characterization.strategy_id == "strat-y"
    assert (
        bundle.characterization.strategy_revision_id == "srev_aaaaaaaaaaaa_bbbbbbbbbbbb"
    )

    await builder.close()


@pytest.mark.asyncio
async def test_assemble_pre_decision_context_without_revision_skips_fetch(
    fake_evaluator_subscriber,
):
    """AC1.b — legacy intents without a revision id leave ``characterization``
    at ``None`` and never hit data-manager for the ref probe."""
    builder = _make_builder_with_mocked_http(
        characterization_status=200,
        evaluator_subscriber=fake_evaluator_subscriber,
    )

    bundle = await builder.assemble_pre_decision_context(
        correlation_id="cid-no-rev",
        regime=RegimeResult(
            regime=RegimeEnum.RANGING,
            regime_confidence=ConfidenceLevel.MEDIUM,
            volatility_level=VolatilityLevel.MEDIUM,
            primary_signal="x",
            thought_trace="x",
        ),
        market_signals=MarketSignals(
            signal_summary="",
            current_price=1.0,
            volatility_percentile=0.5,
            trend_strength=0.0,
            price_action_character="n",
        ),
        portfolio=PortfolioSummary(
            gross_exposure=0.0, same_asset_pct=0.0, open_positions_count=0
        ),
        env_stats={
            "global_drawdown_pct": 0.0,
            "available_capital_usd": 0.0,
            "open_orders_global": 0,
            "open_orders_symbol": 0,
        },
        strategy_id="strat-legacy",
        strategy_revision_id=None,
    )

    assert bundle.characterization is None
    # The ref-probe must short-circuit when no revision id is supplied.
    builder.client.get.assert_not_awaited()

    await builder.close()


# ----------------------------------------------------------------------
# AC1.c — bundle reaches the arbitration prompt
# ----------------------------------------------------------------------


def test_action_classifier_includes_bundle_in_user_context():
    """AC1.c — ``_build_user_context`` exposes the bundle under
    ``pre_decision_context`` whenever it is present on the trigger."""
    bundle = _build_bundle()
    ctx = _build_trigger_context(pre_decision_context=bundle)

    # We stub the LLM client + system-prompt load — neither matters for
    # the user-context shape check.
    classifier = ActionClassifier.__new__(ActionClassifier)
    classifier.client = MagicMock()
    classifier.system_prompt = ""

    code_result = MagicMock(
        gross_ev=0.1,
        ev_unavailable=False,
        kelly_position_usd=100.0,
        hard_blocked=False,
        risk_warnings=[],
    )
    regime_result = ctx.regime
    strategy_result = MagicMock()
    strategy_result.health.value = "healthy"
    strategy_result.activation_recommendation.value = "run"
    strategy_result.regime_fit.value = "good"

    user_context = classifier._build_user_context(
        ctx, code_result, regime_result, strategy_result
    )

    assert "pre_decision_context" in user_context
    embedded = user_context["pre_decision_context"]
    assert embedded["market_state"]["regime"] == "ranging"
    assert "evaluator_verdicts" in embedded
    assert set(embedded["evaluator_verdicts"].keys()) == {"ingest", "strategies"}


def test_action_classifier_omits_bundle_when_absent():
    """AC1.c (negative) — when no bundle is attached, the user_context
    is unchanged: prompt-contract enforcement (AC3) needs to detect the
    bundle's *absence*, not see a ``None`` placeholder."""
    ctx = _build_trigger_context(pre_decision_context=None)
    classifier = ActionClassifier.__new__(ActionClassifier)
    classifier.client = MagicMock()
    classifier.system_prompt = ""

    code_result = MagicMock(
        gross_ev=0.1,
        ev_unavailable=False,
        kelly_position_usd=100.0,
        hard_blocked=False,
        risk_warnings=[],
    )
    strategy_result = MagicMock()
    strategy_result.health.value = "healthy"
    strategy_result.activation_recommendation.value = "run"
    strategy_result.regime_fit.value = "good"

    user_context = classifier._build_user_context(
        ctx, code_result, ctx.regime, strategy_result
    )
    assert "pre_decision_context" not in user_context


# ----------------------------------------------------------------------
# AC2 — missing-context handling (per-surface availability + gap log)
# ----------------------------------------------------------------------


def test_pre_decision_context_defaults_all_surfaces_available():
    """AC2.a — happy-path bundle has every flag at True and no gaps."""
    bundle = _build_bundle()
    assert bundle.market_state_available is True
    assert bundle.portfolio_state_available is True
    assert bundle.evaluator_verdicts_available is True
    assert bundle.characterization_available is True
    assert bundle.gaps == []


@pytest.mark.asyncio
async def test_assemble_flags_evaluators_unavailable_when_subscriber_missing(
    fake_evaluator_subscriber,  # noqa: ARG001 — fixture present for parity
):
    """AC2.a + AC2.b — no evaluator subscriber wired flips the flag AND
    records a structured gap ready for FR12 audit-trail persistence."""
    builder = _make_builder_with_mocked_http(
        characterization_status=200,
        evaluator_subscriber=None,  # explicit missing wiring
    )

    bundle = await builder.assemble_pre_decision_context(
        correlation_id="cid-ev-miss",
        regime=RegimeResult(
            regime=RegimeEnum.RANGING,
            regime_confidence=ConfidenceLevel.MEDIUM,
            volatility_level=VolatilityLevel.MEDIUM,
            primary_signal="ok",
            thought_trace="ok",
        ),
        market_signals=MarketSignals(
            signal_summary="m",
            current_price=1.0,
            volatility_percentile=0.5,
            trend_strength=0.0,
            price_action_character="n",
        ),
        portfolio=PortfolioSummary(
            gross_exposure=0.0,
            same_asset_pct=0.0,
            open_positions_count=0,
        ),
        env_stats={
            "global_drawdown_pct": 0.0,
            "available_capital_usd": 0.0,
            "open_orders_global": 0,
            "open_orders_symbol": 0,
        },
        strategy_id="strat-ev",
        strategy_revision_id=None,
    )

    assert bundle.evaluator_verdicts == {}
    assert bundle.evaluator_verdicts_available is False
    # AC2.a — other surfaces stay True; gap is per-surface, not blanket.
    assert bundle.market_state_available is True
    assert bundle.portfolio_state_available is True
    assert bundle.characterization_available is True
    assert any(
        g.surface == "evaluators" and "subscriber_not_wired" in g.reason
        for g in bundle.gaps
    )
    await builder.close()


@pytest.mark.asyncio
async def test_assemble_flags_characterization_unavailable_on_endpoint_500(
    fake_evaluator_subscriber,
):
    """AC2.a + AC2.b — data-manager 500 with a revision id flips the
    flag and records a gap with the HTTP status in the reason."""
    builder = _make_builder_with_mocked_http(
        characterization_status=500,
        evaluator_subscriber=fake_evaluator_subscriber,
    )

    bundle = await builder.assemble_pre_decision_context(
        correlation_id="cid-char-500",
        regime=RegimeResult(
            regime=RegimeEnum.RANGING,
            regime_confidence=ConfidenceLevel.MEDIUM,
            volatility_level=VolatilityLevel.MEDIUM,
            primary_signal="ok",
            thought_trace="ok",
        ),
        market_signals=MarketSignals(
            signal_summary="m",
            current_price=1.0,
            volatility_percentile=0.5,
            trend_strength=0.0,
            price_action_character="n",
        ),
        portfolio=PortfolioSummary(
            gross_exposure=0.0,
            same_asset_pct=0.0,
            open_positions_count=0,
        ),
        env_stats={
            "global_drawdown_pct": 0.0,
            "available_capital_usd": 0.0,
            "open_orders_global": 0,
            "open_orders_symbol": 0,
        },
        strategy_id="strat-char",
        strategy_revision_id="srev_aaaaaaaaaaaa_bbbbbbbbbbbb",
    )

    assert bundle.characterization is None
    assert bundle.characterization_available is False
    # legacy "no revision id" path is NOT a gap — make sure the gap
    # reason actually mentions the endpoint status.
    char_gaps = [g for g in bundle.gaps if g.surface == "characterization"]
    assert len(char_gaps) == 1
    assert "500" in char_gaps[0].reason
    await builder.close()


@pytest.mark.asyncio
async def test_assemble_no_gap_when_no_revision_id(
    fake_evaluator_subscriber,
):
    """AC2.a edge — legacy (no revision id) is the steady-state path:
    characterization=None must stay flagged 'available' so consumers do
    not mistake "intent is unrevisioned" for a missing surface."""
    builder = _make_builder_with_mocked_http(
        characterization_status=200,
        evaluator_subscriber=fake_evaluator_subscriber,
    )

    bundle = await builder.assemble_pre_decision_context(
        correlation_id="cid-legacy",
        regime=RegimeResult(
            regime=RegimeEnum.RANGING,
            regime_confidence=ConfidenceLevel.MEDIUM,
            volatility_level=VolatilityLevel.MEDIUM,
            primary_signal="ok",
            thought_trace="ok",
        ),
        market_signals=MarketSignals(
            signal_summary="m",
            current_price=1.0,
            volatility_percentile=0.5,
            trend_strength=0.0,
            price_action_character="n",
        ),
        portfolio=PortfolioSummary(
            gross_exposure=0.0,
            same_asset_pct=0.0,
            open_positions_count=0,
        ),
        env_stats={
            "global_drawdown_pct": 0.0,
            "available_capital_usd": 0.0,
            "open_orders_global": 0,
            "open_orders_symbol": 0,
        },
        strategy_id="strat-legacy",
        strategy_revision_id=None,
    )

    assert bundle.characterization is None
    assert bundle.characterization_available is True
    assert not any(g.surface == "characterization" for g in bundle.gaps)
    await builder.close()


@pytest.mark.asyncio
async def test_assemble_propagates_caller_availability_map(
    fake_evaluator_subscriber,
):
    """AC2.a — when build() detected market/portfolio fetch failures it
    passes an availability map; assemble() must surface those flags + the
    gaps it received without dropping them."""
    from cio.models import ContextGap

    caller_gaps = [
        ContextGap(surface="market", reason="fetch_error: simulated 500"),
        ContextGap(surface="portfolio", reason="fetch_error: timeout"),
    ]
    caller_avail = {
        "market": False,
        "portfolio": False,
        "evaluators": True,
        "characterization": True,
    }

    builder = _make_builder_with_mocked_http(
        characterization_status=200,
        evaluator_subscriber=fake_evaluator_subscriber,
    )

    bundle = await builder.assemble_pre_decision_context(
        correlation_id="cid-upstream-fail",
        regime=RegimeResult(
            regime=RegimeEnum.RANGING,
            regime_confidence=ConfidenceLevel.MEDIUM,
            volatility_level=VolatilityLevel.MEDIUM,
            primary_signal="error",
            thought_trace="upstream",
        ),
        market_signals=MarketSignals(
            signal_summary="m",
            current_price=1.0,
            volatility_percentile=0.5,
            trend_strength=0.0,
            price_action_character="n",
        ),
        portfolio=PortfolioSummary(
            gross_exposure=1.0,
            same_asset_pct=1.0,
            open_positions_count=999,
        ),
        env_stats={
            "global_drawdown_pct": 1.0,
            "available_capital_usd": 0.0,
            "open_orders_global": 999,
            "open_orders_symbol": 0,
        },
        strategy_id="strat-up",
        strategy_revision_id=None,
        gaps=caller_gaps,
        availability=caller_avail,
    )

    assert bundle.market_state_available is False
    assert bundle.portfolio_state_available is False
    assert bundle.evaluator_verdicts_available is True
    surfaces = {g.surface for g in bundle.gaps}
    assert "market" in surfaces
    assert "portfolio" in surfaces
    await builder.close()


@pytest.mark.asyncio
async def test_fetch_regime_records_gap_on_data_manager_empty():
    """AC2.b — the existing "200 OK with empty metadata" branch on the
    Data Manager regime endpoint is a measurable outage from the bundle's
    perspective; it should produce a `market` gap when collectors are
    passed in."""
    from cio.models import ContextGap

    builder = _make_builder_with_mocked_http(
        characterization_status=200,
        evaluator_subscriber=None,
    )
    # Replace the AsyncMock get with one that returns the
    # "No regime data" metadata signature.
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "pair": "BTCUSDT",
        "metric": "regime",
        "data": None,
        "metadata": {"message": "No regime data available"},
    }
    builder.client.get = AsyncMock(return_value=mock_response)

    gaps: list[ContextGap] = []
    availability = {"market": True}
    result = await builder._fetch_regime(
        "BTCUSDT",
        "cid-empty",
        gaps=gaps,
        availability=availability,
    )
    assert result.primary_signal == "data_manager_empty"
    assert availability["market"] is False
    assert any(g.surface == "market" for g in gaps)
    await builder.close()
