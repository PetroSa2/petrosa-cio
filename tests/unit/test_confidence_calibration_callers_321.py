import asyncio

from cio.core.confidence_calibration import calibration_mode, effective_confidence
from cio.core.orchestrator import Orchestrator
from cio.models.context import TriggerContext


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
