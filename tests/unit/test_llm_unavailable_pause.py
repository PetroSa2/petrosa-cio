from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from cio.clients.llm_client import CIO_LLM_Client
from cio.core.orchestrator import Orchestrator
from cio.models import (
    SAFE_DEFAULTS,
    ActionType,
    ConfidenceLevel,
    MarketSignals,
    PnlTrend,
    PortfolioSummary,
    RawLLMResponse,
    RegimeEnum,
    RegimeFit,
    RegimeResult,
    RiskLimits,
    StrategyDefaults,
    StrategyStats,
    TriggerContext,
    TriggerType,
    VolatilityLevel,
)
from cio.models.decision import is_safe_default
from cio.models.enums import RejectionSource
from cio.personas.action_classifier import PROMPT_ID as ACTION_PROMPT_ID
from cio.personas.regime_analyst import PROMPT_ID as REGIME_PROMPT_ID
from cio.personas.strategy_assessor import PROMPT_ID as STRATEGY_PROMPT_ID


def _make_context() -> TriggerContext:
    return TriggerContext(
        correlation_id="test-persona-concurrency",
        trigger_type=TriggerType.TRADE_INTENT,
        strategy_id="test_strat",
        symbol="BTCUSDT",
        source_subject="cio.intent.trading.test_strat",
        trigger_payload={"symbol": "BTCUSDT"},
        regime=RegimeResult(
            regime=RegimeEnum.RANGING,
            regime_confidence=ConfidenceLevel.HIGH,
            volatility_level=VolatilityLevel.MEDIUM,
            primary_signal="test",
            confidence=1.0,
            fit=RegimeFit.GOOD,
            thought_trace="test",
        ),
        volatility_level=VolatilityLevel.MEDIUM,
        market_signals=MarketSignals(
            signal_summary="bullish",
            current_price=50000.0,
            volatility_percentile=0.5,
            trend_strength=0.7,
            price_action_character="stable",
        ),
        strategy_stats=StrategyStats(recent_pnl_trend=PnlTrend.NEUTRAL),
        strategy_defaults=StrategyDefaults(
            stop_loss_pct=0.02, take_profit_pct=0.04, max_hold_hours=24
        ),
        global_drawdown_pct=0.0,
        open_orders_global=0,
        open_orders_symbol=0,
        available_capital_usd=1000.0,
        portfolio=PortfolioSummary(
            gross_exposure=0.0, same_asset_pct=0.0, open_positions_count=0
        ),
        risk_limits=RiskLimits(
            max_drawdown_pct=0.1,
            max_orders_global=50,
            max_orders_per_symbol=5,
            max_position_size_usd=1000.0,
        ),
    )


def _mock_cache(regime_json: str | None, strategy_json: str | None):
    cache = AsyncMock()

    async def _get(key: str):
        if key.startswith("regime:"):
            return regime_json
        if key.startswith("strategy:"):
            return strategy_json
        return None

    cache.get = AsyncMock(side_effect=_get)
    cache.set = AsyncMock()
    cache.delete = AsyncMock()
    return cache


def _raw(prompt_id: str, *, content: str = "", error: str | None = None):
    return RawLLMResponse(
        prompt_id=prompt_id,
        content=content,
        error=error,
        model="test-model",
        input_tokens=1,
        output_tokens=1,
        latency_ms=1,
        timestamp=datetime.now(UTC),
    )


class ScriptedClient(CIO_LLM_Client):
    def __init__(
        self,
        failures: dict[str, str] | None = None,
        action: str = "execute",
    ):
        super().__init__()
        self.failures = failures or {}
        self.action = action
        self.calls: list[str] = []

    async def complete(self, prompt_id, system_prompt, user_context):
        self.calls.append(prompt_id)
        failure = self.failures.get(prompt_id)
        if failure == "transport" or failure == "circuit_breaker":
            return _raw(
                prompt_id,
                error="CIRCUIT_BREAKER_OPEN"
                if failure == "circuit_breaker"
                else "upstream_timeout",
            )
        if failure == "missing_input":
            return _raw(prompt_id, content='{"error": "MISSING_INPUT"}')
        if failure == "validation":
            return _raw(prompt_id, content="not json at all")
        if prompt_id == REGIME_PROMPT_ID:
            return _raw(
                prompt_id,
                content=json.dumps(
                    {
                        "regime": "ranging",
                        "regime_confidence": "high",
                        "volatility_level": "medium",
                        "primary_signal": "ok",
                        "thought_trace": "ok",
                    }
                ),
            )
        if prompt_id == STRATEGY_PROMPT_ID:
            return _raw(
                prompt_id,
                content=json.dumps(
                    {
                        "health": "healthy",
                        "regime_fit": "good",
                        "activation_recommendation": "run",
                        "thought_trace": "ok",
                    }
                ),
            )
        return _raw(
            prompt_id,
            content=json.dumps(
                {
                    "action": self.action,
                    "justification": "ok",
                    "thought_trace": "ok",
                }
            ),
        )

    async def _schema_fallback(self, prompt_id, system_prompt, user_context):
        return _raw(prompt_id, error="fallback_unavailable")

    async def embed(self, text: str) -> list[float]:
        return []

    async def get_cached(self, cache_key: str) -> str | None:
        return None

    async def put_cached(self, cache_key: str, value: str, ttl: int = 900) -> None:
        return None


@pytest.mark.parametrize(
    "failure", ["transport", "circuit_breaker", "missing_input", "validation"]
)
@pytest.mark.asyncio
async def test_classifier_outage_pauses_strategy(failure):
    client = ScriptedClient({ACTION_PROMPT_ID: failure})
    orchestrator = Orchestrator(client, _mock_cache(None, None))

    decision = await orchestrator.run(_make_context())

    assert decision.action == ActionType.PAUSE_STRATEGY
    assert decision.rejection_source == RejectionSource.LLM_UNAVAILABLE
    assert decision.thought_trace == "LLM_UNAVAILABLE"
    assert decision.computed_position_size_usd == 0.0
    assert decision.hard_blocked is False
    assert decision.justification.startswith(
        "LLM_UNAVAILABLE: PETROSA_PROMPT_ACTION_CLASSIFIER fell back to SAFE_DEFAULTS"
    )


@pytest.mark.asyncio
async def test_upstream_outage_pauses_without_classifier_call():
    client = ScriptedClient({REGIME_PROMPT_ID: "transport"})
    orchestrator = Orchestrator(client, _mock_cache(None, None))

    decision = await orchestrator.run(_make_context())

    assert decision.action == ActionType.PAUSE_STRATEGY
    assert REGIME_PROMPT_ID in decision.justification
    assert ACTION_PROMPT_ID not in client.calls


@pytest.mark.asyncio
async def test_safe_default_results_are_not_cached():
    client = ScriptedClient({STRATEGY_PROMPT_ID: "transport"})
    cache = _mock_cache(None, None)
    orchestrator = Orchestrator(client, cache)

    await orchestrator.run(_make_context())

    keys = [call.args[0] for call in cache.set.call_args_list]
    assert any(key.startswith("regime:") for key in keys)
    assert not any(key.startswith("strategy:") for key in keys)


@pytest.mark.asyncio
async def test_legitimate_classifier_skip_is_not_converted():
    client = ScriptedClient(action="skip")
    orchestrator = Orchestrator(client, _mock_cache(None, None))
    decision = await orchestrator.run(_make_context())

    assert decision.action == ActionType.SKIP
    assert decision.rejection_source is None
    assert decision.thought_trace != "LLM_UNAVAILABLE"


def test_is_safe_default_is_identity_not_equality():
    fallback = SAFE_DEFAULTS[ACTION_PROMPT_ID]
    assert is_safe_default(ACTION_PROMPT_ID, fallback)
    assert not is_safe_default(ACTION_PROMPT_ID, fallback.model_copy())
    assert not is_safe_default("unknown", fallback)
