"""win_rate_delta is optional on the CIO side (petrosa-data-manager#579, CIO side).

data-manager returns the delta only when it is distinguishable from noise, so an established strategy with no
significant change in win rate has ``win_rate_delta: null`` and its ``win_rate_delta_window`` present. That is
"no shift", never "no history": it must reach the LLM, keep its win rate in the EV and never be MISSING_INPUT.
"""

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from test_engine import build_test_context

from cio.clients.llm_client import MockLLMClient
from cio.core.context_builder import ContextBuilder, classify_strategy_history
from cio.core.engine import CodeEngine
from cio.models import SAFE_DEFAULTS, ContextGap, PnlTrend, StrategyStats
from cio.models.enums import ActivationRecommendation, HealthStatus
from cio.personas.strategy_assessor import COLD_START_TRACE, StrategyAssessor

CALC = {"source": "data-manager-pnl-calculator", "fills_replayed": 600}


def _stats(**overrides) -> dict:
    stats = {
        "win_rate": 0.5,
        "win_rate_delta": None,
        "win_rate_delta_window": 150,
        "win_rate_delta_se": 0.058,
        "consecutive_losses": 5,
        "recent_pnl_trend": "neutral",
        "realized_pnl": 12.0,
        "unrealized_pnl": 0.0,
    }
    stats.update(overrides)
    return stats


def _raw(stats: dict, metadata: dict | None = CALC) -> dict:
    raw: dict = {"stats": stats}
    if metadata is not None:
        raw["metadata"] = metadata
    return raw


@pytest.mark.parametrize(
    ("stats", "expected"),
    [
        # established strategy, no significant change: computed
        (_stats(), "computed"),
        # a real shift: computed
        (_stats(win_rate_delta=-0.3), "computed"),
        # an older data-manager (no window key) that has a delta: computed
        (
            {
                "win_rate": 0.5,
                "win_rate_delta": 0.1,
                "consecutive_losses": 1,
            },
            "computed",
        ),
        # an older data-manager with a null delta: too few closed trades, as before
        (
            {"win_rate": 1.0, "win_rate_delta": None, "consecutive_losses": 0},
            "insufficient_history",
        ),
        # no closed trades at all (the fe22cb1 behaviour)
        (
            {
                "win_rate": None,
                "win_rate_delta": None,
                "win_rate_delta_window": None,
                "win_rate_delta_se": None,
                "consecutive_losses": None,
            },
            "insufficient_history",
        ),
        # one closed trade: win rate known, no window yet
        (
            _stats(win_rate_delta_window=None, win_rate_delta_se=None),
            "insufficient_history",
        ),
        # a window but no losses figure: not computed
        (_stats(consecutive_losses=None), "insufficient_history"),
    ],
)
def test_classification_with_an_optional_delta(stats, expected):
    assert classify_strategy_history(_raw(stats)) == expected


def test_classification_without_the_calculator_source_stays_unavailable():
    no_db = {"source": "data-manager-analysis-no-db", "fills_replayed": 0}
    assert (
        classify_strategy_history(_raw(_stats(win_rate=None), no_db)) == "unavailable"
    )


@pytest.mark.asyncio
async def test_fetch_keeps_the_window_and_records_no_gap_for_a_null_delta(caplog):
    caplog.set_level(logging.WARNING, logger="cio.core.context_builder")
    builder = ContextBuilder(data_manager_url="http://dm", tradeengine_url="http://te")
    builder.client.get = AsyncMock(
        return_value=MagicMock(
            status_code=200,
            raise_for_status=lambda: None,
            json=lambda: _raw(_stats()),
        )
    )
    gaps: list[ContextGap] = []
    result = await builder._fetch_strategy_stats("established", "cid", gaps=gaps)
    await builder.close()

    assert result.history_status == "computed"
    assert result.win_rate == 0.5
    assert result.win_rate_delta is None
    assert result.win_rate_delta_window == 150
    assert result.win_rate_delta_se == pytest.approx(0.058)
    assert not [gap for gap in gaps if gap.surface == "strategy_stats"]
    assert not any(
        "STRATEGY_STATS_STRUCTURAL_GAP" in record.message for record in caplog.records
    )


@pytest.mark.asyncio
async def test_old_data_manager_response_without_the_new_keys_still_works():
    builder = ContextBuilder(data_manager_url="http://dm", tradeengine_url="http://te")
    builder.client.get = AsyncMock(
        return_value=MagicMock(
            status_code=200,
            raise_for_status=lambda: None,
            json=lambda: _raw(
                {
                    "win_rate": 0.6,
                    "win_rate_delta": 0.05,
                    "consecutive_losses": 1,
                    "recent_pnl_trend": "positive",
                }
            ),
        )
    )
    result = await builder._fetch_strategy_stats("old-dm", "cid", gaps=[])
    await builder.close()

    assert result.history_status == "computed"
    assert result.win_rate_delta == 0.05
    assert result.win_rate_delta_window is None
    assert result.win_rate_delta_se is None


def _established_context(**overrides):
    context = build_test_context(win_rate=0.5, portfolio_state_available=True)
    fields = {
        "win_rate": 0.5,
        "win_rate_delta": None,
        "win_rate_delta_window": 150,
        "win_rate_delta_se": 0.058,
        "consecutive_losses": 5,
        "recent_pnl_trend": PnlTrend.NEUTRAL,
        "history_status": "computed",
    }
    fields.update(overrides)
    context.strategy_stats = StrategyStats(**fields)
    return context


def _assessor(result=None):
    client = MagicMock()
    client.capability_profile = "standard"
    client.complete_with_schema = AsyncMock(
        return_value=result or SAFE_DEFAULTS["PETROSA_PROMPT_STRATEGY_ASSESSOR"]
    )
    return StrategyAssessor(client), client


@pytest.mark.asyncio
async def test_established_strategy_with_a_null_delta_and_losses_reaches_the_llm(
    caplog,
):
    """300 trades, 50 % win rate, 5 losses in a row: not cold start, the losing-streak check can run."""
    assessor, client = _assessor()
    context = _established_context()
    caplog.set_level(logging.WARNING, logger="cio.personas.strategy_assessor")

    result = await assessor.assess(context)

    assert client.complete_with_schema.await_count == 1
    assert result.thought_trace != COLD_START_TRACE
    sent = client.complete_with_schema.await_args.kwargs["user_context"]
    assert sent["win_rate"] == 0.5
    assert sent["consecutive_losses"] == 5
    assert sent["win_rate_delta"] is None
    assert sent["win_rate_delta_window"] == 150
    assert sent["win_rate_delta_se"] == pytest.approx(0.058)
    # a null delta with its window is not a missing input: no warning, no gap
    assert not any(
        "STRATEGY_ASSESSOR_MISSING_INPUT_FIELDS" in record.message
        for record in caplog.records
    )
    assert not [
        gap
        for gap in context.pre_decision_context.gaps
        if gap.surface == "strategy_stats"
    ]


@pytest.mark.asyncio
async def test_a_null_delta_without_a_window_is_still_reported_missing(caplog):
    assessor, client = _assessor()
    context = _established_context(win_rate_delta_window=None, win_rate_delta_se=None)
    caplog.set_level(logging.WARNING, logger="cio.personas.strategy_assessor")

    await assessor.assess(context)

    assert any(
        "STRATEGY_ASSESSOR_MISSING_INPUT_FIELDS" in record.message
        and "win_rate_delta" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_no_closed_trades_is_still_cold_start():
    assessor, client = _assessor()
    context = _established_context(
        win_rate=None,
        win_rate_delta_window=None,
        win_rate_delta_se=None,
        consecutive_losses=None,
        history_status="insufficient_history",
    )

    result = await assessor.assess(context)

    assert client.complete_with_schema.await_count == 0
    assert result.health == HealthStatus.HEALTHY
    assert result.activation_recommendation == ActivationRecommendation.RUN
    assert result.thought_trace == COLD_START_TRACE


def test_engine_keeps_the_win_rate_of_an_established_strategy_with_a_null_delta():
    context = _established_context()
    result = CodeEngine.run(context)
    assert result.ev_unavailable is False
    assert result.gross_ev is not None


@pytest.mark.asyncio
async def test_mock_llm_treats_a_null_delta_as_no_shift():
    client = MockLLMClient()
    base = {
        "strategy_id": "s",
        "win_rate": 0.5,
        "win_rate_delta": None,
        "win_rate_delta_window": 150,
        "win_rate_delta_se": 0.058,
        "recent_pnl_trend": "neutral",
        "regime": "ranging",
        "regime_confidence": "medium",
    }
    streak = await client.complete(
        prompt_id="PETROSA_PROMPT_STRATEGY_ASSESSOR",
        system_prompt="x",
        user_context={**base, "consecutive_losses": 5},
    )
    calm = await client.complete(
        prompt_id="PETROSA_PROMPT_STRATEGY_ASSESSOR",
        system_prompt="x",
        user_context={**base, "consecutive_losses": 1},
    )
    assert streak.error is None and calm.error is None
    assert (
        '"failing"' in streak.content and '"pause"' in streak.content
    )  # the streak rule runs
    assert (
        '"healthy"' in calm.content and '"run"' in calm.content
    )  # null delta is no shift


def test_the_prompt_makes_the_delta_optional():
    import os

    import yaml

    path = os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "cio",
        "prompts",
        "strategy_assessor_v1.yaml",
    )
    data = yaml.safe_load(open(path))
    assert "win_rate_delta" not in data["required_context_fields"]
    assert "win_rate_delta_window" in data["system_prompt"]
    assert "never as missing input" in data["system_prompt"]
    assert "win_rate_delta_window" in data["system_prompt_minimal"]
