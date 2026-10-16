from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cio.core.concentration_cap import ConcentrationConfig, evaluate, load_config
from cio.core.decision_store import DecisionRecord, DecisionStore
from cio.core.router import OutputRouter
from cio.models import (
    ActionType,
    ActivationRecommendation,
    ConfidenceLevel,
    DecisionResult,
    HealthStatus,
    RegimeFit,
    TriggerContext,
)


def test_under_cap_and_exact_hour_boundary_are_included():
    now = datetime(2026, 10, 16, 12, tzinfo=UTC)
    records = [
        SimpleNamespace(
            strategy_id="other",
            action="execute",
            timestamp=now - timedelta(hours=1),
            new_notional_usd=300,
        )
    ]
    result = evaluate(
        strategy_id="target",
        proposed_notional_usd=100,
        records=records,
        at=now,
        config=ConcentrationConfig(0.25, "test", "shadow", "test"),
    )
    assert result.share == 0.25
    assert not result.exceeded
    assert result.result == "under_cap"


def test_old_execute_is_outside_the_rolling_hour():
    now = datetime(2026, 10, 16, 12, tzinfo=UTC)
    result = evaluate(
        strategy_id="target",
        proposed_notional_usd=100,
        records=[
            SimpleNamespace(
                strategy_id="other",
                action="execute",
                timestamp=now - timedelta(hours=1, seconds=1),
                new_notional_usd=300,
            )
        ],
        at=now,
        config=ConcentrationConfig(0.25, "test", "shadow", "test"),
    )
    assert result.share == 1.0
    assert result.exceeded


def test_over_cap_records_enforce_and_shadow_modes():
    now = datetime(2026, 10, 16, 12, tzinfo=UTC)
    records = [
        SimpleNamespace(
            strategy_id="target",
            action="execute",
            timestamp=now - timedelta(minutes=10),
            new_notional_usd=300,
        ),
        SimpleNamespace(
            strategy_id="other",
            action="execute",
            timestamp=now - timedelta(minutes=10),
            new_notional_usd=100,
        ),
    ]
    enforce = evaluate(
        strategy_id="target",
        proposed_notional_usd=100,
        records=records,
        at=now,
        config=ConcentrationConfig(0.5, "test", "enforce", "test"),
    )
    shadow = evaluate(
        strategy_id="target",
        proposed_notional_usd=100,
        records=records,
        at=now,
        config=ConcentrationConfig(0.5, "test", "shadow", "test"),
    )
    assert enforce.exceeded and enforce.mode == "enforce"
    assert shadow.exceeded and shadow.mode == "shadow"
    assert shadow.as_reason()["type"] == "concentration_cap"


def test_concentration_metrics_record_share_and_result():
    now = datetime(2026, 10, 16, 12, tzinfo=UTC)
    with patch("cio.core.metrics.CONCENTRATION_CAP_TOTAL") as counter:
        result = evaluate(
            strategy_id="target",
            proposed_notional_usd=100,
            records=[],
            at=now,
            config=ConcentrationConfig(0.5, "test", "shadow", "test"),
        )

    from cio.core import metrics

    assert metrics._concentration_shares["target"] == pytest.approx(1.0)
    counter.add.assert_called_once_with(1, {"mode": "shadow", "result": "over_cap"})
    assert result.result == "over_cap"


def test_invalid_configuration_uses_labelled_shadow_fallback(monkeypatch):
    monkeypatch.setenv("CIO_CONCENTRATION_CAP_PCT", "not-a-percent")
    monkeypatch.setenv("CIO_CONCENTRATION_CAP_MODE", "apply")
    config = load_config()
    assert config.cap == 0.25
    assert config.cap_source == "fallback"
    assert config.mode == "shadow"
    assert config.mode_source == "fallback"


@pytest.mark.asyncio
async def test_router_enforce_skips_and_persists_structured_reason():
    store = DecisionStore()
    now = datetime.now(UTC)
    store.record(
        DecisionRecord(
            strategy_id="target",
            action="execute",
            reasoning_trace="prior",
            confidence=0.9,
            timestamp=now - timedelta(minutes=5),
            new_notional_usd=300,
        )
    )
    store.record(
        DecisionRecord(
            strategy_id="other",
            action="execute",
            reasoning_trace="prior",
            confidence=0.9,
            timestamp=now - timedelta(minutes=5),
            new_notional_usd=100,
        )
    )
    context = MagicMock(spec=TriggerContext)
    context.strategy_id = "target"
    context.decision_id = "decision-cap"
    context.correlation_id = "correlation-cap"
    context.trigger_payload = {"symbol": "BTCUSDT"}
    decision = DecisionResult(
        hard_blocked=False,
        ev_passes=True,
        cost_viable=True,
        regime_confidence=ConfidenceLevel.HIGH,
        regime_fit=RegimeFit.GOOD,
        strategy_health=HealthStatus.HEALTHY,
        activation_recommendation=ActivationRecommendation.RUN,
        computed_position_size_usd=100,
        action=ActionType.EXECUTE,
    )
    router = OutputRouter(
        nats_client=AsyncMock(),
        vector_client=AsyncMock(),
        decision_store=store,
    )
    with patch.dict(
        "os.environ",
        {
            "CIO_CONCENTRATION_CAP_PCT": "0.5",
            "CIO_CONCENTRATION_CAP_MODE": "enforce",
            "DRY_RUN": "false",
        },
    ):
        await router.route(context, decision)

    assert decision.action == ActionType.SKIP
    assert decision.rejection_source.value == "concentration_cap"
    assert decision.concentration_cap["type"] == "concentration_cap"
    signal_calls = [
        call
        for call in router.nats_client.publish.await_args_list
        if str(call.args[0]).startswith("signals.trading.")
    ]
    assert signal_calls == []

    shadow_nats = AsyncMock()
    shadow_router = OutputRouter(
        nats_client=shadow_nats,
        vector_client=AsyncMock(),
        decision_store=store,
    )
    shadow_decision = decision.model_copy(update={"action": ActionType.EXECUTE})
    with patch.dict(
        "os.environ",
        {
            "CIO_CONCENTRATION_CAP_PCT": "0.5",
            "CIO_CONCENTRATION_CAP_MODE": "shadow",
            "DRY_RUN": "false",
        },
    ):
        await shadow_router.route(context, shadow_decision)

    assert shadow_decision.action == ActionType.EXECUTE
    assert shadow_decision.concentration_cap["mode"] == "shadow"
    assert shadow_decision.concentration_cap["type"] == "concentration_cap"
