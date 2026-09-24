"""End-to-end coverage for #174: the leverage-arbitration decision computed
and audited by ``arbitrate_leverage`` must actually reach the dispatched
``Signal`` that petrosa-tradeengine consumes for order placement.

Before this fix:
- ``contracts/signal.py::Signal`` had no ``leverage`` field at all.
- ``OutputRouter.route`` only computed ``arbitrate_leverage(...)`` AFTER the
  legacy translator had already built the outbound payload (inside the
  ``decision_store`` block), so the value never reached the Signal even if
  the field had existed.
- ``TriggerContext`` never carried a real ``recommended_leverage`` — the
  arbiter always fell back to the operator-max-only path.

These tests assert the full chain: TriggerContext.recommended_leverage
(sourced from strategy config) -> Orchestrator/OutputRouter ->
arbitrate_leverage -> DecisionResult.decided_leverage ->
Signal.leverage (the dispatched NATS payload) -- i.e. the value CIO
"decided" is the value that would reach tradeengine's order-placement path
for a given signal.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

# `contracts/` lives at the repo root alongside `cio/` and is not part of the
# installed `cio*` package (see pyproject.toml packages.find), so it is not
# reliably importable under every pytest invocation mode. Make the import
# robust regardless of how the test runner was invoked.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from petrosa_contracts import Signal  # noqa: E402

from cio.core.leverage_arbiter import arbitrate_leverage  # noqa: E402
from cio.core.router import OutputRouter  # noqa: E402
from cio.models import (  # noqa: E402
    ActionType,
    ActivationRecommendation,
    ConfidenceLevel,
    DecisionResult,
    HealthStatus,
    MarketSignals,
    PortfolioSummary,
    RegimeEnum,
    RegimeFit,
    RegimeResult,
    RiskLimits,
    StrategyDefaults,
    StrategyStats,
    TriggerContext,
    TriggerType,
    VolatilityLevel,
)
from cio.output.translator import TradeEngineTranslator  # noqa: E402


def _make_context(**overrides) -> TriggerContext:
    regime = RegimeResult(
        regime=RegimeEnum.RANGING,
        regime_confidence=ConfidenceLevel.MEDIUM,
        volatility_level=VolatilityLevel.MEDIUM,
        primary_signal="test",
        thought_trace="test",
    )
    defaults = {
        "correlation_id": "test-corr",
        "source_subject": "cio.intent.trading.s1",
        "trigger_type": TriggerType.TRADE_INTENT,
        "trigger_payload": {
            "symbol": "BTCUSDT",
            "side": "long",
            "current_price": 50000.0,
        },
        "regime": regime,
        "volatility_level": VolatilityLevel.MEDIUM,
        "market_signals": MarketSignals(
            signal_summary="test",
            current_price=50000.0,
            volatility_percentile=0.5,
            trend_strength=0.5,
            price_action_character="Neutral",
        ),
        "strategy_id": "s1",
        "strategy_stats": StrategyStats(),
        "strategy_defaults": StrategyDefaults(
            stop_loss_pct=0.02,
            take_profit_pct=0.04,
            leverage=1.0,
            max_hold_hours=24.0,
        ),
        "global_drawdown_pct": 0.0,
        "open_orders_global": 0,
        "open_orders_symbol": 0,
        "available_capital_usd": 1000.0,
        "portfolio": PortfolioSummary(
            gross_exposure=0.0, same_asset_pct=0.0, open_positions_count=0
        ),
        "risk_limits": RiskLimits(
            max_single_position_pct=0.1,
            max_global_drawdown_pct=0.1,
            max_portfolio_exposure=0.5,
            max_open_orders=10,
            max_same_asset_concentration=0.25,
        ),
    }
    defaults.update(overrides)
    return TriggerContext(**defaults)


def _make_decision(
    position_usd: float = 100.0, leverage: float = 1.0
) -> DecisionResult:
    return DecisionResult(
        action=ActionType.EXECUTE,
        justification="test",
        thought_trace="test",
        computed_position_size_usd=position_usd,
        hard_blocked=False,
        ev_passes=True,
        cost_viable=True,
        regime_confidence=ConfidenceLevel.HIGH,
        regime_fit=RegimeFit.GOOD,
        strategy_health=HealthStatus.HEALTHY,
        activation_recommendation=ActivationRecommendation.RUN,
        leverage=leverage,
    )


class TestTranslatorLeverageField:
    def test_leverage_present_on_legacy_signal_when_decided(self):
        ctx = _make_context()
        decision = _make_decision()
        decision.decided_leverage = 7

        result = TradeEngineTranslator.to_legacy_signal(ctx, decision)

        assert result is not None
        assert result["leverage"] == 7
        # Round-trips through the actual Signal contract model tradeengine
        # validates against.
        signal = Signal(**result)
        assert signal.leverage == 7

    def test_leverage_none_when_not_decided(self):
        """Hand-built DecisionResult objects that bypass the router (e.g.
        constructed directly in a unit test) get `leverage=None` — the field
        is optional, never a silent contract violation."""
        ctx = _make_context()
        decision = _make_decision()

        result = TradeEngineTranslator.to_legacy_signal(ctx, decision)

        assert result is not None
        assert result["leverage"] is None
        assert Signal(**result).leverage is None


class TestContextBuilderRecommendedLeverage:
    def test_recommended_leverage_is_declared_field(self):
        """TriggerContext must declare `recommended_leverage` as a real
        field (not just accept it via getattr fallback) so the context
        builder can set it from real strategy config (#174 AC2)."""
        ctx = _make_context(recommended_leverage=5)
        assert ctx.recommended_leverage == 5

    def test_recommended_leverage_defaults_to_none(self):
        ctx = _make_context()
        assert ctx.recommended_leverage is None
        assert ctx.strategy_leverage_envelope is None


@pytest.mark.asyncio
class TestRouterEndToEndLeveragePropagation:
    """The core #174 regression test: what CIO decided is what dispatches."""

    async def test_decided_leverage_matches_arbiter_and_reaches_dispatched_signal(
        self, monkeypatch
    ):
        monkeypatch.setenv("CIO_DEFAULT_MAX_LEVERAGE", "10")
        monkeypatch.setenv("DRY_RUN", "false")

        mock_nats = AsyncMock()
        mock_vector = AsyncMock()
        router = OutputRouter(nats_client=mock_nats, vector_client=mock_vector)

        # Strategy prefers 5x, well within the operator ceiling of 10x —
        # arbiter should accept it as-is. `leverage=10.0` means CodeEngine
        # did not compute a tighter regime cap for this decision (#1125's
        # clamp is a no-op here — this test targets the arbiter, not the
        # regime cap).
        ctx = _make_context(recommended_leverage=5)
        decision = _make_decision(leverage=10.0)

        expected = arbitrate_leverage(recommended_leverage=5, operator_max=10)
        assert expected.decided_leverage == 5
        assert expected.branch == "accept"

        await router.route(ctx, decision)

        # 1. The DecisionResult mutated in place carries the arbiter output.
        assert decision.decided_leverage == 5

        # 2. The dispatched legacy signal (signals.trading.<strategy_id>)
        #    carries the SAME leverage value tradeengine would consume.
        legacy_calls = [
            call
            for call in mock_nats.publish.call_args_list
            if call.args[0] == "signals.trading.s1"
        ]
        assert len(legacy_calls) == 1
        dispatched_payload = json.loads(legacy_calls[0].args[1])
        assert dispatched_payload["leverage"] == 5
        signal_payload = {
            key: value
            for key, value in dispatched_payload.items()
            if key != "_otel_trace_context"
        }
        assert Signal(**signal_payload).leverage == 5

    async def test_recommended_leverage_over_ceiling_is_clamped_not_dropped(
        self, monkeypatch
    ):
        monkeypatch.setenv("CIO_DEFAULT_MAX_LEVERAGE", "10")
        monkeypatch.setenv("DRY_RUN", "false")

        mock_nats = AsyncMock()
        mock_vector = AsyncMock()
        router = OutputRouter(nats_client=mock_nats, vector_client=mock_vector)

        # Strategy asks for 25x — above the operator ceiling. Per AC3.b this
        # is an override (clamp), never a silent drop back to some
        # tradeengine-local hardcoded default. `leverage=10.0` — no tighter
        # regime cap for this decision (#1125's clamp is a no-op here).
        ctx = _make_context(recommended_leverage=25)
        decision = _make_decision(leverage=10.0)

        await router.route(ctx, decision)

        assert decision.decided_leverage == 10

        legacy_calls = [
            call
            for call in mock_nats.publish.call_args_list
            if call.args[0] == "signals.trading.s1"
        ]
        dispatched_payload = json.loads(legacy_calls[0].args[1])
        assert dispatched_payload["leverage"] == 10


@pytest.mark.asyncio
class TestRouterRegimeLeverageCap:
    """#1125: `decided_leverage` must never exceed the regime-capped
    leverage CodeEngine computed for this decision (`decision.leverage`,
    set from `code_result.leverage` = `min(strategy_defaults.leverage,
    REGIME_LEVERAGE_CAPS[regime])`). Before this fix, `arbitrate_leverage`
    had no visibility into the regime cap at all, so a strategy/operator
    default (typically 10x) could reach the dispatched Signal even when
    CodeEngine determined the current regime only supports a lower
    leverage — the symptom reported in #1125, reproducible in both bypass
    mode and normal LLM-reasoning mode since both share this code path.
    """

    async def test_decided_leverage_clamped_to_regime_cap(self, monkeypatch):
        monkeypatch.setenv("CIO_DEFAULT_MAX_LEVERAGE", "10")
        monkeypatch.setenv("DRY_RUN", "false")

        mock_nats = AsyncMock()
        mock_vector = AsyncMock()
        router = OutputRouter(nats_client=mock_nats, vector_client=mock_vector)

        # No strategy recommendation -> arbiter falls back to operator_max
        # (10x). But CodeEngine computed a regime cap of 3x for this
        # decision (e.g. a choppy-regime REGIME_LEVERAGE_CAPS entry).
        ctx = _make_context()
        decision = _make_decision(leverage=3.0)

        await router.route(ctx, decision)

        assert decision.decided_leverage == 3

        legacy_calls = [
            call
            for call in mock_nats.publish.call_args_list
            if call.args[0] == "signals.trading.s1"
        ]
        assert len(legacy_calls) == 1
        dispatched_payload = json.loads(legacy_calls[0].args[1])
        assert dispatched_payload["leverage"] == 3
        dispatched_payload.pop("_otel_trace_context", None)
        assert Signal(**dispatched_payload).leverage == 3

    async def test_decided_leverage_unclamped_when_within_regime_cap(self, monkeypatch):
        monkeypatch.setenv("CIO_DEFAULT_MAX_LEVERAGE", "10")
        monkeypatch.setenv("DRY_RUN", "false")

        mock_nats = AsyncMock()
        mock_vector = AsyncMock()
        router = OutputRouter(nats_client=mock_nats, vector_client=mock_vector)

        # Strategy prefers 5x, regime cap is a generous 20x -> the strategy
        # preference (not the regime cap) governs, per arbitrate_leverage's
        # existing accept-branch semantics.
        ctx = _make_context(recommended_leverage=5)
        decision = _make_decision(leverage=20.0)

        await router.route(ctx, decision)

        assert decision.decided_leverage == 5

    async def test_bypass_mode_decision_respects_regime_cap_end_to_end(
        self, monkeypatch
    ):
        """Simulates the exact #1125 bypass-mode path: ActionClassifier's
        bypass branch calls DecisionAssembler.assemble(), which sets
        `decision.leverage = code_result.leverage` (the regime cap). The
        router must clamp `decided_leverage` to that cap before dispatch —
        the trade engine must not fall back to the 10x default.
        """
        from cio.core.assembler import DecisionAssembler
        from cio.models import ActionType as _ActionType
        from cio.models import CodeEngineResult

        monkeypatch.setenv("CIO_DEFAULT_MAX_LEVERAGE", "10")
        monkeypatch.setenv("DRY_RUN", "false")

        ctx = _make_context()
        code_result = CodeEngineResult(
            hard_blocked=False,
            block_context_fallback=False,
            kelly_position_usd=100.0,
            recommended_sl_pct=0.02,
            recommended_tp_pct=0.04,
            leverage=2.0,
            gross_ev=0.01,
            ev_unavailable=False,
        )
        regime = ctx.regime
        from cio.models import (
            ActivationRecommendation as _ActivationRecommendation,
        )
        from cio.models import HealthStatus as _HealthStatus
        from cio.models import RegimeFit as _RegimeFit
        from cio.models import StrategyResult as _StrategyResult

        strategy_result = _StrategyResult(
            health=_HealthStatus.HEALTHY,
            activation_recommendation=_ActivationRecommendation.RUN,
            regime_fit=_RegimeFit.GOOD,
            thought_trace="DETERMINISTIC_BYPASS",
        )

        bypass_decision = DecisionAssembler.assemble(
            context=ctx,
            code_result=code_result,
            regime_result=regime,
            strategy_result=strategy_result,
            llm_action=_ActionType.EXECUTE,
            llm_justification="Deterministic bypass: Executing based on Code "
            "Engine approval (NURSE_USE_LLM_REASONING=false).",
        )
        assert bypass_decision.leverage == 2.0
        assert bypass_decision.decided_leverage is None  # not yet routed

        mock_nats = AsyncMock()
        mock_vector = AsyncMock()
        router = OutputRouter(nats_client=mock_nats, vector_client=mock_vector)
        await router.route(ctx, bypass_decision)

        assert bypass_decision.decided_leverage == 2

        legacy_calls = [
            call
            for call in mock_nats.publish.call_args_list
            if call.args[0] == "signals.trading.s1"
        ]
        dispatched_payload = json.loads(legacy_calls[0].args[1])
        assert dispatched_payload["leverage"] == 2
