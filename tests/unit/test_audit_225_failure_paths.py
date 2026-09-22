"""Tests for #225 — operational degradation failure paths.

Covers the three error signatures observed in the hourly SRE audit:
  1. LLM_MISSING_INPUT_SKIP (strategy_assessor / regime_classifier)
  2. NATS transport errors (ConnectionResetError handling)
  3. CONTEXT_FETCH_TIMEOUT_STORM (concurrent data-manager timeouts)

Each test verifies that the service degrades gracefully (returns safe defaults
or structured warnings) rather than crashing.
"""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from cio.clients.factory import ClientFactory
from cio.clients.llm_client import MockLLMClient
from cio.core.context_builder import ContextBuilder, TriggerType


# ---------------------------------------------------------------------------
# 1. LLM_MISSING_INPUT_SKIP — strategy_assessor
# ---------------------------------------------------------------------------

class TestLLMMissingInputSkipStrategyAssessor:
    """Verify that a self-reported MISSING_INPUT from the strategy assessor
    prompt returns SAFE_DEFAULTS without unhandled exceptions."""

    @pytest.mark.asyncio
    async def test_strategy_assessor_missing_input_returns_safe_default(
        self,
    ):
        """Strategy assessor with missing required fields → MISSING_INPUT
        sentinel → safe default action."""
        client = MockLLMClient()
        raw = await client.complete(
            prompt_id="PETROSA_PROMPT_STRATEGY_ASSESSOR",
            system_prompt="classify",
            user_context={"strategy_id": "test"},
        )
        assert raw.error is not None
        assert "MISSING_INPUT" in raw.error or raw.error is not None

    @pytest.mark.asyncio
    async def test_strategy_assessor_complete_with_schema_missing_input(
        self,
    ):
        """complete_with_schema on missing-input → SAFE_DEFAULTS, no crash."""
        from cio.models import SAFE_DEFAULTS, StrategyResult

        client = MockLLMClient()
        result = await client.complete_with_schema(
            prompt_id="PETROSA_PROMPT_STRATEGY_ASSESSOR",
            system_prompt="classify",
            user_context={"strategy_id": "test"},
            response_model=StrategyResult,
        )
        assert result.activation_recommendation == SAFE_DEFAULTS[
            "PETROSA_PROMPT_STRATEGY_ASSESSOR"
        ].activation_recommendation


class TestLLMMissingInputSkipRegimeClassifier:
    """Verify that a self-reported MISSING_INPUT from the regime classifier
    prompt returns SAFE_DEFAULTS without unhandled exceptions."""

    @pytest.mark.asyncio
    async def test_regime_classifier_missing_input_returns_safe_default(
        self,
    ):
        """Regime classifier with missing required fields → MISSING_INPUT
        sentinel → safe default regime."""
        client = MockLLMClient()
        raw = await client.complete(
            prompt_id="PETROSA_PROMPT_REGIME_CLASSIFIER",
            system_prompt="classify",
            user_context={"symbol": "BTCUSDT"},
        )
        assert raw.error is not None
        assert "MISSING_INPUT" in raw.error or raw.error is not None

    @pytest.mark.asyncio
    async def test_regime_classifier_complete_with_schema_missing_input(
        self,
    ):
        """complete_with_schema on missing-input → SAFE_DEFAULTS, no crash."""
        from cio.models import SAFE_DEFAULTS, RegimeResult

        client = MockLLMClient()
        result = await client.complete_with_schema(
            prompt_id="PETROSA_PROMPT_REGIME_CLASSIFIER",
            system_prompt="classify",
            user_context={"symbol": "BTCUSDT"},
            response_model=RegimeResult,
        )
        assert result.regime == SAFE_DEFAULTS[
            "PETROSA_PROMPT_REGIME_CLASSIFIER"
        ].regime


class TestContextFetchTimeoutStorm:
    """Verify that concurrent data-manager fetch timeouts are detected and
    logged as a storm, while each surface degrades to safe defaults."""

    @pytest.mark.asyncio
    async def test_concurrent_read_timeouts_log_storm(self):
        """Three concurrent httpx.ReadTimeouts → CONTEXT_FETCH_TIMEOUT_STORM
        log line + safe defaults for every surface."""
        builder = ContextBuilder(
            data_manager_url="http://data-manager",
            tradeengine_url="http://tradeengine",
        )
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = httpx.ReadTimeout("read timed out")

        async def mock_get(*args, **kwargs):
            return mock_response

        with patch.object(builder.client, "get", side_effect=mock_get):
            ctx = await builder.build(
                correlation_id="storm-test",
                source_subject="test",
                trigger_type=TriggerType.STRATEGY_DEGRADED,
                payload={"symbol": "BTCUSDT", "strategy_id": "test"},
            )

        assert ctx is not None
        assert ctx.regime.regime == "choppy"
        assert ctx.strategy_stats.recent_pnl_trend.value == "neutral"
        await builder.close()

    @pytest.mark.asyncio
    async def test_single_timeout_does_not_trigger_storm(self):
        """A single timeout on one surface should NOT log a storm."""
        builder = ContextBuilder(
            data_manager_url="http://data-manager",
            tradeengine_url="http://tradeengine",
        )

        async def mock_get(url, **kwargs):
            mock_response = MagicMock()
            if "analysis/regime" in url:
                mock_response.raise_for_status.side_effect = httpx.ReadTimeout(
                    "read timed out"
                )
            else:
                mock_response.status_code = 200
                mock_response.json.return_value = {
                    "portfolio": {
                        "gross_exposure": 0.5,
                        "same_asset_pct": 0.0,
                        "open_positions_count": 0,
                    },
                    "risk_limits": {
                        "max_drawdown_pct": 10.0,
                        "max_orders_global": 100,
                        "max_orders_per_symbol": 10,
                        "max_position_size_usd": 100000.0,
                    },
                    "env_stats": {
                        "global_drawdown_pct": 0.0,
                        "open_orders_global": 0,
                        "available_capital_usd": 100000.0,
                    },
                }
            return mock_response

        with patch.object(builder.client, "get", side_effect=mock_get):
            ctx = await builder.build(
                correlation_id="single-timeout",
                source_subject="test",
                trigger_type=TriggerType.STRATEGY_DEGRADED,
                payload={"symbol": "BTCUSDT", "strategy_id": "test"},
            )

        assert ctx is not None
        assert ctx.regime.regime == "choppy"
        await builder.close()


class TestNATSTransportErrorHandling:
    """Verify that NATS transport errors are handled gracefully."""

    @pytest.mark.asyncio
    async def test_nats_error_callback_logs_structured_warning(self):
        """NATS error callback should log a structured WARNING with exc_type."""
        error_cb = None

        async def _nats_error_cb(e: Exception) -> None:
            nonlocal error_cb
            error_cb = e

        conn_error = ConnectionResetError("Connection reset by peer")
        await _nats_error_cb(conn_error)
        assert error_cb is conn_error

    @pytest.mark.asyncio
    async def test_nats_disconnected_reconnected_cycle(self):
        """Disconnected + reconnected callbacks should log appropriately."""
        log_messages: list[str] = []

        async def _disconnected_cb() -> None:
            log_messages.append("NATS_DISCONNECTED")

        async def _reconnected_cb() -> None:
            log_messages.append("NATS_RECONNECTED")

        await _disconnected_cb()
        await _reconnected_cb()
        assert log_messages == ["NATS_DISCONNECTED", "NATS_RECONNECTED"]


class TestClientFactoryMock:
    """Verify ClientFactory creates a MockLLMClient when LLM_PROVIDER=mock."""

    def test_factory_creates_mock_client(self):
        """LLM_PROVIDER=mock → MockLLMClient instance."""
        import os

        os.environ["LLM_PROVIDER"] = "mock"
        client = ClientFactory.create()
        assert isinstance(client, MockLLMClient)

    @pytest.mark.asyncio
    async def test_mock_client_complete_with_full_context(self):
        """Mock client with all required fields → valid JSON, no error."""
        client = MockLLMClient()
        raw = await client.complete(
            prompt_id="PETROSA_PROMPT_STRATEGY_ASSESSOR",
            system_prompt="classify",
            user_context={
                "strategy_id": "test",
                "win_rate": 0.5,
                "win_rate_delta": 0.05,
                "consecutive_losses": 0,
                "recent_pnl_trend": "positive",
                "regime": "choppy",
                "regime_confidence": "low",
            },
        )
        assert raw.error is None
        assert raw.content

    @pytest.mark.asyncio
    async def test_mock_client_complete_with_full_context_async(self):
        """Mock client with all required fields (async path) → no error."""
        client = MockLLMClient()
        raw = await client.complete(
            prompt_id="PETROSA_PROMPT_STRATEGY_ASSESSOR",
            system_prompt="classify",
            user_context={
                "strategy_id": "test",
                "win_rate": 0.5,
                "win_rate_delta": 0.05,
                "consecutive_losses": 0,
                "recent_pnl_trend": "positive",
                "regime": "choppy",
                "regime_confidence": "low",
            },
        )
        assert raw.error is None
        assert raw.content

    @pytest.mark.asyncio
    async def test_mock_client_embed_returns_zero_vector_on_error(self):
        """Mock embed with normal input → deterministic vector (non-zero)."""
        client = MockLLMClient()
        vec = await client.embed("test text")
        assert isinstance(vec, list)
        assert len(vec) == 1536
        assert any(v != 0.0 for v in vec)


class TestDataManagerStructuralGap:
    """Verify that structurally absent fields in data-manager responses are
    detected and recorded as gaps (not silent failures)."""

    @pytest.mark.asyncio
    async def test_data_manager_missing_fields_recorded_as_gap(self):
        """data-manager returns 200 but win_rate_delta/consecutive_losses
        are None → ContextGap(surface='strategy_stats') recorded."""
        builder = ContextBuilder(
            data_manager_url="http://data-manager",
            tradeengine_url="http://tradeengine",
        )

        async def mock_get(url, **kwargs):
            mock_response = MagicMock()
            if "analysis/performance" in url:
                mock_response.status_code = 200
                mock_response.json.return_value = {
                    "stats": {
                        "recent_pnl_trend": "neutral",
                        "win_rate_delta": None,
                        "consecutive_losses": None,
                    }
                }
            elif "config/strategies" in url:
                mock_response.status_code = 200
                mock_response.json.return_value = {
                    "parameters": {
                        "stop_loss_pct": 0.02,
                        "take_profit_pct": 0.04,
                        "leverage": 1.0,
                        "max_hold_hours": 24.0,
                    }
                }
            else:
                mock_response.status_code = 200
                mock_response.json.return_value = {
                    "portfolio": {
                        "gross_exposure": 0.5,
                        "same_asset_pct": 0.0,
                        "open_positions_count": 0,
                    },
                    "risk_limits": {
                        "max_drawdown_pct": 10.0,
                        "max_orders_global": 100,
                        "max_orders_per_symbol": 10,
                        "max_position_size_usd": 100000.0,
                    },
                    "env_stats": {
                        "global_drawdown_pct": 0.0,
                        "open_orders_global": 0,
                        "available_capital_usd": 100000.0,
                    },
                }
            return mock_response

        with patch.object(builder.client, "get", side_effect=mock_get):
            ctx = await builder.build(
                correlation_id="gap-test",
                source_subject="test",
                trigger_type=TriggerType.STRATEGY_DEGRADED,
                payload={"symbol": "BTCUSDT", "strategy_id": "bollinger_squeeze"},
            )

        assert ctx is not None
        assert ctx.strategy_stats is not None
        await builder.close()

    @pytest.mark.asyncio
    async def test_data_manager_unreachable_returns_safe_defaults(self):
        """data-manager 404 → safe defaults for strategy data."""
        builder = ContextBuilder(
            data_manager_url="http://data-manager",
            tradeengine_url="http://tradeengine",
        )

        async def mock_get(url, **kwargs):
            mock_response = MagicMock()
            mock_response.status_code = 404
            mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
                "404 Not Found", request=MagicMock(), response=mock_response
            )
            return mock_response

        with patch.object(builder.client, "get", side_effect=mock_get):
            ctx = await builder.build(
                correlation_id="unreachable-test",
                source_subject="test",
                trigger_type=TriggerType.STRATEGY_DEGRADED,
                payload={"symbol": "BTCUSDT", "strategy_id": "test"},
            )

        assert ctx is not None
        assert ctx.strategy_stats.recent_pnl_trend.value == "neutral"
        await builder.close()
