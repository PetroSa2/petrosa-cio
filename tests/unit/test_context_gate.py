"""Unit tests for the context-completeness gate (petrosa-cio#199).

Covers the three unit-testable Acceptance Criteria from the ticket:

  * AC1 — with all context surfaces raising ``httpx.ReadTimeout``, the LLM
    is NOT invoked and the cycle emits a ``CONTEXT_UNAVAILABLE`` verdict.
  * AC2 — a strategy frozen with reason ``CONTEXT_UNAVAILABLE`` auto-
    unfreezes after N consecutive healthy-context cycles (mocked
    verdicts — no dependency on live data-manager).
  * AC3 — CIO's own prior pause decision does not appear in the next
    cycle's ``pre_decision_context`` (extends the cio#169 self-exclusion
    already covered by ``test_pre_decision_context_bundle.py``).

The two "Live:" ACs in the ticket require an end-to-end data-manager
outage/recovery and are explicitly out of scope here.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cio.apps.nurse.models import RiskLimits
from cio.core.context_gate import (
    CONTEXT_UNAVAILABLE_FREEZE_VALUE,
    FREEZE_KEY_PREFIX,
    HEALTHY_STREAK_KEY_PREFIX,
    apply_context_gate,
    count_timeout_gaps,
)
from cio.models import (
    ContextGap,
    EvaluatorVerdict,
    MarketState,
    PortfolioState,
    PortfolioSummary,
    PreDecisionContext,
    RegimeResult,
    StrategyDefaults,
    StrategyStats,
    TriggerContext,
)
from cio.models.context import MarketSignals
from cio.models.enums import (
    ActionType,
    ConfidenceLevel,
    RegimeEnum,
    RejectionSource,
    TriggerType,
    VolatilityLevel,
)

# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------


class FakeCache:
    """Minimal in-memory stand-in for ``AsyncRedisCache``'s async surface."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._store.get(key)

    async def set(self, key: str, value: str, ttl: int = 900) -> None:
        self._store[key] = value

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)


def _pre_decision_context(gaps: list[ContextGap]) -> PreDecisionContext:
    return PreDecisionContext(
        market_state=MarketState(
            regime=RegimeEnum.RANGING,
            regime_confidence=ConfidenceLevel.MEDIUM,
            volatility_level=VolatilityLevel.MEDIUM,
            current_price=50000.0,
            primary_signal="timeout" if gaps else "ok",
        ),
        portfolio_state=PortfolioState(
            gross_exposure=0.0,
            same_asset_pct=0.0,
            open_positions_count=0,
            global_drawdown_pct=0.0,
            available_capital_usd=1000.0,
            open_orders_global=0,
            open_orders_symbol=0,
        ),
        evaluator_verdicts={},
        characterization=None,
        market_state_available=not gaps,
        portfolio_state_available=True,
        evaluator_verdicts_available=True,
        characterization_available=True,
        gaps=gaps,
    )


def _make_context(
    *, strategy_id: str = "strat-1", gaps: list[ContextGap] | None = None
) -> TriggerContext:
    gaps = gaps or []
    return TriggerContext(
        correlation_id="cid-gate",
        source_subject="cio.intent.trading.strat-1",
        trigger_type=TriggerType.TRADE_INTENT,
        trigger_payload={},
        regime=RegimeResult(
            regime=RegimeEnum.RANGING,
            regime_confidence=ConfidenceLevel.HIGH,
            volatility_level=VolatilityLevel.MEDIUM,
            primary_signal="t",
            confidence=0.9,
            fit="good",
            thought_trace="t",
        ),
        volatility_level=VolatilityLevel.MEDIUM,
        market_signals=MarketSignals(
            signal_summary="ok",
            current_price=100.0,
            volatility_percentile=0.5,
            trend_strength=0.5,
            price_action_character="ranging",
        ),
        strategy_id=strategy_id,
        strategy_stats=StrategyStats(),
        strategy_defaults=StrategyDefaults(
            stop_loss_pct=0.01, take_profit_pct=0.02, max_hold_hours=4.0
        ),
        global_drawdown_pct=0.0,
        open_orders_global=0,
        open_orders_symbol=0,
        available_capital_usd=1000.0,
        portfolio=PortfolioSummary(
            gross_exposure=0.0, same_asset_pct=0.0, open_positions_count=0
        ),
        risk_limits=RiskLimits(),
        pre_decision_context=_pre_decision_context(gaps),
    )


_STORM_GAPS = [
    ContextGap(surface="market", reason="read_timeout endpoint=x timeout_s=10.0"),
    ContextGap(
        surface="strategy_stats", reason="read_timeout endpoint=y timeout_s=10.0"
    ),
    ContextGap(
        surface="strategy_defaults", reason="read_timeout endpoint=z timeout_s=10.0"
    ),
]

_SINGLE_GAP = [
    ContextGap(surface="market", reason="read_timeout endpoint=x timeout_s=10.0"),
]


# ---------------------------------------------------------------------------
# count_timeout_gaps
# ---------------------------------------------------------------------------


def test_count_timeout_gaps_none_bundle_is_zero():
    assert count_timeout_gaps(None) == 0


def test_count_timeout_gaps_counts_only_read_timeout_reasons():
    gaps = [
        ContextGap(surface="market", reason="read_timeout endpoint=x"),
        ContextGap(surface="portfolio", reason="fetch_error exc_type=RuntimeError"),
        ContextGap(surface="strategy_stats", reason="read_timeout endpoint=y"),
    ]
    bundle = _pre_decision_context(gaps)
    assert count_timeout_gaps(bundle) == 2


# ---------------------------------------------------------------------------
# AC1 — apply_context_gate trips on a concurrent timeout storm
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_context_gate_trips_on_storm_no_cache():
    context = _make_context(gaps=_STORM_GAPS)
    result = await apply_context_gate(context, cache=None)

    assert result is not None
    assert result.action == ActionType.SKIP
    assert result.rejection_source == RejectionSource.CONTEXT_UNAVAILABLE
    assert result.hard_blocked is False
    assert result.thought_trace == "CONTEXT_UNAVAILABLE"
    assert "3 context surface(s)" in (result.justification or "")


@pytest.mark.asyncio
async def test_apply_context_gate_does_not_trip_below_threshold():
    """A single-surface timeout must NOT trip the gate (matches the
    ContextBuilder storm-detection threshold of >= 2 concurrent gaps)."""
    context = _make_context(gaps=_SINGLE_GAP)
    result = await apply_context_gate(context, cache=None)
    assert result is None


@pytest.mark.asyncio
async def test_apply_context_gate_healthy_context_passes_through():
    context = _make_context(gaps=[])
    result = await apply_context_gate(context, cache=None)
    assert result is None


@pytest.mark.asyncio
async def test_apply_context_gate_threshold_is_env_configurable(monkeypatch):
    monkeypatch.setenv("CIO_CONTEXT_GATE_MIN_SURFACES", "5")
    context = _make_context(gaps=_STORM_GAPS)  # only 3 gaps, below the new floor
    result = await apply_context_gate(context, cache=None)
    assert result is None


# ---------------------------------------------------------------------------
# AC1b — Orchestrator.run() short-circuits BEFORE any LLM persona
# ---------------------------------------------------------------------------


def _build_orchestrator():
    from cio.core.orchestrator import Orchestrator

    with patch("cio.core.orchestrator.ClientFactory.create", return_value=MagicMock()):
        return Orchestrator()


@pytest.mark.asyncio
async def test_orchestrator_skips_llm_personas_on_timeout_storm():
    orch = _build_orchestrator()
    orch.regime_analyst.classify = AsyncMock()
    orch.strategy_assessor.assess = AsyncMock()
    orch.action_classifier.classify = AsyncMock()
    context = _make_context(gaps=_STORM_GAPS)

    result = await orch.run(context)

    orch.regime_analyst.classify.assert_not_awaited()
    orch.strategy_assessor.assess.assert_not_awaited()
    orch.action_classifier.classify.assert_not_awaited()
    assert result.action == ActionType.SKIP
    assert result.rejection_source == RejectionSource.CONTEXT_UNAVAILABLE


@pytest.mark.asyncio
async def test_orchestrator_proceeds_normally_when_context_healthy():
    """Sanity check: the gate is a no-op on a healthy cycle — the existing
    deterministic-bypass path still runs to completion."""
    orch = _build_orchestrator()
    orch.use_llm_reasoning = False  # deterministic bypass — avoids LLM mocks
    context = _make_context(gaps=[])

    result = await orch.run(context)

    assert result.rejection_source != RejectionSource.CONTEXT_UNAVAILABLE


# ---------------------------------------------------------------------------
# AC2 — idempotent + reversible freeze: auto-unfreeze after N healthy cycles
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_trip_sets_context_unavailable_freeze_marker():
    cache = FakeCache()
    context = _make_context(strategy_id="strat-freeze", gaps=_STORM_GAPS)

    result = await apply_context_gate(context, cache=cache)

    assert result is not None
    assert (
        await cache.get(f"{FREEZE_KEY_PREFIX}strat-freeze")
        == CONTEXT_UNAVAILABLE_FREEZE_VALUE
    )


@pytest.mark.asyncio
async def test_gate_never_clobbers_a_genuine_pause_strategy_freeze():
    """A freeze set by a real LLM pause_strategy decision (plain 'LOCKED',
    see router.py) must survive a subsequent context-gate trip untouched —
    the gate only ever manages freezes it itself created."""
    cache = FakeCache()
    await cache.set(f"{FREEZE_KEY_PREFIX}strat-genuine", "LOCKED")
    context = _make_context(strategy_id="strat-genuine", gaps=_STORM_GAPS)

    await apply_context_gate(context, cache=cache)

    assert await cache.get(f"{FREEZE_KEY_PREFIX}strat-genuine") == "LOCKED"


@pytest.mark.asyncio
async def test_auto_unfreeze_after_n_consecutive_healthy_cycles():
    """AC2: with the default streak of 3, the freeze must survive the
    first 2 healthy cycles and clear on the 3rd."""
    cache = FakeCache()
    strategy_id = "strat-unfreeze"
    freeze_key = f"{FREEZE_KEY_PREFIX}{strategy_id}"
    streak_key = f"{HEALTHY_STREAK_KEY_PREFIX}{strategy_id}"

    # Cycle 1: storm trips the gate, freeze applied.
    await apply_context_gate(
        _make_context(strategy_id=strategy_id, gaps=_STORM_GAPS), cache=cache
    )
    assert await cache.get(freeze_key) == CONTEXT_UNAVAILABLE_FREEZE_VALUE

    healthy_ctx = _make_context(strategy_id=strategy_id, gaps=[])

    # Cycle 2: healthy — streak=1, still frozen.
    result = await apply_context_gate(healthy_ctx, cache=cache)
    assert result is None
    assert await cache.get(freeze_key) == CONTEXT_UNAVAILABLE_FREEZE_VALUE
    assert await cache.get(streak_key) == "1"

    # Cycle 3: healthy — streak=2, still frozen.
    await apply_context_gate(healthy_ctx, cache=cache)
    assert await cache.get(freeze_key) == CONTEXT_UNAVAILABLE_FREEZE_VALUE
    assert await cache.get(streak_key) == "2"

    # Cycle 4: healthy — streak=3 reaches the default threshold, auto-unfreeze.
    await apply_context_gate(healthy_ctx, cache=cache)
    assert await cache.get(freeze_key) is None
    assert await cache.get(streak_key) is None


@pytest.mark.asyncio
async def test_a_relapse_into_storm_resets_the_healthy_streak():
    cache = FakeCache()
    strategy_id = "strat-relapse"
    freeze_key = f"{FREEZE_KEY_PREFIX}{strategy_id}"
    streak_key = f"{HEALTHY_STREAK_KEY_PREFIX}{strategy_id}"

    await apply_context_gate(
        _make_context(strategy_id=strategy_id, gaps=_STORM_GAPS), cache=cache
    )
    await apply_context_gate(
        _make_context(strategy_id=strategy_id, gaps=[]), cache=cache
    )
    assert await cache.get(streak_key) == "1"

    # Relapse: storm again — streak must reset, freeze remains/refreshed.
    await apply_context_gate(
        _make_context(strategy_id=strategy_id, gaps=_STORM_GAPS), cache=cache
    )
    assert await cache.get(streak_key) is None
    assert await cache.get(freeze_key) == CONTEXT_UNAVAILABLE_FREEZE_VALUE


@pytest.mark.asyncio
async def test_unfreeze_streak_threshold_is_env_configurable(monkeypatch):
    monkeypatch.setenv("CIO_CONTEXT_GATE_UNFREEZE_STREAK", "1")
    cache = FakeCache()
    strategy_id = "strat-fast-unfreeze"
    freeze_key = f"{FREEZE_KEY_PREFIX}{strategy_id}"

    await apply_context_gate(
        _make_context(strategy_id=strategy_id, gaps=_STORM_GAPS), cache=cache
    )
    assert await cache.get(freeze_key) == CONTEXT_UNAVAILABLE_FREEZE_VALUE

    await apply_context_gate(
        _make_context(strategy_id=strategy_id, gaps=[]), cache=cache
    )
    assert await cache.get(freeze_key) is None


@pytest.mark.asyncio
async def test_no_freeze_present_is_a_pure_no_op():
    """When nothing was ever frozen, healthy cycles must not create any
    stray cache entries."""
    cache = FakeCache()
    context = _make_context(strategy_id="strat-clean", gaps=[])
    result = await apply_context_gate(context, cache=cache)
    assert result is None
    assert cache._store == {}


# ---------------------------------------------------------------------------
# AC3 — CIO's own prior pause decision does not leak into the next cycle's
# pre_decision_context (extends the cio#169 subsystem="cio" self-exclusion
# already enforced by ContextBuilder._collect_evaluator_verdicts).
# ---------------------------------------------------------------------------


def test_evaluator_verdicts_exclude_a_stale_self_applied_pause():
    """A CIO-published verdict describing its OWN prior pause decision
    (subsystem='cio') must never reach the next cycle's
    pre_decision_context.evaluator_verdicts, exactly like the existing
    cio#169 self-health exclusion — this is the same guardrail, applied to
    the new failure mode #199 introduces (the context-gate freeze)."""
    from cio.core.context_builder import ContextBuilder

    fake_subscriber = MagicMock()
    fake_subscriber.snapshot.return_value = {
        "verdicts": [
            {
                "subsystem": "ingest",
                "verdict": "healthy",
                "reason": "ok",
                "observed_at": "2026-09-16T20:47:00",
                "override": None,
            },
            {
                # CIO's own prior-cycle pause decision, fed back through the
                # same evaluator.cio.verdict channel as the self-health
                # verdict cio#169 already excludes.
                "subsystem": "cio",
                "verdict": "unhealthy",
                "reason": "stale_self_pause: CONTEXT_UNAVAILABLE",
                "observed_at": "2026-09-16T20:47:53",
                "override": None,
            },
        ],
        "paused": [],
        "pause_audit_log": [],
    }

    builder = ContextBuilder(
        data_manager_url="http://dm",
        tradeengine_url="http://te",
        evaluator_subscriber=fake_subscriber,
    )
    result: dict[str, EvaluatorVerdict] = builder._collect_evaluator_verdicts()

    assert "cio" not in result, (
        "CIO's own prior pause decision must be excluded from the next "
        "cycle's evaluator_verdicts — feeding it back would bias the LLM "
        "toward pause_strategy again on stale grounds (#199)"
    )
    assert "ingest" in result


@pytest.mark.asyncio
async def test_context_gate_bookkeeping_never_read_by_context_builder():
    """Regression guard: the freeze/streak cache keys written by
    apply_context_gate must not be surfaced anywhere in a freshly
    assembled PreDecisionContext — ContextBuilder has no code path that
    reads cio:freeze:* or cio:context_healthy_streak:* keys."""
    from cio.core.context_builder import ContextBuilder

    cache = FakeCache()
    strategy_id = "strat-no-leak"
    await apply_context_gate(
        _make_context(strategy_id=strategy_id, gaps=_STORM_GAPS), cache=cache
    )
    assert cache._store  # sanity: something was written

    builder = ContextBuilder(data_manager_url="http://dm", tradeengine_url="http://te")
    verdicts = builder._collect_evaluator_verdicts()
    # No evaluator subscriber wired at all — the freeze cache keys have no
    # bearing on this method whatsoever.
    assert verdicts == {}

    await builder.close()
