import asyncio
import logging
import os
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

try:
    from datetime import UTC
except ImportError:  # pragma: no cover — py310 compatibility
    from datetime import timezone

    UTC = timezone.utc  # noqa: UP017

import httpx

from cio.core.service_resolver import TargetServiceResolver
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

# #202 — placeholder defaults for the regime-classifier profile fields.
# When the trigger payload carries none of the real producing fields, these
# are emitted so the LLM never sees a "neutral-looking" profile presented as
# observation; the fallback is instead flagged via degraded_fields + a gap.
_DEFAULT_SIGNAL_SUMMARY = "Manual trigger"
_DEFAULT_VOLATILITY_PERCENTILE = 0.5
_DEFAULT_TREND_STRENGTH = 0.0
_DEFAULT_PRICE_ACTION = "Neutral"

DM_PERFORMANCE_COMPUTED_SOURCE = "data-manager-pnl-calculator"


def classify_strategy_history(raw: dict) -> str:
    """Classify the completeness and provenance of performance history."""
    stats = raw.get("stats")
    if not isinstance(stats, dict):
        return "unavailable"

    required_fields = ("win_rate", "win_rate_delta", "consecutive_losses")
    if all(stats.get(field) is not None for field in required_fields):
        return "computed"

    metadata = raw.get("metadata")
    fills_replayed = (
        metadata.get("fills_replayed") if isinstance(metadata, dict) else None
    )
    if (
        isinstance(metadata, dict)
        and metadata.get("source") == DM_PERFORMANCE_COMPUTED_SOURCE
        and all(field in stats for field in required_fields)
        and isinstance(fills_replayed, int)
        and not isinstance(fills_replayed, bool)
        and fills_replayed >= 0
    ):
        return "insufficient_history"
    return "unavailable"


# #236: "Failed to fetch portfolio/risk: All connection attempts failed" is
# httpx.ConnectError — a fast TCP-level failure (connection refused / no
# route), not a slow read timeout. #199 already trimmed CIO_CONTEXT_FETCH
# _TIMEOUT_S specifically to stop retries-via-timeout from burning the
# decision window, so retrying here is deliberately scoped to *only* the
# fast connect-failure case, with a short capped backoff — a transient NATS
# / tradeengine network blip self-heals without adding meaningful latency to
# the decision path, while a persistent outage still falls back to the
# existing conservative defaults after retries are exhausted.
_PORTFOLIO_FETCH_MAX_RETRIES = int(os.getenv("CIO_PORTFOLIO_FETCH_RETRIES", "2"))
_PORTFOLIO_FETCH_RETRY_BACKOFF_S = float(
    os.getenv("CIO_PORTFOLIO_FETCH_RETRY_BACKOFF_S", "0.25")
)
_PORTFOLIO_STALE_MAX_S = float(os.getenv("CIO_PORTFOLIO_STALE_MAX_S", "120"))

_SIGNAL_STRENGTH_TO_TREND = {
    "weak": 0.25,
    "medium": 0.5,
    "strong": 0.75,
    "extreme": 0.95,
}

_SIGNAL_ACTION_TO_PRICE_ACTION = {
    "buy": "Bullish",
    "long": "Bullish",
    "sell": "Bearish",
    "short": "Bearish",
    "hold": "Neutral",
    "close": "Neutral",
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
        clock: Callable[[], float] | None = None,
    ):
        self.data_manager_url = data_manager_url
        self.tradeengine_url = tradeengine_url
        self.vector_client = vector_client
        self._clock = clock or time.monotonic
        self._portfolio_cache: dict[
            str, tuple[float, PortfolioSummary, RiskLimits, dict[str, Any]]
        ] = {}
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

        # #199: previously hardcoded to 30s. Three concurrent 30s
        # ReadTimeouts (market/strategy_stats/strategy_defaults, the
        # CONTEXT_FETCH_TIMEOUT_STORM pattern) burn most of the decision
        # window before the fallback defaults even kick in. Reduced
        # default to 10s — data-manager's steady-state p99 is well under
        # that; env-overridable so a slow-cluster deploy can raise it
        # back without a code change.
        timeout_s = float(os.getenv("CIO_CONTEXT_FETCH_TIMEOUT_S", "10.0"))
        self.client = httpx.AsyncClient(
            timeout=timeout_s,
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
        # petrosa-cio#212: the raw payload value may be a human display name
        # ("Spread Liquidity Monitor" instead of "spread_liquidity") — the
        # routing path already normalizes via the same resolver
        # (router.py:_resolve_routing_strategy_id / TargetServiceResolver),
        # but this context path previously used the raw value verbatim for
        # every downstream fetch key and URL (_fetch_strategy_data,
        # vector_client.query, and /analysis/performance/{strategy_id} in
        # _fetch_strategy_stats). A display-name id there is a guaranteed
        # data-manager URL miss -> empty stats -> pause_strategy bias.
        strategy_id = TargetServiceResolver.canonicalize(
            payload.get("strategy_id", "unknown")
        )

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
            "market_signals": True,
        }
        fetch_tasks = [
            self._fetch_regime(
                symbol, correlation_id, gaps=gaps, availability=availability
            ),
            self._fetch_portfolio_and_risk(
                symbol, correlation_id, gaps=gaps, availability=availability
            ),
            self._fetch_strategy_data(strategy_id, correlation_id, gaps=gaps),
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

        # AC2 (#197): a single upstream outage (data-manager unreachable/slow)
        # commonly times out regime + strategy_stats + strategy_defaults
        # concurrently, since all three hit the same host under the same
        # asyncio.gather(). Detect that pattern from the per-surface gaps
        # recorded by the individual _fetch_* methods and emit ONE
        # correlated summary line — instead of forcing an operator to piece
        # together three separate per-surface log lines — while the
        # individual WARNING lines (AC1/AC3) remain for per-surface detail.
        self._log_timeout_storm_if_concurrent(gaps, correlation_id)

        # Assemble TriggerContext
        # Pass decision_id only when provided; TriggerContext.default_factory generates one otherwise
        extra = {"decision_id": decision_id} if decision_id is not None else {}

        # P1.5-AC3 (#137) / #174 — surface the strategy's configured leverage
        # (fetched from data-manager's strategy config, `defaults.leverage`)
        # as `recommended_leverage` so `arbitrate_leverage` exercises its
        # primary accept/override branches instead of permanently falling
        # back to the operator-max-only path. `strategy_leverage_envelope`
        # is left None until petrosa-data-manager#179 ships the per-strategy
        # characterization-derived envelope field.
        recommended_leverage: int | None = None
        if defaults.leverage is not None and defaults.leverage >= 1:
            recommended_leverage = int(round(defaults.leverage))

        market_signals = self._build_market_signals(payload, correlation_id, gaps=gaps)
        availability["market_signals"] = not market_signals.is_placeholder

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
            symbol=symbol,
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
            recommended_leverage=recommended_leverage,
            global_drawdown_pct=env_stats.get("global_drawdown_pct", 0.0),
            open_orders_global=env_stats.get("open_orders_global", 0),
            open_orders_symbol=env_stats.get("open_orders_symbol", 0),
            available_capital_usd=env_stats.get("available_capital_usd", 0.0),
            portfolio=portfolio,
            risk_limits=risk,
            historical_context=historical_context,
            pre_decision_context=pre_decision_context,
        )

    def _build_market_signals(
        self,
        payload: dict[str, Any],
        correlation_id: str,
        gaps: list[ContextGap] | None = None,
    ) -> MarketSignals:
        """#202 — map real signal-producer fields onto the MarketSignals profile.

        The trigger payload carries the fields ta-bot / realtime-strategies
        actually publish (``confidence``, ``strength``, ``current_price``,
        ``action``/``side``/``signal_type``, and a ``metadata`` blob). The
        regime-classifier prompt consumes the four profile fields
        (signal_summary / volatility_percentile / trend_strength /
        price_action_character); previously they were static placeholders,
        so the prompt-contract rule #4 made the model self-report
        MISSING_INPUT and the loop degraded to ``pause_strategy``.

        This derives the profile from the real producer fields where present
        (confidence -> volatility percentile, strength -> trend magnitude,
        action -> price-action character, metadata -> signal summary) and
        keeps the placeholder only for fields the payload truly does not
        carry. Every field that still lands on a placeholder is recorded as a
        degraded field on the model plus a ``ContextGap(surface=
        'market_signals')`` and a WARNING line — the fallback is explicit,
        never silent.
        """
        metadata = payload.get("metadata") or {}

        signal_summary = payload.get("signal_summary")
        if not signal_summary:
            signal_summary = metadata.get("signal_summary")
        if not signal_summary:
            signal_summary = metadata.get("summary")
        # RTS/ta-bot producers that carry no explicit summary field describe the
        # signal in prose under metadata.reasoning (e.g. the iceberg detector's
        # "Large hidden seller detected at ..."). Derive the profile summary from
        # that so a live signal is not reported as a degraded summary field.
        if not signal_summary:
            signal_summary = payload.get("reasoning")
        if not signal_summary:
            signal_summary = metadata.get("reasoning")
        if not signal_summary:
            signal_summary = metadata.get("reason")
        has_summary = bool(signal_summary)
        if not has_summary:
            signal_summary = _DEFAULT_SIGNAL_SUMMARY

        volatility_percentile: float | None = payload.get("volatility_percentile")
        if volatility_percentile is None:
            volatility_percentile = metadata.get("volatility_percentile")
        has_volatility = volatility_percentile is not None
        if not has_volatility:
            confidence = payload.get("confidence")
            if isinstance(confidence, int | float) and 0 < confidence <= 1:
                volatility_percentile = round(float(confidence), 4)
                has_volatility = True
        if volatility_percentile is None:
            volatility_percentile = _DEFAULT_VOLATILITY_PERCENTILE

        trend_strength: float | None = payload.get("trend_strength")
        if trend_strength is None:
            trend_strength = metadata.get("trend_strength")
        has_trend = trend_strength is not None
        if not has_trend:
            strength_key = payload.get("strength")
            if strength_key is None:
                strength_key = metadata.get("strength")
            if strength_key is not None:
                trend_strength = _SIGNAL_STRENGTH_TO_TREND.get(
                    str(strength_key).lower()
                )
                has_trend = trend_strength is not None
        if trend_strength is None:
            trend_strength = _DEFAULT_TREND_STRENGTH

        price_action_character: str | None = payload.get("price_action_character")
        if not price_action_character:
            price_action_character = metadata.get("price_action_character")
        if not price_action_character:
            price_action_character = metadata.get("price_action")
        has_price_action = bool(price_action_character)
        if not has_price_action:
            action_key = (
                payload.get("action")
                or payload.get("side")
                or payload.get("signal_type")
            )
            if action_key is not None:
                price_action_character = _SIGNAL_ACTION_TO_PRICE_ACTION.get(
                    str(action_key).lower()
                )
                has_price_action = price_action_character is not None
        if price_action_character is None:
            price_action_character = _DEFAULT_PRICE_ACTION

        degraded_fields = [
            field
            for field, present in (
                ("signal_summary", has_summary),
                ("volatility_percentile", has_volatility),
                ("trend_strength", has_trend),
                ("price_action_character", has_price_action),
            )
            if not present
        ]

        if degraded_fields:
            logger.warning(
                "MARKET_SIGNALS_PLACEHOLDER_FIELDS: %s",
                ",".join(degraded_fields),
                extra={
                    "correlation_id": correlation_id,
                    "surface": "market_signals",
                    "degraded_fields": degraded_fields,
                },
            )
            if gaps is not None:
                gaps.append(
                    ContextGap(
                        surface="market_signals",
                        reason="placeholder_fields: " + ",".join(degraded_fields),
                    )
                )

        return MarketSignals(
            signal_summary=signal_summary,
            current_price=float(
                payload.get("current_price") or payload.get("price") or 0.0
            ),
            volatility_percentile=float(volatility_percentile),
            trend_strength=float(trend_strength),
            price_action_character=price_action_character,
            degraded_fields=sorted(degraded_fields),
            is_placeholder=bool(degraded_fields),
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
        symbol: str | None = None,
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

        ``symbol`` (#193) is the trigger's symbol, threaded through to
        :meth:`_collect_evaluator_verdicts` so a verdict the publisher
        scoped to a *different* symbol does not bias this signal's LLM
        context. ``None`` (the default, matching every pre-#193 caller)
        keeps the conservative legacy behavior — every unhealthy verdict
        is shown regardless of scope.
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
        evaluator_verdicts = self._collect_evaluator_verdicts(
            gaps=local_gaps, symbol=symbol
        )
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
                "market_signals": True,
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
            market_signals_available=avail.get("market_signals", True),
            gaps=list(local_gaps),
        )

    def _collect_evaluator_verdicts(
        self,
        gaps: list[ContextGap] | None = None,
        symbol: str | None = None,
    ) -> dict[str, EvaluatorVerdict]:
        """FR57 — project the evaluator subscriber's snapshot into a typed dict.

        Tolerates the legacy "no subscriber wired" case by returning an
        empty dict. The subscriber's snapshot shape is documented at
        ``EvaluatorSubscriber.snapshot``.

        P1.4-AC2 (#132): when the subscriber is missing or ``snapshot()``
        raises, append a ``ContextGap(surface='evaluators')`` to ``gaps`` so
        the bundle's availability flag is flipped and the FR12 audit-trail
        consumer can persist the event.

        Blast-radius scoping (#193): when an entry carries a ``scope``
        (``{"symbols": [...]}``) that does NOT include ``symbol``, the
        verdict is excluded from this signal's context — a fault the
        publisher isolated to another symbol should not bias this
        signal's LLM prompt toward ``pause_strategy``/``skip``, mirroring
        the same narrowing ``SignalArbiter.check`` applies to the hard
        gate. Unscoped verdicts (``scope is None``, the default every
        current publisher emits) and ``symbol=None`` callers keep the
        legacy behavior of always surfacing the verdict.
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
            # AC3 (cio#169): Exclude CIO's own health from the LLM decision surface.
            # CIOHealthEvaluator emits evaluator.cio.verdict which the EvaluatorSubscriber
            # picks up and feeds back into context. A self-unhealthy verdict causes the LLM
            # to pause strategies on every signal — a circular self-assessment bias.
            # Pod liveness/readiness owns CIO health recovery, not per-signal decisions.
            if subsystem == "cio":
                logger.debug(
                    "EVALUATOR_VERDICT_FILTERED: cio self-health excluded from LLM context"
                )
                continue
            scope = entry.get("scope")
            # #193: a verdict scoped to symbols that do NOT include this
            # trigger's symbol is not relevant to this decision — exclude
            # it so the LLM prompt isn't biased toward pause_strategy/skip
            # for a fault isolated elsewhere. Unscoped (scope is None) or
            # symbol=None (caller doesn't know) keeps the conservative
            # legacy behavior of always surfacing the verdict.
            if (
                verdict == "unhealthy"
                and isinstance(scope, dict)
                and scope.get("symbols")
                and symbol is not None
                and symbol not in scope["symbols"]
            ):
                logger.debug(
                    "EVALUATOR_VERDICT_FILTERED: subsystem=%s unhealthy scoped to "
                    "%s, excluded from context for symbol=%s",
                    subsystem,
                    scope.get("symbols"),
                    symbol,
                )
                continue
            out[subsystem] = EvaluatorVerdict(
                subsystem=subsystem,
                verdict=verdict,
                reason=entry.get("reason") or "",
                scope=scope if isinstance(scope, dict) else None,
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
        url = f"{self.data_manager_url}/analysis/regime?pair={symbol}"
        try:
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
        except httpx.ReadTimeout:
            # AC1/AC3 (#197): httpx.ReadTimeout.__str__() is '' on 0.28.1 —
            # str(e) alone produces the misleading "Failed to fetch regime: "
            # empty tail. Classify by exception TYPE, log at WARNING (not the
            # generic ERROR path below) with the timeout value + endpoint so
            # the failure is identifiable without a debugger.
            timeout_s = self.client.timeout.read
            logger.warning(
                f"FETCH_TIMEOUT surface=market endpoint={url} "
                f"timeout_s={timeout_s} exc_type=ReadTimeout — data-manager "
                "did not respond within the configured timeout",
                extra={
                    "correlation_id": correlation_id,
                    "surface": "market",
                    "endpoint": url,
                    "timeout_s": timeout_s,
                    "exc_type": "ReadTimeout",
                },
            )
            if gaps is not None:
                gaps.append(
                    ContextGap(
                        surface="market",
                        reason=f"read_timeout endpoint={url} timeout_s={timeout_s}",
                    )
                )
            if availability is not None:
                availability["market"] = False
            return RegimeResult(
                regime="choppy",
                regime_confidence="low",
                volatility_level=VolatilityLevel.MEDIUM,
                primary_signal="timeout",
                thought_trace=f"ReadTimeout after {timeout_s}s calling {url}",
            )
        except Exception as e:
            # AC1: log the exception TYPE, never just str(e) — several httpx
            # exceptions (ReadTimeout among them) stringify to '' and would
            # otherwise mask the failure behind an empty-tail log line.
            exc_type = type(e).__name__
            detail = str(e) or "<empty>"
            logger.error(
                f"Failed to fetch regime: exc_type={exc_type} endpoint={url} "
                f"detail={detail}",
                extra={
                    "correlation_id": correlation_id,
                    "surface": "market",
                    "endpoint": url,
                    "exc_type": exc_type,
                },
            )
            if gaps is not None:
                gaps.append(
                    ContextGap(
                        surface="market",
                        reason=f"fetch_error exc_type={exc_type} detail={detail}",
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
                thought_trace=f"Error fetching regime: exc_type={exc_type} detail={detail}",
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
        url = f"{self.tradeengine_url}/state?symbol={symbol}"
        last_exc: Exception = RuntimeError("unreachable")
        for attempt in range(_PORTFOLIO_FETCH_MAX_RETRIES + 1):
            try:
                response = await self.client.get(url)
                response.raise_for_status()
                data = response.json()

                portfolio = PortfolioSummary(**data["portfolio"])
                risk = RiskLimits(**data["risk_limits"])
                env_stats = data["env_stats"]
                self._portfolio_cache[symbol] = (
                    self._clock(),
                    portfolio.model_copy(deep=True),
                    risk.model_copy(deep=True),
                    dict(env_stats),
                )

                return portfolio, risk, env_stats
            except httpx.ConnectError as e:
                last_exc = e
                if attempt < _PORTFOLIO_FETCH_MAX_RETRIES:
                    logger.warning(
                        "PORTFOLIO_FETCH_CONNECT_RETRY attempt=%d/%d endpoint=%s "
                        "detail=%s",
                        attempt + 1,
                        _PORTFOLIO_FETCH_MAX_RETRIES,
                        url,
                        str(e) or "<empty>",
                        extra={"correlation_id": correlation_id},
                    )
                    await asyncio.sleep(
                        _PORTFOLIO_FETCH_RETRY_BACKOFF_S * (attempt + 1)
                    )
                    continue
                break
            except Exception as e:
                last_exc = e
                break

        if isinstance(last_exc, httpx.ConnectError):
            cached = self._portfolio_cache.get(symbol)
            if cached is not None:
                fetched_at, portfolio, risk, env_stats = cached
                age_s = self._clock() - fetched_at
                if 0 <= age_s < _PORTFOLIO_STALE_MAX_S:
                    logger.warning(
                        "PORTFOLIO_FETCH_STALE_CACHE_USED age_s=%.3f symbol=%s",
                        age_s,
                        symbol,
                        extra={"correlation_id": correlation_id},
                    )
                    if gaps is not None:
                        gaps.append(
                            ContextGap(
                                surface="portfolio",
                                reason="portfolio_state_stale_cache",
                            )
                        )
                    if availability is not None:
                        availability["portfolio"] = False
                    return (
                        portfolio.model_copy(deep=True),
                        risk.model_copy(deep=True),
                        dict(env_stats),
                    )

        e = last_exc
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
        self,
        strategy_id: str,
        correlation_id: str,
        gaps: list[ContextGap] | None = None,
    ) -> tuple[StrategyStats, StrategyDefaults]:
        """
        Fetches strategy performance and DNA from the Data Manager.
        Consolidates analytics and configuration into the CIO context.
        """
        # Parallelize strategy-specific fetches
        tasks = [
            self._fetch_strategy_stats(strategy_id, correlation_id, gaps=gaps),
            self._fetch_strategy_defaults(strategy_id, correlation_id, gaps=gaps),
        ]
        results = await asyncio.gather(*tasks)
        return results[0], results[1]

    async def _fetch_strategy_stats(
        self,
        strategy_id: str,
        correlation_id: str,
        gaps: list[ContextGap] | None = None,
    ) -> StrategyStats:
        """Fetch historical performance metrics and classify their provenance."""
        url = f"{self.data_manager_url}/analysis/performance/{strategy_id}"
        try:
            response = await self.client.get(url)
            response.raise_for_status()
            data = response.json()
            history_status = classify_strategy_history(data)
            stats_data = {
                key: value
                for key, value in data["stats"].items()
                if key not in ("history_status", "fill_count")
            }
            stats = StrategyStats(**stats_data).model_copy(
                update={"history_status": history_status}
            )
            structurally_absent = [
                field
                for field, value in (
                    ("win_rate", stats.win_rate),
                    ("win_rate_delta", stats.win_rate_delta),
                    ("consecutive_losses", stats.consecutive_losses),
                )
                if value is None
            ]
            if history_status == "insufficient_history":
                fills_replayed = data["metadata"]["fills_replayed"]
                logger.info(
                    "STRATEGY_STATS_INSUFFICIENT_HISTORY strategy_id=%s "
                    "fills_replayed=%s null_fields=%s",
                    strategy_id,
                    fills_replayed,
                    ",".join(structurally_absent),
                    extra={
                        "correlation_id": correlation_id,
                        "surface": "strategy_stats",
                        "strategy_id": strategy_id,
                        "fills_replayed": fills_replayed,
                        "null_fields": structurally_absent,
                    },
                )
            elif structurally_absent and gaps is not None:
                logger.warning(
                    "STRATEGY_STATS_STRUCTURAL_GAP: fields=%s strategy_id=%s "
                    "endpoint=%s — data-manager returned 200 but these fields "
                    "are not yet computed upstream (see petrosa-data-manager "
                    "follow-up); strategy_assessor will self-report "
                    "MISSING_INPUT for this trigger",
                    ",".join(structurally_absent),
                    strategy_id,
                    url,
                    extra={
                        "correlation_id": correlation_id,
                        "surface": "strategy_stats",
                        "strategy_id": strategy_id,
                        "endpoint": url,
                        "structurally_absent_fields": structurally_absent,
                    },
                )
                gaps.append(
                    ContextGap(
                        surface="strategy_stats",
                        reason=(
                            "structural_gap: "
                            + ",".join(structurally_absent)
                            + " never populated by data-manager"
                        ),
                    )
                )
            return stats
        except httpx.ReadTimeout:
            timeout_s = self.client.timeout.read
            logger.warning(
                f"FETCH_TIMEOUT surface=strategy_stats endpoint={url} "
                f"timeout_s={timeout_s} exc_type=ReadTimeout strategy_id={strategy_id} "
                "— data-manager did not respond within the configured timeout",
                extra={
                    "correlation_id": correlation_id,
                    "surface": "strategy_stats",
                    "endpoint": url,
                    "timeout_s": timeout_s,
                    "exc_type": "ReadTimeout",
                },
            )
            if gaps is not None:
                gaps.append(
                    ContextGap(
                        surface="strategy_stats",
                        reason=f"read_timeout endpoint={url} timeout_s={timeout_s}",
                    )
                )
            return StrategyStats(
                recent_pnl_trend=PnlTrend.NEUTRAL,
                history_status="unavailable",
            )
        except Exception as e:
            exc_type = type(e).__name__
            detail = str(e) or "<empty>"
            logger.warning(
                f"Failed to fetch strategy stats for {strategy_id}: "
                f"exc_type={exc_type} endpoint={url} detail={detail}",
                extra={
                    "correlation_id": correlation_id,
                    "surface": "strategy_stats",
                    "endpoint": url,
                    "exc_type": exc_type,
                },
            )
            if gaps is not None:
                gaps.append(
                    ContextGap(
                        surface="strategy_stats",
                        reason=f"fetch_error exc_type={exc_type} detail={detail}",
                    )
                )
            return StrategyStats(
                recent_pnl_trend=PnlTrend.NEUTRAL,
                history_status="unavailable",
            )

    async def _fetch_strategy_defaults(
        self,
        strategy_id: str,
        correlation_id: str,
        gaps: list[ContextGap] | None = None,
    ) -> StrategyDefaults:
        """Fetches strategy DNA (defaults) from Data Manager config API.

        Records a ``ContextGap(surface='strategy_defaults')`` on fallback so
        AC2's concurrent-timeout-storm detection in ``build()`` can see this
        surface alongside market/strategy_stats (AC4 only mandates the
        ``strategy_stats`` surface be tracked; this mirrors it for
        consistency and to support the AC2 correlation check).
        """
        url = f"{self.data_manager_url}/api/v1/config/strategies/{strategy_id}"
        try:
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
        except httpx.ReadTimeout:
            timeout_s = self.client.timeout.read
            logger.warning(
                f"FETCH_TIMEOUT surface=strategy_defaults endpoint={url} "
                f"timeout_s={timeout_s} exc_type=ReadTimeout strategy_id={strategy_id} "
                "— data-manager did not respond within the configured timeout",
                extra={
                    "correlation_id": correlation_id,
                    "surface": "strategy_defaults",
                    "endpoint": url,
                    "timeout_s": timeout_s,
                    "exc_type": "ReadTimeout",
                },
            )
            if gaps is not None:
                gaps.append(
                    ContextGap(
                        surface="strategy_defaults",
                        reason=f"read_timeout endpoint={url} timeout_s={timeout_s}",
                    )
                )
            return StrategyDefaults(
                stop_loss_pct=0.01,
                take_profit_pct=0.01,
                leverage=1.0,
                max_hold_hours=1.0,
            )
        except Exception as e:
            exc_type = type(e).__name__
            detail = str(e) or "<empty>"
            logger.warning(
                f"Failed to fetch strategy defaults for {strategy_id}: "
                f"exc_type={exc_type} endpoint={url} detail={detail}",
                extra={
                    "correlation_id": correlation_id,
                    "surface": "strategy_defaults",
                    "endpoint": url,
                    "exc_type": exc_type,
                },
            )
            if gaps is not None:
                gaps.append(
                    ContextGap(
                        surface="strategy_defaults",
                        reason=f"fetch_error exc_type={exc_type} detail={detail}",
                    )
                )
            return StrategyDefaults(
                stop_loss_pct=0.01,
                take_profit_pct=0.01,
                leverage=1.0,
                max_hold_hours=1.0,
            )

    @staticmethod
    def _log_timeout_storm_if_concurrent(
        gaps: list[ContextGap], correlation_id: str
    ) -> None:
        """AC2 (#197): when regime + strategy_stats + strategy_defaults all
        time out concurrently (single upstream data-manager outage), emit
        ONE consolidated summary line — timeout duration + affected
        endpoints — instead of forcing an operator to correlate three
        separate per-surface log lines by hand. Fires only when 2+ surfaces
        recorded a ``read_timeout`` gap in the same ``build()`` call; the
        per-surface WARNING lines (AC1/AC3) remain untouched for
        single-surface failures.
        """
        timeout_gaps = [g for g in gaps if g.reason.startswith("read_timeout")]
        if len(timeout_gaps) < 2:
            return
        surfaces = ", ".join(g.surface for g in timeout_gaps)
        endpoints = "; ".join(g.reason for g in timeout_gaps)
        logger.error(
            f"CONTEXT_FETCH_TIMEOUT_STORM surfaces=[{surfaces}] "
            f"count={len(timeout_gaps)} correlation_id={correlation_id} "
            f"details=[{endpoints}] — data-manager appears unreachable or "
            "overloaded; multiple context surfaces degraded concurrently",
            extra={
                "correlation_id": correlation_id,
                "surfaces": [g.surface for g in timeout_gaps],
                "count": len(timeout_gaps),
            },
        )

    async def close(self):
        await self.client.aclose()
