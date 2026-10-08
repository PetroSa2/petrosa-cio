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
        async def report(self, timeout=None):
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
        async def report(self, timeout=None):
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


# --- the decision path reads a cached report (petrosa-cio#312 style: no data-manager call per decision) ---


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _service_with_fetches(outcomes, clock):
    """A service whose report() pops ``outcomes`` (a report dict or an exception) and counts the fetches."""
    from cio.core.confidence_calibration import ConfidenceCalibrationService

    class Service(ConfidenceCalibrationService):
        calls = 0
        timeouts: list = []

        async def report(self, timeout=None):
            Service.calls += 1
            Service.timeouts.append(timeout)
            outcome = outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    Service.calls = 0
    Service.timeouts = []
    return Service(clock=clock), Service


GOOD = {"groups": [{"strategy_id": "s", "calibrated": True}]}


def test_two_decisions_within_the_ttl_make_one_fetch(monkeypatch) -> None:
    monkeypatch.delenv("CIO_CALIBRATION_TTL_SECONDS", raising=False)
    monkeypatch.delenv("CIO_CALIBRATION_TIMEOUT_SECONDS", raising=False)
    clock = _Clock()
    service, cls = _service_with_fetches([GOOD], clock)

    async def two():
        first = await service.status("s")
        clock.now += 899
        second = await service.status("s")
        return first, second

    first, second = asyncio.run(two())
    assert first == second == {"strategy_id": "s", "calibrated": True}
    assert cls.calls == 1
    assert cls.timeouts == [2.0]  # the short decision-path timeout, not the endpoint's


def test_the_report_is_fetched_again_after_the_ttl(monkeypatch) -> None:
    monkeypatch.setenv("CIO_CALIBRATION_TTL_SECONDS", "60")
    clock = _Clock()
    service, cls = _service_with_fetches([GOOD], clock)

    async def run():
        await service.status("s")
        clock.now += 61
        await service.status("s")

    asyncio.run(run())
    assert cls.calls == 2


def test_a_failed_fetch_is_not_retried_until_the_ttl(monkeypatch, caplog) -> None:
    import logging

    monkeypatch.delenv("CIO_CALIBRATION_TTL_SECONDS", raising=False)
    clock = _Clock()
    service, cls = _service_with_fetches([RuntimeError("404")], clock)
    caplog.set_level(logging.WARNING, logger="cio.core.confidence_calibration")

    async def run():
        results = [await service.status("s")]
        for _ in range(5):  # five more decisions inside the TTL
            clock.now += 100
            results.append(await service.status("s"))
        return results

    assert asyncio.run(run()) == [None] * 6  # no report: the neutral prior
    assert cls.calls == 1
    warnings = [r for r in caplog.records if "report unavailable" in r.getMessage()]
    assert len(warnings) == 1  # at most one warning per TTL
    assert "using the neutral prior" in warnings[0].getMessage()
    # after the TTL it tries again, and warns again
    clock.now += 500
    asyncio.run(service.status("s"))
    assert cls.calls == 2
    assert (
        len([r for r in caplog.records if "report unavailable" in r.getMessage()]) == 2
    )


def test_a_failed_refresh_keeps_the_last_good_report(monkeypatch, caplog) -> None:
    import logging

    monkeypatch.setenv("CIO_CALIBRATION_TTL_SECONDS", "60")
    clock = _Clock()
    service, cls = _service_with_fetches([GOOD, RuntimeError("down")], clock)
    caplog.set_level(logging.WARNING, logger="cio.core.confidence_calibration")

    async def run():
        first = await service.status("s")
        clock.now += 61
        second = await service.status("s")  # the refresh fails
        clock.now += 30
        third = await service.status("s")  # backed off: no new fetch
        return first, second, third

    first, second, third = asyncio.run(run())
    assert first == second == third == {"strategy_id": "s", "calibrated": True}
    assert cls.calls == 2
    assert any("using the last good report" in r.getMessage() for r in caplog.records)


def test_concurrent_decisions_share_one_fetch(monkeypatch) -> None:
    from cio.core.confidence_calibration import ConfidenceCalibrationService

    class Service(ConfidenceCalibrationService):
        calls = 0

        async def report(self, timeout=None):
            Service.calls += 1
            await asyncio.sleep(0.05)
            return GOOD

    Service.calls = 0

    async def run():
        service = Service()
        return await asyncio.gather(*(service.status("s") for _ in range(10)))

    results = asyncio.run(run())
    assert all(r == {"strategy_id": "s", "calibrated": True} for r in results)
    assert Service.calls == 1


def test_the_ttl_and_timeout_are_env_inputs_with_defaults(monkeypatch) -> None:
    from cio.core.confidence_calibration import (
        calibration_timeout_seconds,
        calibration_ttl_seconds,
    )

    monkeypatch.delenv("CIO_CALIBRATION_TTL_SECONDS", raising=False)
    monkeypatch.delenv("CIO_CALIBRATION_TIMEOUT_SECONDS", raising=False)
    assert (calibration_ttl_seconds(), calibration_timeout_seconds()) == (900.0, 2.0)
    for bad in ("0", "-5", "x"):
        monkeypatch.setenv("CIO_CALIBRATION_TTL_SECONDS", bad)
        assert calibration_ttl_seconds() == 900.0
    monkeypatch.setenv("CIO_CALIBRATION_TTL_SECONDS", "30")
    monkeypatch.setenv("CIO_CALIBRATION_TIMEOUT_SECONDS", "0.5")
    assert (calibration_ttl_seconds(), calibration_timeout_seconds()) == (30.0, 0.5)


def test_the_fresh_report_endpoint_read_is_unchanged_and_passes_the_timeout() -> None:
    import httpx

    from cio.core.confidence_calibration import ConfidenceCalibrationService

    seen = []

    class Client:
        async def get(self, url, **kwargs):
            seen.append((url, kwargs))
            response = httpx.Response(
                200,
                json={
                    "records": [{"strategy_id": "s", "confidence": 0.9, "net_pnl": 1.0}]
                },
                request=httpx.Request("GET", url),
            )
            return response

    service = ConfidenceCalibrationService(
        client=Client(), data_manager_url="http://dm"
    )
    asyncio.run(service.report())
    asyncio.run(service.report(timeout=2.0))
    assert seen[0] == ("http://dm/analysis/calibration/confidence", {})
    assert seen[1] == ("http://dm/analysis/calibration/confidence", {"timeout": 2.0})
