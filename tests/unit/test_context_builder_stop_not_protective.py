"""petrosa-cio#395: tradeengine's 422 stop_not_protective is a bad signal, not a portfolio outage."""

import logging
from unittest.mock import AsyncMock

import httpx
import pytest

from cio.core.context_builder import SIGNAL_STOP_SURFACE, ContextBuilder
from cio.models import ContextGap


def _builder() -> ContextBuilder:
    return ContextBuilder(data_manager_url="http://dm", tradeengine_url="http://te")


def _status_error(status: int, body) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "http://te/state?symbol=ADAUSDT")
    response = httpx.Response(status, json=body, request=request)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


_STOP_NOT_PROTECTIVE = {
    "detail": {
        "error": "stop_not_protective",
        "message": "signal stop is not on the protective side",
        "side": "LONG",
        "stop_loss": 0.18208,
        "reference_price": 0.1819,
    }
}


async def _fetch(builder, gaps, availability):
    return await builder._fetch_portfolio_and_risk(
        "ADAUSDT",
        "corr-1",
        side="LONG",
        stop_loss=0.18208,
        reference_price=0.1819,
        strategy_id="mean_reversion_scalper",
        gaps=gaps,
        availability=availability,
    )


@pytest.mark.asyncio
async def test_stop_not_protective_is_a_signal_gap_not_a_portfolio_outage(caplog):
    builder = _builder()
    builder.client.get = AsyncMock(side_effect=_status_error(422, _STOP_NOT_PROTECTIVE))
    gaps: list[ContextGap] = []
    availability = {"portfolio": True}

    with caplog.at_level(logging.WARNING):
        portfolio, risk, _env = await _fetch(builder, gaps, availability)

    assert availability["portfolio"] is True
    assert [g.surface for g in gaps] == [SIGNAL_STOP_SURFACE]
    assert gaps[0].reason.startswith("stop_not_protective: side=LONG stop_loss=0.18208")
    assert "SIGNAL_STOP_NOT_PROTECTIVE" in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    # The fail-safe values still block if anything reads them.
    assert portfolio.gross_exposure == 1.0 and risk.max_orders_global == 0
    assert builder.client.get.await_count == 1  # not retried


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        _status_error(500, {"detail": "Failed to retrieve engine state: boom"}),
        _status_error(422, {"detail": {"error": "validation_failed"}}),
        _status_error(422, {"detail": [{"loc": ["query", "side"], "msg": "bad"}]}),
        httpx.ReadTimeout("slow"),
    ],
)
async def test_other_failures_stay_portfolio_outages(error, caplog):
    builder = _builder()
    builder.client.get = AsyncMock(side_effect=error)
    gaps: list[ContextGap] = []
    availability = {"portfolio": True}

    with caplog.at_level(logging.WARNING):
        await _fetch(builder, gaps, availability)

    assert availability["portfolio"] is False
    assert [g.surface for g in gaps] == ["portfolio"]
    assert "Failed to fetch portfolio/risk" in caplog.text
    assert "SIGNAL_STOP_NOT_PROTECTIVE" not in caplog.text
