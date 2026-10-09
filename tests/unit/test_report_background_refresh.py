"""Reports refresh in the background and keep the last good one, with an age bound (petrosa-cio#312 follow-up)."""

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import httpx
import pytest

from cio.core import context_builder as cb_module
from cio.core import report_freshness as rf
from cio.core.confidence_calibration import ConfidenceCalibrationService
from cio.core.context_builder import ContextBuilder
from cio.core.orchestrator import Orchestrator
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
    "strategies": {"s1": {"fills": 10, "closed_rounds": 6, "wins": 4, "losses": 2}}
}
GOOD = {"groups": [{"strategy_id": "s", "calibrated": True}]}


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


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class DataManager:
    """Answers the three reports; ``fail`` makes every call raise; records the keyword arguments of each call."""

    def __init__(self, bodies=None):
        self.fail: Exception | None = None
        self.bodies = bodies or {}
        self.kwargs: list[dict] = []
        self.calls = 0

    async def get(self, url, **kwargs):
        self.calls += 1
        self.kwargs.append(kwargs)
        if self.fail is not None:
            raise self.fail
        text = str(url)
        key = (
            "slippage"
            if "slippage-by-regime" in text
            else "risk_inputs"
            if "risk/inputs" in text
            else "rounds"
        )
        default = {"slippage": SLIPPAGE, "risk_inputs": RISK, "rounds": ROUNDS}[key]
        return response(self.bodies.get(key, default))


def builder(clock):
    return ContextBuilder("http://dm", "http://te", clock=clock)


async def settle(b):
    for task in list(b._refresh_tasks.values()):
        if not task.done():
            await task


READERS = {
    "slippage": lambda b: b._fetch_slippage(regime(), "cid"),
    "risk_inputs": lambda b: b._fetch_risk_inputs("cid"),
    "rounds": lambda b: b._fetch_rounds("s1", "cid"),
}


def has_data(name, value):
    if name == "rounds":
        return value[1] is not None
    return value is not None


# --- the reports: last good + age bound ----------------------------------------------------------


@pytest.mark.parametrize("name", ["slippage", "risk_inputs", "rounds"])
@pytest.mark.asyncio
async def test_a_failed_refresh_keeps_the_last_good_report(name, monkeypatch, caplog):
    monkeypatch.setenv("CIO_REPORT_FETCH_BUDGET_S", "1")
    clock = Clock()
    dm = DataManager()
    b = builder(clock)
    caplog.set_level(logging.WARNING)
    with patch("httpx.AsyncClient.get", side_effect=dm.get):
        assert has_data(name, await READERS[name](b))
        clock.now += 3601  # the hour is over: the next read starts a refresh
        dm.fail = httpx.ReadTimeout("")
        served = await READERS[name](b)  # served from the last good at once
        assert has_data(name, served)
        await settle(b)
        clock.now += 30  # inside the backoff: still the last good, no new call
        calls = dm.calls
        assert has_data(name, await READERS[name](b))
        await settle(b)
        assert dm.calls == calls
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "exc_type=ReadTimeout" in text and "the last good report (age" in text


@pytest.mark.parametrize("name", ["slippage", "risk_inputs", "rounds"])
@pytest.mark.asyncio
async def test_a_report_older_than_the_bound_is_not_used(name, monkeypatch):
    monkeypatch.setenv("CIO_REPORT_FETCH_BUDGET_S", "1")
    monkeypatch.setenv("CIO_REPORT_MAX_AGE_SECONDS", "7200")
    clock = Clock()
    dm = DataManager()
    b = builder(clock)
    with patch("httpx.AsyncClient.get", side_effect=dm.get):
        assert has_data(name, await READERS[name](b))
        dm.fail = httpx.ConnectError("down")
        clock.now += 3601
        assert has_data(name, await READERS[name](b))  # 1 h old: still good
        await settle(b)
        clock.now += 3700  # now 2 h 1 min old
        assert not has_data(
            name, await READERS[name](b)
        )  # back to the labelled fallbacks
        await settle(b)


@pytest.mark.asyncio
async def test_the_age_is_measured_from_data_managers_computation_time(monkeypatch):
    monkeypatch.setenv("CIO_REPORT_FETCH_BUDGET_S", "1")
    monkeypatch.setenv("CIO_REPORT_MAX_AGE_SECONDS", "7200")
    old = (datetime.now(UTC) - timedelta(hours=5)).isoformat()
    dm = DataManager({"slippage": {**SLIPPAGE, "metadata": {"calculated_at": old}}})
    b = builder(Clock())
    with patch("httpx.AsyncClient.get", side_effect=dm.get):
        # just fetched, but data-manager computed it 5 h ago: older than the 2 h bound
        assert await b._fetch_slippage(regime(), "cid") is None
        assert b._report_age("slippage") is None  # dropped
    assert dm.calls == 1


@pytest.mark.asyncio
async def test_a_fresh_computation_time_keeps_the_report(monkeypatch):
    monkeypatch.setenv("CIO_REPORT_FETCH_BUDGET_S", "1")
    recent = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    dm = DataManager({"risk_inputs": {**RISK, "as_of": recent}})
    b = builder(Clock())
    with patch("httpx.AsyncClient.get", side_effect=dm.get):
        assert await b._fetch_risk_inputs("cid") is not None
    assert 590 < b._report_age("risk_inputs") < 700


@pytest.mark.asyncio
async def test_the_retry_delay_grows_to_a_cap_and_resets_on_success(monkeypatch):
    monkeypatch.setenv("CIO_REPORT_FETCH_BUDGET_S", "1")
    clock = Clock()
    dm = DataManager()
    dm.fail = httpx.ConnectError("down")
    b = builder(clock)
    delays = []
    with patch("httpx.AsyncClient.get", side_effect=dm.get):
        await b._fetch_slippage(regime(), "cid")
        for _ in range(7):
            delays.append(round(b._slippage_cache[0] - clock.now))
            clock.now = b._slippage_cache[0] + 1
            await b._fetch_slippage(regime(), "cid")
            await settle(b)
        dm.fail = None
        clock.now = b._slippage_cache[0] + 1
        await b._fetch_slippage(regime(), "cid")
        await settle(b)
    assert delays[:6] == [60, 120, 240, 480, 900, 900]
    assert b._report_failures["slippage"] == 0


def test_the_backoff_schedule():
    assert [rf.backoff_seconds(n) for n in (0, 1, 2, 3, 4, 5, 6, 20)] == [
        60.0,
        60.0,
        120.0,
        240.0,
        480.0,
        900.0,
        900.0,
        900.0,
    ]


@pytest.mark.asyncio
async def test_the_refresh_uses_its_own_timeout_not_the_decision_path_one(monkeypatch):
    monkeypatch.setenv("CIO_REPORT_FETCH_BUDGET_S", "1")
    monkeypatch.delenv("CIO_REPORT_FETCH_TIMEOUT_S", raising=False)
    dm = DataManager()
    b = builder(Clock())
    with patch("httpx.AsyncClient.get", side_effect=dm.get):
        await b._fetch_slippage(regime(), "cid")
        await b._fetch_risk_inputs("cid")
        await b._fetch_rounds("s1", "cid")
        assert [k["timeout"] for k in dm.kwargs] == [30.0, 30.0, 30.0]
        monkeypatch.setenv("CIO_REPORT_FETCH_TIMEOUT_S", "75")
        assert rf.report_fetch_timeout_seconds() == 75.0
    for bad in ("0", "-1", "x"):
        monkeypatch.setenv("CIO_REPORT_FETCH_TIMEOUT_S", bad)
        assert rf.report_fetch_timeout_seconds() == 30.0


@pytest.mark.asyncio
async def test_refreshes_are_counted_and_the_age_is_recorded(monkeypatch):
    monkeypatch.setenv("CIO_REPORT_FETCH_BUDGET_S", "1")
    counted, ages = [], []
    monkeypatch.setattr(
        cb_module.REPORT_REFRESH, "add", lambda n, attrs: counted.append(attrs)
    )
    monkeypatch.setattr(
        cb_module.REPORT_AGE, "record", lambda v, attrs: ages.append(attrs["report"])
    )
    clock = Clock()
    dm = DataManager()
    b = builder(clock)
    with patch("httpx.AsyncClient.get", side_effect=dm.get):
        await b._fetch_slippage(regime(), "cid")
        dm.fail = httpx.ConnectError("x")
        clock.now += 3601
        await b._fetch_slippage(regime(), "cid")
        await settle(b)
    assert counted == [
        {"report": "slippage", "outcome": "ok"},
        {"report": "slippage", "outcome": "failed"},
    ]
    assert ages and set(ages) == {"slippage"}


def test_computed_at_is_read_from_the_places_data_manager_puts_it():
    stamp = "2026-10-09T10:00:00+00:00"
    expected = datetime(2026, 10, 9, 10, tzinfo=UTC)
    for body in (
        {"metadata": {"calculated_at": stamp}},
        {"metadata": {"computed_at": stamp}},
        {"metadata": {"timestamp": "2026-10-09T10:00:00"}},
        {"as_of": stamp},
        {"as_of": "2026-10-09T10:00:00Z"},
    ):
        assert rf.computed_at_of(body) == expected
    for body in (None, [], {}, {"metadata": {"calculated_at": "nonsense"}}):
        assert rf.computed_at_of(body) is None


def test_the_age_is_the_larger_of_the_fetch_age_and_the_computation_age():
    now = datetime(2026, 10, 9, 12, tzinfo=UTC)
    computed = now - timedelta(hours=3)
    assert rf.report_age_seconds(since_fetch=60, computed_at=computed, now=now) == 10800
    assert (
        rf.report_age_seconds(since_fetch=20000, computed_at=computed, now=now) == 20000
    )
    assert rf.report_age_seconds(since_fetch=60, computed_at=None, now=now) == 60


# --- calibration: the decision never awaits the fetch --------------------------------------------


class Hung(ConfidenceCalibrationService):
    """A calibration service whose fetch never completes until released."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.release = asyncio.Event()
        self.started = 0

    async def report(self, timeout=None):
        self.started += 1
        await self.release.wait()
        return GOOD


@pytest.mark.asyncio
async def test_a_decision_never_awaits_the_calibration_fetch():
    service = Hung()
    started = time.perf_counter()
    results = await asyncio.wait_for(
        asyncio.gather(*(service.status("s") for _ in range(20))), timeout=0.5
    )
    assert time.perf_counter() - started < 0.2
    assert results == [None] * 20  # the neutral prior while it is not there
    await asyncio.sleep(0)
    assert service.started == 1  # one refresh in flight serves every decision
    service.release.set()
    await service.wait_refresh()
    assert await service.status("s") == {"strategy_id": "s", "calibrated": True}


@pytest.mark.asyncio
async def test_the_orchestrators_calibration_step_does_not_wait_either():
    service = Hung()
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.calibration_service = service
    context = MagicMock()
    context.strategy_id = "s"
    context.trigger_payload = {"confidence": 0.7}
    status = await asyncio.wait_for(orchestrator._calibration_status(context), 0.5)
    assert status is None
    service.release.set()
    await service.wait_refresh()


@pytest.mark.asyncio
async def test_a_hung_calibration_refresh_does_not_hold_a_lock_other_decisions_need():
    service = Hung()
    await service.status("s")  # starts the refresh, which never finishes
    # no lock to wait on: any number of later decisions return at once
    for _ in range(50):
        assert await asyncio.wait_for(service.status("s"), 0.05) is None
    service.release.set()
    await service.wait_refresh()


@pytest.mark.asyncio
async def test_the_calibration_last_good_report_is_bounded_by_its_age(monkeypatch):
    monkeypatch.setenv("CIO_REPORT_MAX_AGE_SECONDS", "7200")
    monkeypatch.setenv("CIO_CALIBRATION_TTL_SECONDS", "60")
    clock = Clock()

    class Service(ConfidenceCalibrationService):
        fail = False

        async def report(self, timeout=None):
            if Service.fail:
                raise httpx.ReadTimeout("")
            return GOOD

    service = Service(clock=clock)
    await service.status("s")
    await service.wait_refresh()
    assert await service.status("s") is not None
    Service.fail = True
    clock.now += 3601
    assert await service.status("s") is not None  # failing, but only 1 h old
    await service.wait_refresh()
    clock.now += 3700
    assert await service.status("s") is None  # older than the bound: the neutral prior


@pytest.mark.asyncio
async def test_the_calibration_age_counts_from_data_managers_computation_time(
    monkeypatch,
):
    monkeypatch.setenv("CIO_REPORT_MAX_AGE_SECONDS", "7200")
    old = (datetime.now(UTC) - timedelta(hours=5)).isoformat()

    class Client:
        async def get(self, url, **kwargs):
            body = {
                "records": [{"strategy_id": "s", "confidence": 0.9, "net_pnl": 1.0}],
                "metadata": {"calculated_at": old},
            }
            return httpx.Response(200, json=body, request=httpx.Request("GET", url))

    service = ConfidenceCalibrationService(
        client=Client(), data_manager_url="http://dm"
    )
    service.warm()
    await service.wait_refresh()
    assert service._good is not None  # fetched
    assert await service.status("s") is None  # but computed 5 h ago: not used


@pytest.mark.asyncio
async def test_calibration_without_a_running_refresh_is_started_by_warm_and_counted(
    monkeypatch,
):
    counted = []
    from cio.core import confidence_calibration as cc

    monkeypatch.setattr(
        cc.REPORT_REFRESH, "add", lambda n, attrs: counted.append(attrs)
    )
    service = Hung()
    service.release.set()
    service.warm()
    await service.wait_refresh()
    assert counted == [{"report": "calibration", "outcome": "ok"}]


def test_a_status_call_without_a_running_loop_starts_nothing():
    service = Hung()
    assert service.cached_report() is None
    assert service._task is None
