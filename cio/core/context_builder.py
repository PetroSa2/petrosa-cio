import asyncio
import logging
import os
from typing import Any, Optional

import httpx

from cio.core.vector import VectorClientProtocol
from cio.models import (
    MarketSignals,
    PnlTrend,
    PortfolioSummary,
    RegimeAPIResponse,
    RegimeResult,
    RiskLimits,
    StrategyDefaults,
    StrategyStats,
    TriggerContext,
    TriggerType,
    VolatilityLevel,
)

logger = logging.getLogger(__name__)

# Categorize triggers into reasoning paths
COLD_TRIGGERS = {
    TriggerType.SCHEDULED_REVIEW,
    TriggerType.PARAMETER_OPTIMIZATION,
    TriggerType.ESCALATION,
}


class ContextBuilder:
    """
    Assembles the complete TriggerContext for a reasoning loop iteration.
    Orchestrates calls to external Petrosa microservices.
    """

    def __init__(
        self,
        data_manager_url: str,
        tradeengine_url: str,
        vector_client: VectorClientProtocol | None = None,
    ):
        self.data_manager_url = data_manager_url
        self.tradeengine_url = tradeengine_url
        self.vector_client = vector_client
        token = os.getenv("PETROSA_INTERNAL_TOKEN", "")
        if not token:
            logger.warning(
                "SECURITY_WARNING: PETROSA_INTERNAL_TOKEN is not set. "
                "All internal HTTP requests from ContextBuilder will be unauthenticated."
            )

        # Increased timeout to 15s to handle cluster latency under load
        self.client = httpx.AsyncClient(
            timeout=15.0,
            headers={
                "X-Petrosa-Issuer": "CIO",
                "X-Petrosa-Internal-Token": token,
            },
        )

    async def build(
        self,
        correlation_id: str,
        source_subject: str,
        trigger_type: TriggerType,
        payload: dict[str, Any],
    ) -> TriggerContext:
        """
        Assembles a full TriggerContext.

        Orchestration Logic:
        1. Fetch Regime, Portfolio/Risk, and Strategy data in parallel.
        2. If trigger is COLD, fetch historical context from Vector DB.
        3. Combine into TriggerContext.
        """
        logger.info(
            "Building trigger context",
            extra={
                "correlation_id": correlation_id,
                "trigger_type": trigger_type.value,
            },
        )

        symbol = payload.get("symbol", "BTCUSDT")
        strategy_id = payload.get("strategy_id", "unknown")

        # 1. Parallelize independent fetches to reduce total latency (max vs sum)
        fetch_tasks = [
            self._fetch_regime(symbol, correlation_id),
            self._fetch_portfolio_and_risk(symbol, correlation_id),
            self._fetch_strategy_data(strategy_id, correlation_id),
        ]

        # 2. Add Vector retrieval if COLD path
        vector_task = None
        if trigger_type in COLD_TRIGGERS and self.vector_client:
            logger.info(
                "COLD trigger detected; adding historical context task",
                extra={"correlation_id": correlation_id, "strategy_id": strategy_id},
            )
            vector_task = self.vector_client.query(strategy_id)
            fetch_tasks.append(vector_task)

        # 3. Synchronize all gathers
        results = await asyncio.gather(*fetch_tasks)

        regime = results[0]
        portfolio, risk, env_stats = results[1]
        stats, defaults = results[2]
        historical_context = results[3] if vector_task else None

        # Assemble TriggerContext
        return TriggerContext(
            correlation_id=correlation_id,
            source_subject=source_subject,
            trigger_type=trigger_type,
            trigger_payload=payload,
            regime=regime,
            volatility_level=regime.volatility_level,
            market_signals=MarketSignals(
                signal_summary=payload.get("signal_summary", "Manual trigger"),
                current_price=payload.get("current_price")
                or payload.get("price")
                or 0.0,
                volatility_percentile=payload.get("volatility_percentile", 0.5),
                trend_strength=payload.get("trend_strength", 0.0),
                price_action_character=payload.get("price_action_character", "Neutral"),
            ),
            strategy_id=strategy_id,
            strategy_stats=stats,
            strategy_defaults=defaults,
            global_drawdown_pct=env_stats.get("global_drawdown_pct", 0.0),
            open_orders_global=env_stats.get("open_orders_global", 0),
            open_orders_symbol=env_stats.get("open_orders_symbol", 0),
            available_capital_usd=env_stats.get("available_capital_usd", 0.0),
            portfolio=portfolio,
            risk_limits=risk,
            historical_context=historical_context,
        )

    async def _fetch_regime(self, symbol: str, correlation_id: str) -> RegimeResult:
        """Fetches and maps regime data from petrosa-data-manager."""
        try:
            url = f"{self.data_manager_url}/analysis/regime?pair={symbol}"
            response = await self.client.get(url)
            response.raise_for_status()

            data = response.json()
            # Defensive check: Data Manager sometimes returns 200 OK with an error message body
            if "message" in data and "No regime data" in data["message"]:
                return RegimeResult(
                    regime="choppy",
                    regime_confidence="low",
                    volatility_level=VolatilityLevel.MEDIUM,
                    primary_signal="data_manager_empty",
                    thought_trace=f"Data Manager reports: {data['message']}",
                )

            api_resp = RegimeAPIResponse.model_validate(data)
            return RegimeResult.from_api_response(api_resp)
        except Exception as e:
            logger.error(
                f"Failed to fetch regime: {e}", extra={"correlation_id": correlation_id}
            )
            # Return safe default
            return RegimeResult(
                regime="choppy",
                regime_confidence="low",
                volatility_level=VolatilityLevel.MEDIUM,
                primary_signal="error",
                thought_trace=f"Error fetching regime: {str(e)}",
            )

    async def _fetch_portfolio_and_risk(
        self, symbol: str, correlation_id: str
    ) -> tuple[PortfolioSummary, RiskLimits, dict[str, Any]]:
        """Fetches portfolio and risk data from tradeengine."""
        try:
            url = f"{self.tradeengine_url}/state?symbol={symbol}"
            response = await self.client.get(url)
            response.raise_for_status()
            data = response.json()

            portfolio = PortfolioSummary(**data["portfolio"])
            risk = RiskLimits(**data["risk_limits"])
            env_stats = data["env_stats"]

            return portfolio, risk, env_stats
        except Exception as e:
            logger.error(
                f"Failed to fetch portfolio/risk: {e}",
                extra={"correlation_id": correlation_id},
            )
            # Safe conservative defaults (trigger blocks)
            return (
                PortfolioSummary(
                    net_directional_exposure=1.0,
                    same_asset_pct=1.0,
                    open_positions_count=999,
                ),
                RiskLimits(
                    max_drawdown_pct=0.0,
                    max_orders_global=0,
                    max_orders_per_symbol=0,
                    max_position_size_usd=0.0,
                ),
                {
                    "global_drawdown_pct": 1.0,
                    "open_orders_global": 999,
                    "available_capital_usd": 0.0,
                },
            )

    async def _fetch_strategy_data(
        self, strategy_id: str, correlation_id: str
    ) -> tuple[StrategyStats, StrategyDefaults]:
        """
        Fetches strategy performance and DNA from the Data Manager.
        Consolidates analytics and configuration into the CIO context.
        """
        # Parallelize strategy-specific fetches
        tasks = [
            self._fetch_strategy_stats(strategy_id, correlation_id),
            self._fetch_strategy_defaults(strategy_id, correlation_id),
        ]
        results = await asyncio.gather(*tasks)
        return results[0], results[1]

    async def _fetch_strategy_stats(
        self, strategy_id: str, correlation_id: str
    ) -> StrategyStats:
        """Fetches historical performance metrics from Data Manager analysis API."""
        try:
            url = f"{self.data_manager_url}/analysis/performance/{strategy_id}"
            response = await self.client.get(url)
            response.raise_for_status()
            data = response.json()
            return StrategyStats(**data["stats"])
        except Exception as e:
            logger.warning(
                f"Failed to fetch strategy stats for {strategy_id}: {e}",
                extra={"correlation_id": correlation_id},
            )
            return StrategyStats(recent_pnl_trend=PnlTrend.NEUTRAL)

    async def _fetch_strategy_defaults(
        self, strategy_id: str, correlation_id: str
    ) -> StrategyDefaults:
        """Fetches strategy DNA (defaults) from Data Manager config API."""
        try:
            url = f"{self.data_manager_url}/api/v1/config/strategies/{strategy_id}"
            response = await self.client.get(url)
            response.raise_for_status()
            data = response.json()

            # Map Data Manager parameters to CIO StrategyDefaults
            params = data.get("parameters", {})
            return StrategyDefaults(
                stop_loss_pct=params.get("stop_loss_pct") or params.get("sl_pct", 0.02),
                take_profit_pct=params.get("take_profit_pct")
                or params.get("tp_pct", 0.04),
                leverage=params.get("leverage", 1.0),
                max_hold_hours=params.get("max_hold_hours", 24.0),
            )
        except Exception as e:
            logger.warning(
                f"Failed to fetch strategy defaults for {strategy_id}: {e}",
                extra={"correlation_id": correlation_id},
            )
            return StrategyDefaults(
                stop_loss_pct=0.01,
                take_profit_pct=0.01,
                leverage=1.0,
                max_hold_hours=1.0,
            )

    async def close(self):
        await self.client.aclose()
