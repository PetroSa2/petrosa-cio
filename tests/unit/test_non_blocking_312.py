"""The decision path never waits on data-manager reports and a stalled loop leaves a stack (petrosa-cio#312)."""

import asyncio
import logging
import time
from unittest.mock import MagicMock, patch

import httpx
import pytest

from cio.core import stage_timing
from cio.core.context_builder import ContextBuilder, _report_budget_s
from cio.core.loop_watchdog import LoopWatchdog
from cio.models import RegimeEnum, RegimeResult, VolatilityLevel
from cio.models.enums import ConfidenceLevel

SLIPPAGE = {
    "by_regime": {"balanced_market": {"median_bp": 3.0, "count": 50}},
    "overall": {"median_bp": 2.5, "count": 200},
}
RISK = {
    "symbols": {"BTCUSDT": {"sigma_daily_best": {"sufficient": True, "value": 0.03}}},
    "correlation": {},
    "equity": {"sigma_daily": 0.002, "sufficient": False},
}
ROUNDS = {
    "strategies": {
        "s1": {"fills": 10, "closed_rounds": 6, "wins": 4, "losses": 2},
        "s2": {"fills": 12, "closed_rounds": 8, "wins": 3, "losses": 5},
    }
}


def response(body):
    r = MagicMock(spec=httpx.Response)
    r.status_code = 200
    r.json.return_value = body
    r.raise_for_status.return_value = None
    return r


def regime():
    return RegimeResult(
        regime=RegimeEnum.RANGING,
        regime_confidence=ConfidenceLevel.HIGH,
        volatility_level=VolatilityLevel.MEDIUM,
        primary_signal="t",
        thought_trace="t",
        data_manager_regime="balanced_market",
    )


class Stub:
    """A data-manager stub: ``delay`` seconds before it answers, counting the calls per path."""

    def __init__(self, delay=0.0):
        self.delay = delay
        self.calls: dict[str, int] = {}
        self.release = asyncio.Event()

    async def get(self, url, **kwargs):
        text = str(url)
        key = (
            "slippage"
            if "slippage-by-regime" in text
            else "risk_inputs"
            if "risk/inputs" in text
            else "rounds"
        )
        self.calls[key] = self.calls.get(key, 0) + 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return response(
            SLIPPAGE if key == "slippage" else RISK if key == "risk_inputs" else ROUNDS
        )


def builder(monkeypatch, budget="0.2", clock=None):
    monkeypatch.setenv("CIO_REPORT_FETCH_BUDGET_S", budget)
    return ContextBuilder("http://dm", "http://te", clock=clock)


async def lag_during(coro):
    """Run ``coro`` while measuring the longest the loop went without answering a 10 ms ticker."""
    worst = 0.0
    stop = asyncio.Event()

    async def ticker():
        nonlocal worst
        last = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            worst = max(worst, now - last - 0.01)
            last = now

    task = asyncio.create_task(ticker())
    try:
        result = await coro
    finally:
        stop.set()
        await task
    return result, worst


# --- the reports are off the decision path -------------------------------------------------------


@pytest.mark.asyncio
async def test_a_hung_data_manager_costs_a_decision_at_most_the_budget(monkeypatch):
    stub = Stub(delay=30.0)
    b = builder(monkeypatch)
    started = time.perf_counter()
    with patch("httpx.AsyncClient.get", side_effect=stub.get):
        (slippage, (prior, rounds)), lag = await lag_during(
            asyncio.gather(
                b._fetch_slippage(regime(), "cid"), b._fetch_rounds("s1", "cid")
            )
        )
    elapsed = time.perf_counter() - started
    assert (
        slippage is None and prior is None and rounds is None
    )  # the labelled fallbacks
    assert elapsed < 1.0  # both waited concurrently, once, for the 0.2 s budget
    assert lag < 0.1  # the loop kept running
    for task in b._refresh_tasks.values():
        task.cancel()


@pytest.mark.asyncio
async def test_a_missed_budget_is_not_paid_again_while_the_refresh_is_in_flight(
    monkeypatch,
):
    stub = Stub(delay=30.0)
    b = builder(monkeypatch)
    with patch("httpx.AsyncClient.get", side_effect=stub.get):
        await b._fetch_slippage(regime(), "cid")
        started = time.perf_counter()
        assert await b._fetch_slippage(regime(), "cid") is None
        assert time.perf_counter() - started < 0.05
    assert stub.calls == {"slippage": 1}  # one refresh serves every decision
    b._refresh_tasks["slippage"].cancel()


@pytest.mark.asyncio
async def test_the_refresh_fills_the_cache_after_a_missed_budget(monkeypatch):
    stub = Stub(delay=0.4)
    b = builder(monkeypatch, budget="0.1")
    with patch("httpx.AsyncClient.get", side_effect=stub.get):
        assert await b._fetch_slippage(regime(), "cid") is None
        await b._refresh_tasks["slippage"]
        estimate = await b._fetch_slippage(regime(), "cid")
    assert estimate.median_bp == 3.0 and estimate.pooled_median_bp == 2.5
    assert stub.calls == {"slippage": 1}


@pytest.mark.asyncio
async def test_concurrent_decisions_share_one_refresh(monkeypatch):
    stub = Stub(delay=0.05)
    b = builder(monkeypatch, budget="1")
    with patch("httpx.AsyncClient.get", side_effect=stub.get):
        results = await asyncio.gather(
            *(b._fetch_rounds("s1", "cid") for _ in range(8))
        )
    assert stub.calls == {"rounds": 1}
    assert all(rounds.wins == 4 for _, rounds in results)


@pytest.mark.asyncio
async def test_a_stale_report_is_served_while_it_refreshes_in_the_background(
    monkeypatch,
):
    now = [0.0]
    stub = Stub()
    b = builder(monkeypatch, budget="1", clock=lambda: now[0])
    with patch("httpx.AsyncClient.get", side_effect=stub.get):
        await b._fetch_slippage(regime(), "cid")
        assert stub.calls == {"slippage": 1}
        now[0] = 4000.0  # past the hour
        stub.delay = 5.0
        started = time.perf_counter()
        estimate = await b._fetch_slippage(regime(), "cid")
        assert time.perf_counter() - started < 0.1  # served stale, no wait
        assert estimate.median_bp == 3.0
        await asyncio.sleep(0)  # let the background refresh start
        assert stub.calls == {"slippage": 2}  # and the refresh started
        b._refresh_tasks["slippage"].cancel()


@pytest.mark.asyncio
async def test_a_failed_refresh_caches_the_empty_result_for_a_minute(monkeypatch):
    async def boom(url, **kwargs):
        raise httpx.ConnectError("down")

    b = builder(monkeypatch, budget="1")
    with patch("httpx.AsyncClient.get", side_effect=boom):
        assert await b._fetch_slippage(regime(), "cid") is None
        assert await b._fetch_rounds("s1", "cid") == (None, None)


@pytest.mark.asyncio
async def test_warm_reports_starts_both_refreshes_so_the_first_decision_finds_them(
    monkeypatch,
):
    stub = Stub(delay=0.05)
    b = builder(monkeypatch, budget="0.01")
    with patch("httpx.AsyncClient.get", side_effect=stub.get):
        b.warm_reports()
        b.warm_reports()  # idempotent while in flight
        await asyncio.gather(*b._refresh_tasks.values())
        started = time.perf_counter()
        slippage = await b._fetch_slippage(regime(), "cid")
        _, rounds = await b._fetch_rounds("s1", "cid")
    assert time.perf_counter() - started < 0.05
    assert slippage.median_bp == 3.0 and rounds.closed_rounds == 6
    assert stub.calls == {"slippage": 1, "rounds": 1, "risk_inputs": 1}


@pytest.mark.asyncio
async def test_a_whole_context_build_completes_on_fallbacks_with_hung_reports(
    monkeypatch,
):
    from cio.models import TriggerType

    stub = Stub(delay=30.0)

    async def get(url, **kwargs):
        text = str(url)
        if (
            "slippage-by-regime" in text
            or "analysis/rounds" in text
            or "risk/inputs" in text
        ):
            return await stub.get(url)
        if "analysis/regime" in text:
            return response(
                {
                    "pair": "BTCUSDT",
                    "metric": "regime",
                    "data": {
                        "regime": "balanced_market",
                        "volatility_level": "medium",
                        "volume_level": "normal",
                        "trend_direction": "neutral",
                        "confidence": "0.9",
                    },
                    "metadata": {
                        "timestamp": "2026-10-07T12:00:00Z",
                        "collection": "c",
                    },
                }
            )
        return response({})

    b = builder(monkeypatch)
    started = time.perf_counter()
    with patch("httpx.AsyncClient.get", side_effect=get):
        context = await asyncio.wait_for(
            b.build(
                correlation_id="cid",
                source_subject="cio.intent.trading.s1",
                trigger_type=TriggerType.TRADE_INTENT,
                payload={"symbol": "BTCUSDT", "strategy_id": "s1", "side": "long"},
            ),
            timeout=8.0,
        )
    assert time.perf_counter() - started < 5.0
    assert context.risk_inputs is None  # the third report, hung as well
    assert context.slippage is None and context.prior_strength is None
    assert context.strategy_rounds is None
    for task in b._refresh_tasks.values():
        task.cancel()


def test_the_budget_is_an_env_input_with_a_two_second_default(monkeypatch):
    monkeypatch.delenv("CIO_REPORT_FETCH_BUDGET_S", raising=False)
    assert _report_budget_s() == 2.0
    for bad in ("0", "-1", "x"):
        monkeypatch.setenv("CIO_REPORT_FETCH_BUDGET_S", bad)
        assert _report_budget_s() == 2.0
    monkeypatch.setenv("CIO_REPORT_FETCH_BUDGET_S", "0.5")
    assert _report_budget_s() == 0.5


# --- the loop watchdog ---------------------------------------------------------------------------


def block_the_loop(seconds):
    time.sleep(seconds)


@pytest.mark.asyncio
async def test_a_blocked_loop_logs_the_stack_it_is_stuck_in_and_the_recovery(caplog):
    caplog.set_level(logging.ERROR, logger="cio.core.loop_watchdog")
    watchdog = LoopWatchdog(
        asyncio.get_running_loop(), threshold_s=0.2, interval_s=0.05
    )
    watchdog.start()
    await asyncio.sleep(0.1)
    block_the_loop(0.8)  # blocks the loop thread
    await asyncio.sleep(0.3)
    watchdog.stop()
    text = caplog.text
    assert "EVENT_LOOP_BLOCKED" in text
    assert "block_the_loop" in text  # the stack names the culprit
    assert "EVENT_LOOP_RECOVERED" in text
    assert watchdog.stalls == 1 and watchdog.max_stall_s >= 0.7


@pytest.mark.asyncio
async def test_a_healthy_loop_logs_nothing(caplog):
    caplog.set_level(logging.ERROR, logger="cio.core.loop_watchdog")
    watchdog = LoopWatchdog(
        asyncio.get_running_loop(), threshold_s=0.3, interval_s=0.02
    )
    watchdog.start()
    for _ in range(10):
        await asyncio.sleep(0.03)
    watchdog.stop()
    assert "EVENT_LOOP" not in caplog.text and watchdog.stalls == 0


@pytest.mark.asyncio
async def test_the_watchdog_thresholds_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("CIO_LOOP_WATCHDOG_THRESHOLD_S", "7")
    monkeypatch.setenv("CIO_LOOP_WATCHDOG_INTERVAL_S", "x")
    watchdog = LoopWatchdog(asyncio.get_running_loop())
    assert watchdog.threshold_s == 7.0 and watchdog.interval_s == 1.0


@pytest.mark.asyncio
async def test_the_watchdog_stops_when_the_loop_is_closed():
    loop = asyncio.new_event_loop()
    watchdog = LoopWatchdog(loop, threshold_s=0.1, interval_s=0.01)
    loop.close()
    watchdog._run()  # call_soon_threadsafe raises on a closed loop: the thread ends, no error


# --- per-stage timings ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stages_are_timed_across_tasks_and_summarised_in_order():
    stage_timing.begin()

    async def work(name, seconds):
        with stage_timing.stage(name):
            await asyncio.sleep(seconds)

    with stage_timing.stage("context"):
        await asyncio.sleep(0.02)
    await asyncio.gather(work("llm", 0.03), work("llm", 0.03))  # tasks share the record
    line = stage_timing.summary()
    assert line.startswith("context=") and "llm=" in line
    assert float(line.split("llm=")[1].split("ms")[0]) >= 55  # both added up


def test_without_a_decision_record_stages_are_a_no_op():
    from contextvars import copy_context

    def inside():
        with stage_timing.stage("x"):
            pass
        return stage_timing.summary()

    assert copy_context().run(inside) in {"-", stage_timing.summary()}
    assert stage_timing.summary({}) == "-"


@pytest.mark.asyncio
async def test_an_unexpected_refresh_error_is_logged_and_the_decision_goes_on(
    monkeypatch, caplog
):
    caplog.set_level(logging.WARNING, logger="cio.core.context_builder")
    b = builder(monkeypatch, budget="1")

    async def broken(correlation_id):
        raise RuntimeError("refresh bug")

    monkeypatch.setattr(b, "_refresh_slippage", broken)
    assert await b._fetch_slippage(regime(), "cid") is None
    await asyncio.sleep(0)
    assert "REPORT_REFRESH_FAILED report=slippage: refresh bug" in caplog.text


@pytest.mark.asyncio
async def test_the_watchdog_starts_once_and_reports_an_unavailable_stack(caplog):
    watchdog = LoopWatchdog(
        asyncio.get_running_loop(), threshold_s=5.0, interval_s=0.05
    )
    assert watchdog._stack() == "unavailable"  # not started: no loop thread to read
    watchdog.start()
    first = watchdog._thread
    watchdog.start()
    assert watchdog._thread is first
    watchdog.stop()


@pytest.mark.asyncio
async def test_stopping_the_watchdog_during_a_stall_ends_its_thread():
    import threading

    watchdog = LoopWatchdog(
        asyncio.get_running_loop(), threshold_s=0.1, interval_s=0.05
    )
    watchdog.start()
    await asyncio.sleep(0.05)
    threading.Timer(0.3, watchdog.stop).start()
    block_the_loop(0.7)  # the loop is blocked when stop() is called
    await asyncio.sleep(0.2)
    watchdog._thread.join(1.0)
    assert not watchdog._thread.is_alive()
