import asyncio

from cio.core.confidence_calibration import calibration_mode, effective_confidence
from cio.core.orchestrator import Orchestrator
from cio.models.context import StrategyStats, TriggerContext


def test_uncalibrated_confidences_produce_identical_effective_inputs() -> None:
    assert effective_confidence(0.61, {"calibrated": False}) == effective_confidence(
        0.95, {"calibrated": False}
    )


def test_calibration_status_is_fetched_and_log_only_preserves_mode(monkeypatch) -> None:
    class Service:
        async def status(self, strategy_id: str):
            assert strategy_id == "strategy"
            return {"strategy_id": strategy_id, "calibrated": False}

    monkeypatch.delenv("CIO_CALIBRATION_MODE", raising=False)
    orchestrator = Orchestrator(calibration_service=Service())
    context = TriggerContext.model_construct(
        strategy_id="strategy", trigger_payload={"confidence": 0.95}
    )

    status = asyncio.run(orchestrator._calibration_status(context))

    assert status == {"strategy_id": "strategy", "calibrated": False}
    assert calibration_mode() == "log_only"


def test_enforce_mode_is_explicit(monkeypatch) -> None:
    monkeypatch.setenv("CIO_CALIBRATION_MODE", "enforce")
    assert calibration_mode() == "enforce"


def test_calibrated_context_replaces_ev_input_with_neutral_prior() -> None:
    context = TriggerContext.model_construct(
        strategy_id="strategy",
        trigger_payload={"confidence": 0.95},
        strategy_stats=StrategyStats(win_rate=0.8),
    )

    calibrated = Orchestrator._calibrated_context(
        context, {"strategy_id": "strategy", "calibrated": False}
    )

    assert calibrated.strategy_stats.win_rate == 0.5
    assert (
        Orchestrator._calibrated_context(
            context, {"strategy_id": "strategy", "calibrated": True}
        ).strategy_stats.win_rate
        == 0.95
    )
