import asyncio

from cio.core.confidence_calibration import calibration_mode, effective_confidence
from cio.core.orchestrator import Orchestrator
from cio.models import StrategyStats, TriggerContext


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


def test_calibration_status_failure_is_non_fatal(monkeypatch) -> None:
    class Service:
        async def status(self, strategy_id: str):
            raise RuntimeError("data-manager unavailable")

    monkeypatch.delenv("CIO_CALIBRATION_MODE", raising=False)
    orchestrator = Orchestrator(calibration_service=Service())
    context = TriggerContext.model_construct(
        strategy_id="strategy", trigger_payload={"confidence": "bad"}
    )

    import asyncio

    assert asyncio.run(orchestrator._calibration_status(context)) is None


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


def test_enforce_mode_runs_the_engine_with_calibrated_context(monkeypatch) -> None:
    from unittest.mock import AsyncMock, MagicMock, patch

    from test_orchestrator_persona_concurrency import (
        _make_context,
        _make_regime_result,
        _make_strategy_result,
        _mock_cache,
        _patched_engine_no_block,
    )

    class Service:
        async def status(self, strategy_id: str):
            return {"strategy_id": strategy_id, "calibrated": False}

    monkeypatch.setenv("CIO_CALIBRATION_MODE", "enforce")
    with (
        patch("cio.core.orchestrator.CodeEngine") as engine,
        patch("cio.core.orchestrator.RegimeAnalyst") as regime,
        patch("cio.core.orchestrator.StrategyAssessor") as strategy,
        patch("cio.core.orchestrator.ActionClassifier") as classifier,
    ):
        engine.run.return_value = _patched_engine_no_block()
        regime.return_value.classify = AsyncMock(return_value=_make_regime_result())
        strategy.return_value.assess = AsyncMock(return_value=_make_strategy_result())
        classifier.return_value.classify = AsyncMock(return_value=MagicMock())
        orchestrator = Orchestrator(
            cache=_mock_cache(None, None), calibration_service=Service()
        )

        import asyncio

        asyncio.run(orchestrator.run(_make_context()))

        assert engine.run.call_count == 1
        assert engine.run.call_args.args[0].strategy_stats.win_rate == 0.5


def test_log_only_mode_computes_diagnostics_without_replacing_context(
    monkeypatch,
) -> None:
    from unittest.mock import AsyncMock, MagicMock, patch

    from test_orchestrator_persona_concurrency import (
        _make_context,
        _make_regime_result,
        _make_strategy_result,
        _mock_cache,
        _patched_engine_no_block,
    )

    class Service:
        async def status(self, strategy_id: str):
            return {"strategy_id": strategy_id, "calibrated": False}

    monkeypatch.setenv("CIO_CALIBRATION_MODE", "log_only")
    with (
        patch("cio.core.orchestrator.CodeEngine") as engine,
        patch("cio.core.orchestrator.RegimeAnalyst") as regime,
        patch("cio.core.orchestrator.StrategyAssessor") as strategy,
        patch("cio.core.orchestrator.ActionClassifier") as classifier,
    ):
        engine.run.return_value = _patched_engine_no_block()
        regime.return_value.classify = AsyncMock(return_value=_make_regime_result())
        strategy.return_value.assess = AsyncMock(return_value=_make_strategy_result())
        classifier.return_value.classify = AsyncMock(return_value=MagicMock())
        orchestrator = Orchestrator(
            cache=_mock_cache(None, None), calibration_service=Service()
        )

        import asyncio

        context = _make_context()
        asyncio.run(orchestrator.run(context))

        assert engine.run.call_count == 2
        assert engine.run.call_args_list[0].args[0].strategy_stats.win_rate is None
        assert engine.run.call_args_list[1].args[0].strategy_stats.win_rate == 0.5


def test_service_status_returns_the_strategys_group_from_the_report() -> None:
    from cio.core.confidence_calibration import ConfidenceCalibrationService

    class Service(ConfidenceCalibrationService):
        async def report(self):
            return {
                "groups": [
                    {"strategy_id": "other", "calibrated": True},
                    {"strategy_id": "strategy", "calibrated": False},
                ]
            }

    import asyncio

    service = Service()
    assert asyncio.run(service.status("strategy")) == {
        "strategy_id": "strategy",
        "calibrated": False,
    }


def test_service_status_is_none_for_a_strategy_without_a_group() -> None:
    from cio.core.confidence_calibration import ConfidenceCalibrationService

    class Service(ConfidenceCalibrationService):
        async def report(self):
            return {"groups": [{"strategy_id": "other", "calibrated": True}]}

    import asyncio

    assert asyncio.run(Service().status("strategy")) is None
    assert asyncio.run(Service().status("")) is None


def test_calibrated_context_falls_back_to_the_neutral_input_for_an_unreadable_confidence() -> (
    None
):
    for payload in ({"confidence": "bad"}, {"confidence": None}):
        context = TriggerContext.model_construct(
            strategy_id="strategy",
            trigger_payload=payload,
            strategy_stats=StrategyStats(win_rate=0.8),
        )
        uncalibrated = Orchestrator._calibrated_context(
            context, {"strategy_id": "strategy", "calibrated": False}
        )
        assert uncalibrated.strategy_stats.win_rate == 0.5  # the neutral prior
        calibrated = Orchestrator._calibrated_context(
            context, {"strategy_id": "strategy", "calibrated": True}
        )
        assert (
            calibrated.strategy_stats.win_rate == 0.5
        )  # an unreadable raw confidence reads as 0.5
