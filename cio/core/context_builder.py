import asyncio
import logging
import os
from datetime import datetime
from typing import Any

try:
    from datetime import UTC
except ImportError:  # pragma: no cover — py310 compatibility
    from datetime import timezone

    UTC = timezone.utc  # noqa: UP017

import httpx

from cio.core.vector import VectorClientProtocol
from cio.models import (
    CharacterizationRef,
    ContextGap,
    EvaluatorVerdict,
    MarketSignals,
    MarketState,
    PnlTrend,
    PortfolioState,
    PortfolioSummary,
    PreDecisionContext,
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
        evaluator_subscriber: Any | None = None,
    ):
        self.data_manager_url = data_manager_url
        self.tradeengine_url = tradeengine_url
        self.vector_client = vector_client
        # P1.4-AC1 (#131): wired in by main.py at startup so the
        # PreDecisionContext bundle can read live evaluator verdicts
        # without coupling to NATS in this layer. ``None`` is the legacy
        # path — the bundle is then assembled with an empty verdicts
        # dict and downstream stories (122.2) handle the fallback.
        self.evaluator_subscriber = evaluator_subscriber
        token = os.getenv("PETROSA_INTERNAL_TOKEN", "")
        if not token:
            logger.warning(
                "SECURITY_WARNING: PETROSA_INTERNAL_TOKEN is not set. "
                "All internal HTTP requests from ContextBuilder will be unauthenticated."
            )

        # Increased timeout to 30s to handle cluster latency under load
        self.client = httpx.AsyncClient(
            timeout=30.0,
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
        decision_id: str | None = None,
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
        # P1.4-AC2 (#132): per-build gap collector — passed to fetches so they
        # can record "this surface fell back to safe defaults" events without
        # changing their existing return types (tests still mock _fetch_*
        # directly). Same per-build availability map keys are read by
        # assemble_pre_decision_context to set the *_available flags.
        gaps: list[ContextGap] = []
        availability: dict[str, bool] = {
            "market": True,
            "portfolio": True,
            "evaluators": True,
            "characterization": True,
        }
        fetch_tasks = [
            self._fetch_regime(
                symbol, correlation_id, gaps=gaps, availability=availability
            ),
            self._fetch_portfolio_and_risk(
                symbol, correlation_id, gaps=gaps, availability=availability
            ),
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
        # Pass decision_id only when provided; TriggerContext.default_factory generates one otherwise
        extra = {"decision_id": decision_id} if decision_id is not None else {}

        market_signals = MarketSignals(
            signal_summary=payload.get("signal_summary", "Manual trigger"),
            current_price=payload.get("current_price") or payload.get("price") or 0.0,
            volatility_percentile=payload.get("volatility_percentile", 0.5),
            trend_strength=payload.get("trend_strength", 0.0),
            price_action_character=payload.get("price_action_character", "Neutral"),
        )

        # P1.4-AC1 (#131) — assemble the structured PreDecisionContext
        # bundle from the components already fetched above plus the
        # evaluator-subscriber snapshot + a characterization-ref fetch.
        # P1.4-AC2 (#132) — pass the accumulated gap collector + availability
        # map so the bundle carries per-surface flags and an audit-trail-ready
        # gaps list.
        strategy_revision_id = payload.get("strategy_revision_id")
        pre_decision_context = await self.assemble_pre_decision_context(
            correlation_id=correlation_id,
            regime=regime,
            market_signals=market_signals,
            portfolio=portfolio,
            env_stats=env_stats,
            strategy_id=strategy_id,
            strategy_revision_id=strategy_revision_id,
            gaps=gaps,
            availability=availability,
        )

        return TriggerContext(
            correlation_id=correlation_id,
            source_subject=source_subject,
            **extra,
            trigger_type=trigger_type,
            trigger_payload=payload,
            regime=regime,
            volatility_level=regime.volatility_level,
            market_signals=market_signals,
            strategy_id=strategy_id,
            strategy_revision_id=strategy_revision_id,
            strategy_stats=stats,
            strategy_defaults=defaults,
            global_drawdown_pct=env_stats.get("global_drawdown_pct", 0.0),
            open_orders_global=env_stats.get("open_orders_global", 0),
            open_orders_symbol=env_stats.get("open_orders_symbol", 0),
            available_capital_usd=env_stats.get("available_capital_usd", 0.0),
            portfolio=portfolio,
            risk_limits=risk,
            historical_context=historical_context,
            pre_decision_context=pre_decision_context,
        )

    async def assemble_pre_decision_context(
        self,
        *,
        correlation_id: str,
        regime: RegimeResult,
        market_signals: MarketSignals,
        portfolio: PortfolioSummary,
        env_stats: dict[str, Any],
        strategy_id: str,
        strategy_revision_id: str | None,
        gaps: list[ContextGap] | None = None,
        availability: dict[str, bool] | None = None,
    ) -> PreDecisionContext:
        """P1.4-AC1 / FR55-FR58 (#131) — assemble the typed PreDecisionContext.

        Reuses subsystem fetches already issued during ``build()`` so the
        bundle does not lengthen the cold path; the only extra call is the
        characterization-ref probe, which is bounded by a short timeout
        and degrades to ``characterization=None`` on any failure (the
        stale-gate at orchestrator.py still owns refusal semantics; this
        method only *observes* what is on record).

        P1.4-AC2 (#132) — when ``gaps``/``availability`` are provided by
        ``build()`` they carry the per-surface state captured during the
        upstream fetches; this method only ADDS to them (it does not reset
        them). When called directly by tests/orchestration on the
        already-fetched path, the caller may supply ``availability=None``
        to keep all surfaces flagged ``True`` and only record evaluator/
        characterization gaps detected here.
        """
        market_state = MarketState(
            regime=regime.regime,
            regime_confidence=regime.regime_confidence,
            volatility_level=regime.volatility_level,
            current_price=market_signals.current_price,
            primary_signal=regime.primary_signal,
        )
        portfolio_state = PortfolioState(
            gross_exposure=portfolio.gross_exposure,
            same_asset_pct=portfolio.same_asset_pct,
            open_positions_count=portfolio.open_positions_count,
            global_drawdown_pct=env_stats.get("global_drawdown_pct", 0.0),
            available_capital_usd=env_stats.get("available_capital_usd", 0.0),
            open_orders_global=env_stats.get("open_orders_global", 0),
            open_orders_symbol=env_stats.get("open_orders_symbol", 0),
        )

        local_gaps: list[ContextGap] = gaps if gaps is not None else []
        evaluator_verdicts = self._collect_evaluator_verdicts(gaps=local_gaps)
        characterization = await self._fetch_characterization_ref(
            strategy_id=strategy_id,
            strategy_revision_id=strategy_revision_id,
            correlation_id=correlation_id,
            gaps=local_gaps,
        )

        # AC2.a — flag synthesis: when the caller did not pass an availability
        # map, default each surface to True and only flip on evidence of a gap
        # surfaced from this method's local fetches (subscriber missing,
        # characterization 404).
        avail = (
            dict(availability)
            if availability is not None
            else {
                "market": True,
                "portfolio": True,
                "evaluators": True,
                "characterization": True,
            }
        )

        # Evaluators are unavailable when no subscriber is wired OR snapshot
        # raised (the gap collector captured the reason). Empty verdicts with
        # a wired subscriber is *not* a gap — that's the steady-state "no
        # subsystem reported yet".
        if self.evaluator_subscriber is None or any(
            g.surface == "evaluators" for g in local_gaps
        ):
            avail["evaluators"] = False

        # Characterization is unavailable only when the caller supplied a
        # revision id AND the fetch did not surface a ref. The legacy
        # "no revision id" path keeps available=True with characterization=None,
        # which mirrors AC1's contract.
        if strategy_revision_id and characterization is None:
            avail["characterization"] = False

        return PreDecisionContext(
            market_state=market_state,
            portfolio_state=portfolio_state,
            evaluator_verdicts=evaluator_verdicts,
            characterization=characterization,
            market_state_available=avail.get("market", True),
            portfolio_state_available=avail.get("portfolio", True),
            evaluator_verdicts_available=avail.get("evaluators", True),
            characterization_available=avail.get("characterization", True),
            gaps=list(local_gaps),
        )

    def _collect_evaluator_verdicts(
        self, gaps: list[ContextGap] | None = None
    ) -> dict[str, EvaluatorVerdict]:
        """FR57 — project the evaluator subscriber's snapshot into a typed dict.

        Tolerates the legacy "no subscriber wired" case by returning an
        empty dict. The subscriber's snapshot shape is documented at
        ``EvaluatorSubscriber.snapshot``.

        P1.4-AC2 (#132): when the subscriber is missing or ``snapshot()``
        raises, append a ``ContextGap(surface='evaluators')`` to ``gaps`` so
        the bundle's availability flag is flipped and the FR12 audit-trail
        consumer can persist the event.
        """
        sub = self.evaluator_subscriber
        if sub is None:
            if gaps is not None:
                gaps.append(
                    ContextGap(
                        surface="evaluators",
                        reason="subscriber_not_wired",
                    )
                )
            return {}
        try:
            snap = sub.snapshot()
        except Exception as exc:  # noqa: BLE001 — degrade rather than crash assembly
            if gaps is not None:
                gaps.append(
                    ContextGap(
                        surface="evaluators",
                        reason=f"snapshot_error: {exc}",
                    )
                )
            return {}
        out: dict[str, EvaluatorVerdict] = {}
        for entry in snap.get("verdicts", []) or []:
            subsystem = entry.get("subsystem")
            verdict = entry.get("verdict")
            if not subsystem or not verdict:
                continue
            observed_raw = entry.get("observed_at")
            try:
                observed_at = (
                    datetime.fromisoformat(observed_raw)
                    if isinstance(observed_raw, str)
                    else datetime.now(UTC)
                )
            except ValueError:
                observed_at = datetime.now(UTC)
            out[subsystem] = EvaluatorVerdict(
                subsystem=subsystem,
                verdict=verdict,
                reason=entry.get("reason") or "",
                observed_at=observed_at,
            )
        return out

    async def _fetch_characterization_ref(
        self,
        *,
        strategy_id: str,
        strategy_revision_id: str | None,
        correlation_id: str,
        gaps: list[ContextGap] | None = None,
    ) -> CharacterizationRef | None:
        """FR58 — return a typed reference to the admitted characterization
        for (strategy_id, strategy_revision_id), or ``None`` when the
        intent does not carry a revision id or data-manager has no record.

        This is observation-only — refusal on stale revisions is owned by
        the FR53 / P3.4 stale-gate at ``cio/core/characterization_stale_gate.py``.

        P1.4-AC2 (#132): when a revision id WAS supplied but data-manager
        returns non-200 or the call raises, append a
        ``ContextGap(surface='characterization')`` so the bundle flag is
        flipped. Missing revision id is *not* a gap — the legacy intent
        path is the expected steady-state for unrevisioned strategies.
        """
        if not strategy_revision_id:
            return None
        url = f"{self.data_manager_url}/api/v1/characterizations"
        params = {
            "strategy_id": strategy_id,
            "strategy_revision_id": strategy_revision_id,
        }
        try:
            response = await self.client.get(url, params=params)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "PreDecisionContext: characterization fetch failed — recording None",
                extra={
                    "correlation_id": correlation_id,
                    "strategy_id": strategy_id,
                    "strategy_revision_id": strategy_revision_id,
                    "error": str(exc),
                },
            )
            if gaps is not None:
                gaps.append(
                    ContextGap(
                        surface="characterization",
                        reason=f"fetch_error: {exc}",
                    )
                )
            return None
        if response.status_code != 200:
            if gaps is not None:
                gaps.append(
                    ContextGap(
                        surface="characterization",
                        reason=f"endpoint_{response.status_code}",
                    )
                )
            return None
        return CharacterizationRef(
            strategy_id=strategy_id,
            strategy_revision_id=strategy_revision_id,
        )

    async def _fetch_regime(
        self,
        symbol: str,
        correlation_id: str,
        gaps: list[ContextGap] | None = None,
        availability: dict[str, bool] | None = None,
    ) -> RegimeResult:
        """Fetches and maps regime data from petrosa-data-manager.

        P1.4-AC2 (#132): when the fetch falls back to the safe default
        (HTTP error, exception, or Data-Manager-reported empty regime),
        record a ``ContextGap(surface='market')`` and flip
        ``availability['market']=False`` if the optional collectors were
        supplied by ``build()``. Existing test paths that call this method
        directly (e.g. tests/unit/test_cold_path.py:115) pass no
        collectors, so the existing return contract is preserved.
        """
        try:
            url = f"{self.data_manager_url}/analysis/regime?pair={symbol}"
            response = await self.client.get(url)
            response.raise_for_status()

            data = response.json()
            # Defensive check: Data Manager returns 200 OK with an error message in metadata
            metadata = data.get("metadata", {})
            if (
                metadata
                and "message" in metadata
                and "No regime data" in metadata["message"]
            ):
                if gaps is not None:
                    gaps.append(
                        ContextGap(
                            surface="market",
                            reason=f"data_manager_empty: {metadata['message']}",
                        )
                    )
                if availability is not None:
                    availability["market"] = False
                return RegimeResult(
                    regime="choppy",
                    regime_confidence="low",
                    volatility_level=VolatilityLevel.MEDIUM,
                    primary_signal="data_manager_empty",
                    thought_trace=f"Data Manager reports: {metadata['message']}",
                )

            api_resp = RegimeAPIResponse.model_validate(data)
            return RegimeResult.from_api_response(api_resp)
        except Exception as e:
            logger.error(
                f"Failed to fetch regime: {e}", extra={"correlation_id": correlation_id}
            )
            if gaps is not None:
                gaps.append(
                    ContextGap(
                        surface="market",
                        reason=f"fetch_error: {e}",
                    )
                )
            if availability is not None:
                availability["market"] = False
            # Return safe default
            return RegimeResult(
                regime="choppy",
                regime_confidence="low",
                volatility_level=VolatilityLevel.MEDIUM,
                primary_signal="error",
                thought_trace=f"Error fetching regime: {str(e)}",
            )

    async def _fetch_portfolio_and_risk(
        self,
        symbol: str,
        correlation_id: str,
        gaps: list[ContextGap] | None = None,
        availability: dict[str, bool] | None = None,
    ) -> tuple[PortfolioSummary, RiskLimits, dict[str, Any]]:
        """Fetches portfolio and risk data from tradeengine.

        P1.4-AC2 (#132): when the call falls back to conservative defaults
        (exception path), record a ``ContextGap(surface='portfolio')`` and
        flip ``availability['portfolio']=False`` if the optional collectors
        were supplied. The conservative defaults are still returned so the
        Code Engine's gross_exposure=1.0 / orders=999 trigger-block path
        continues to fire — AC2 is record-not-block.
        """
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
            if gaps is not None:
                gaps.append(
                    ContextGap(
                        surface="portfolio",
                        reason=f"fetch_error: {e}",
                    )
                )
            if availability is not None:
                availability["portfolio"] = False
            # Safe conservative defaults (trigger blocks)
            return (
                PortfolioSummary(
                    gross_exposure=1.0,
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
