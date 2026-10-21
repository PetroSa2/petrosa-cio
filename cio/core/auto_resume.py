"""Safe automatic recovery for strategies paused by CIO."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import time
from asyncio.locks import Event as AsyncEvent
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from cio.core.alerting.fr66_alerts import (
    SEVERITY_CRITICAL,
    build_cio_action_alert,
    cio_action_subject,
    publish_fr66_alert,
)
from cio.core.cache import AsyncRedisCache
from cio.core.router import OutputRouter

logger = logging.getLogger(__name__)

REGISTRY_KEY = "cio:auto_resume:registry"
LOCK_KEY_PREFIX = "cio:auto_resume:lock:"
LOCK_TTL_SECONDS = 120
FREEZE_KEY_PREFIX = "cio:freeze:"
MIN_PAUSE_SECONDS = 1800
MAX_PAUSE_SECONDS = 14400
QUIET_PERIOD_SECONDS = 600
REPAUSE_WINDOW_SECONDS = 3600
MAX_FLAPS = 3
MAX_ATTEMPTS = 3
RETRY_BASE_SECONDS = 300
RATE_LIMIT_MIN_SECONDS, RATE_LIMIT_MAX_SECONDS, RATE_LIMIT_DEFAULT_SECONDS = (
    60,
    3600,
    300,
)
GAVE_UP_RETENTION_SECONDS = 86400
MAX_RESUMES_PER_TICK = 3
AUDIT_LIMIT = 20
HEALTH_WINDOW_SECONDS = 600
HEALTH_MIN_SAMPLES = 10
HEALTH_TAIL_OK = 3
DEFAULT_MIN_SUCCESS_RATIO = 0.9
DEFAULT_INTERVAL_SECONDS = 60.0
PROBE_TIMEOUT_SECONDS = 30.0
HEALTH_PROBE_PROMPT_ID = "PETROSA_PROMPT_HEALTH_PROBE"
HEALTH_PROBE_SYSTEM_PROMPT = (
    "You are a health check. Reply with exactly this JSON object and nothing else: "
    '{"ok": true}'
)


def auto_resume_enabled() -> bool:
    return os.getenv("CIO_AUTO_RESUME_ENABLED", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


class LLMHealthTracker:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        min_success_ratio: float | None = None,
    ) -> None:
        self._clock = clock
        self._min_success_ratio = (
            float(os.getenv("CIO_AUTO_RESUME_MIN_SUCCESS_RATIO", "0.9"))
            if min_success_ratio is None
            else min_success_ratio
        )
        self._samples: deque[tuple[float, bool]] = deque(maxlen=200)

    def record(self, ok: bool) -> None:
        self._samples.append((self._clock(), bool(ok)))

    def _window_samples(self) -> list[tuple[float, bool]]:
        cutoff = self._clock() - HEALTH_WINDOW_SECONDS
        return [sample for sample in self._samples if sample[0] >= cutoff]

    def sample_count(self) -> int:
        return len(self._window_samples())

    def success_ratio(self) -> float:
        samples = self._window_samples()
        if not samples:
            return 0.0
        return sum(ok for _, ok in samples) / len(samples)

    def is_healthy(self) -> bool:
        samples = self._window_samples()
        return (
            len(samples) >= HEALTH_MIN_SAMPLES
            and self.success_ratio() >= self._min_success_ratio
            and len(samples) >= HEALTH_TAIL_OK
            and all(ok for _, ok in samples[-HEALTH_TAIL_OK:])
        )


def instrument_llm_health(llm_client: Any, tracker: LLMHealthTracker) -> None:
    original = llm_client.complete
    if getattr(original, "_cio_llm_health_instrumented", False):
        return

    async def _tracked(
        prompt_id: str, system_prompt: str, user_context: dict[str, Any]
    ):
        try:
            raw = await original(prompt_id, system_prompt, user_context)
            tracker.record(ok=(not raw.error) and bool((raw.content or "").strip()))
            return raw
        except Exception:
            tracker.record(False)
            raise

    _tracked._cio_llm_health_instrumented = True
    llm_client.complete = _tracked  # type: ignore[method-assign]


@dataclass
class PauseEntry:
    strategy_id: str
    service: str
    status: str
    paused_at: float
    last_unavailable_at: float
    min_pause_seconds: int
    flap_count: int = 0
    attempts: int = 0
    next_attempt_at: float = 0.0
    expires_at: float | None = None
    next_recheck_at: float | None = None
    pause_reason: str = ""
    guarded_evidence: dict[str, Any] = dataclasses.field(default_factory=dict)
    renewal_count: int = 0
    resumed_at: float | None = None
    gave_up_at: float | None = None
    gave_up_reason: str | None = None

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> PauseEntry | None:
        if not isinstance(raw, str):
            return None
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                return None
            data.setdefault("expires_at", None)
            data.setdefault("next_recheck_at", None)
            data.setdefault("pause_reason", "")
            data.setdefault("guarded_evidence", {})
            data.setdefault("renewal_count", 0)
            return cls(**data)
        except Exception:
            return None


class LLMPauseRegistry:
    def __init__(self, store: Any, *, clock: Callable[[], float] = time.time):
        self.store = store
        self.clock = clock

    def _log_error(self, op: str, strategy_id: str, exc: Exception) -> None:
        logger.error(
            f"AUTO_RESUME_REGISTRY_ERROR op={op} strategy_id={strategy_id} "
            f"exc_type={type(exc).__name__}"
        )

    async def get(self, strategy_id: str) -> PauseEntry | None:
        try:
            raw = await self.store.get(strategy_id)
            return PauseEntry.from_json(raw) if isinstance(raw, str) else None
        except Exception as exc:
            self._log_error("get", strategy_id, exc)
            return None

    async def all(self) -> list[PauseEntry]:
        try:
            values = await self.store.all()
            if not isinstance(values, dict):
                return []
            entries = []
            for raw in values.values():
                entry = PauseEntry.from_json(raw) if isinstance(raw, str) else None
                if entry is not None:
                    entries.append(entry)
            return entries
        except Exception as exc:
            self._log_error("all", "*", exc)
            return []

    async def put(self, entry: PauseEntry) -> None:
        try:
            await self.store.put(entry.strategy_id, entry.to_json())
        except Exception as exc:
            self._log_error("put", entry.strategy_id, exc)

    async def remove(self, strategy_id: str, reason: str) -> None:
        try:
            existing = await self.store.get(strategy_id)
            if isinstance(existing, str) and PauseEntry.from_json(existing) is not None:
                await self.store.delete(strategy_id)
                logger.info(
                    f"AUTO_RESUME_CLEARED strategy_id={strategy_id} reason={reason}"
                )
        except Exception as exc:
            self._log_error("remove", strategy_id, exc)

    async def touch_unavailable(self, strategy_id: str) -> None:
        try:
            entry = await self.get(strategy_id)
            if entry is not None:
                entry.last_unavailable_at = self.clock()
                await self.put(entry)
        except Exception as exc:
            self._log_error("touch_unavailable", strategy_id, exc)

    async def record_pause(
        self,
        strategy_id: str,
        service: str,
        *,
        reason: str = "",
        guarded_evidence: dict[str, Any] | None = None,
    ) -> PauseEntry | None:
        try:
            now = self.clock()
            entry = await self.get(strategy_id)
            fresh = entry is None or (
                entry.status == "resumed"
                and entry.resumed_at is not None
                and now - entry.resumed_at >= REPAUSE_WINDOW_SECONDS
            )
            if fresh:
                entry = PauseEntry(
                    strategy_id=strategy_id,
                    service=service,
                    status="paused",
                    paused_at=now,
                    last_unavailable_at=now,
                    min_pause_seconds=MIN_PAUSE_SECONDS,
                    expires_at=now + MIN_PAUSE_SECONDS,
                    next_recheck_at=now + MIN_PAUSE_SECONDS,
                    pause_reason=reason,
                    guarded_evidence=dict(guarded_evidence or {}),
                )
            elif entry.status == "resumed":
                entry.flap_count += 1
                entry.status = "paused"
                entry.paused_at = now
                entry.last_unavailable_at = now
                entry.service = service
                entry.min_pause_seconds = min(
                    MIN_PAUSE_SECONDS * 2**entry.flap_count, MAX_PAUSE_SECONDS
                )
                entry.attempts = 0
                entry.next_attempt_at = 0.0
                entry.resumed_at = None
                entry.expires_at = now + entry.min_pause_seconds
                entry.next_recheck_at = entry.expires_at
                entry.pause_reason = reason
                entry.guarded_evidence = dict(guarded_evidence or {})
            elif entry.status == "paused":
                entry.last_unavailable_at = now
                entry.service = service
                entry.pause_reason = reason or entry.pause_reason
                if guarded_evidence:
                    entry.guarded_evidence = dict(guarded_evidence)
                if entry.expires_at is None:
                    entry.expires_at = now + entry.min_pause_seconds
                if entry.next_recheck_at is None:
                    entry.next_recheck_at = entry.expires_at
            elif entry.status == "gave_up":
                entry.last_unavailable_at = now
            entry.expires_at = entry.expires_at or now + entry.min_pause_seconds
            entry.next_recheck_at = entry.next_recheck_at or entry.expires_at
            await self.put(entry)
            logger.info(
                f"AUTO_RESUME_REGISTERED strategy_id={strategy_id} service={service} "
                f"status={entry.status} min_pause_s={entry.min_pause_seconds} "
                f"flap_count={entry.flap_count}"
            )
            return entry
        except Exception as exc:
            self._log_error("record_pause", strategy_id, exc)
            return None

    async def renew_pause(
        self,
        entry: PauseEntry,
        *,
        reason: str,
        guarded_evidence: dict[str, Any],
    ) -> PauseEntry:
        now = self.clock()
        entry.status = "paused"
        entry.paused_at = now
        entry.last_unavailable_at = now
        entry.expires_at = now + entry.min_pause_seconds
        entry.next_recheck_at = entry.expires_at
        entry.pause_reason = reason
        entry.guarded_evidence = dict(guarded_evidence)
        entry.renewal_count += 1
        entry.attempts = 0
        entry.next_attempt_at = 0.0
        await self.put(entry)
        return entry


async def migrate_redis_registry(cache: AsyncRedisCache, store: Any) -> None:
    """Copy legacy entries without overwriting newer durable-state entries."""
    try:
        values = await cache.hgetall(REGISTRY_KEY)
        if not isinstance(values, dict):
            return
        migrated = 0
        for strategy_id, raw in values.items():
            if await store.get(strategy_id) is None:
                await store.put(strategy_id, raw)
                migrated += 1
        await cache.delete(REGISTRY_KEY)
        logger.info("AUTO_RESUME_REGISTRY_MIGRATED count=%d", migrated)
    except Exception as exc:
        logger.error(
            "AUTO_RESUME_REGISTRY_MIGRATION_FAILED exc_type=%s", type(exc).__name__
        )


def build_resume_request(
    base_url: str, strategy_id: str, paused_at: float, service: str = "ta-bot"
) -> tuple[str, str, dict[str, Any]]:
    paused_at_iso = datetime.fromtimestamp(paused_at, UTC).isoformat()
    if service == "realtime-strategies":
        return (
            "PUT",
            f"{base_url}/api/v1/strategies/{strategy_id}/state",
            {
                "state": "running",
                "changed_by": f"petrosa-cio:lifecycle:{strategy_id}",
                "reason": (
                    "CIO_AUTO_RESUME: LLM healthy again after LLM_UNAVAILABLE pause at "
                    f"{paused_at_iso}"
                ),
            },
        )
    return (
        "POST",
        f"{base_url}/api/v1/strategies/{strategy_id}/config",
        {
            "parameters": {"enabled": True},
            "changed_by": f"petrosa-cio:resume:{strategy_id}",
            "reason": (
                "CIO_AUTO_RESUME: LLM healthy again after LLM_UNAVAILABLE pause at "
                f"{paused_at_iso}"
            ),
            "validate_only": False,
        },
    )


class AutoResumeLoop:
    def __init__(
        self,
        registry: LLMPauseRegistry,
        health_tracker: LLMHealthTracker,
        llm_client: Any,
        http_client: Any,
        service_urls: dict[str, str],
        cache: Any,
        nats_client: Any,
        *,
        evaluator_subscriber: Any = None,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.registry = registry
        self.health_tracker = health_tracker
        self.llm_client = llm_client
        self.http_client = http_client
        self.service_urls = service_urls
        self.cache = cache
        self.nats_client = nats_client
        self.evaluator_subscriber = evaluator_subscriber
        self.interval = interval_seconds
        self.clock = clock
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._event_is_real = isinstance(self._stop_event, AsyncEvent)
        self._last_healthy: bool | None = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run())
        logger.info(f"AUTO_RESUME_STARTED interval_s={self.interval}")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            task = self._task
            if not task.done():
                if self._event_is_real:
                    await task
                else:
                    task.cancel()
            if task.done():
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            self._task = None

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.interval)
                return
            except TimeoutError:
                pass
            try:
                await self.tick()
            except Exception as exc:
                logger.warning(f"AUTO_RESUME_TICK_FAILED exc_type={type(exc).__name__}")

    @staticmethod
    def _metric(result: str) -> None:
        try:
            from cio.core.metrics import AUTO_RESUME_EVENTS

            AUTO_RESUME_EVENTS.add(1, {"result": result})
        except ImportError:
            pass

    @staticmethod
    def _probe_metric(result: str) -> None:
        try:
            from cio.core.metrics import LLM_HEALTH_PROBES

            LLM_HEALTH_PROBES.add(1, {"result": result})
        except ImportError:
            pass

    def _eligible(self, entry: PauseEntry, now: float) -> bool:
        expires_at = (
            entry.expires_at
            if entry.expires_at is not None
            else entry.paused_at + entry.min_pause_seconds
        )
        return (
            entry.status == "paused"
            and now >= expires_at
            and now - entry.last_unavailable_at >= QUIET_PERIOD_SECONDS
            and now >= entry.next_attempt_at
        )

    async def _publish_lifecycle_event(
        self, event: str, entry: PauseEntry, *, evidence: dict[str, Any] | None = None
    ) -> None:
        payload = {
            "event": event,
            "strategy_id": entry.strategy_id,
            "service": entry.service,
            "status": entry.status,
            "expires_at": entry.expires_at,
            "next_recheck_at": entry.next_recheck_at,
            "reason": entry.pause_reason,
            "guarded_evidence": evidence
            if evidence is not None
            else entry.guarded_evidence,
            "renewal_count": entry.renewal_count,
        }
        try:
            await self.nats_client.publish(
                f"cio.lifecycle.pause.{event}.{entry.strategy_id}",
                json.dumps(payload).encode(),
            )
        except Exception as exc:
            logger.warning(
                "PAUSE_LIFECYCLE_EVENT_FAILED strategy_id=%s event=%s exc_type=%s",
                entry.strategy_id,
                event,
                type(exc).__name__,
            )

    async def _recheck_guarded_reasons(self, entry: PauseEntry) -> bool:
        if self.evaluator_subscriber is None or not entry.guarded_evidence:
            return False
        current = self.evaluator_subscriber.pause_evidence(
            tuple(entry.guarded_evidence)
        )
        if not current:
            return False
        await self.registry.renew_pause(
            entry,
            reason="guarded evaluator verdict remains unhealthy",
            guarded_evidence=current,
        )
        await self._publish_lifecycle_event("renewed", entry, evidence=current)
        self._metric("renewed")
        return True

    async def tick(self) -> None:
        now = self.clock()
        entries = sorted(await self.registry.all(), key=lambda entry: entry.paused_at)
        eligible: list[PauseEntry] = []
        for entry in entries:
            if entry.status == "resumed" and entry.resumed_at is not None:
                if now - entry.resumed_at >= REPAUSE_WINDOW_SECONDS:
                    await self.registry.remove(entry.strategy_id, "stable")
            elif entry.status == "gave_up" and entry.gave_up_at is not None:
                if now - entry.gave_up_at >= GAVE_UP_RETENTION_SECONDS:
                    await self.registry.remove(entry.strategy_id, "gave_up_expired")
            elif entry.status == "paused":
                if entry.flap_count >= MAX_FLAPS:
                    await self._give_up(entry, "flapping")
                elif entry.attempts >= MAX_ATTEMPTS:
                    await self._give_up(entry, "failed_attempts")
                elif self._eligible(entry, now):
                    eligible.append(entry)

        if not eligible:
            return

        if self.health_tracker.sample_count() < HEALTH_MIN_SAMPLES:
            ok = False
            try:
                raw = await asyncio.wait_for(
                    self.llm_client.complete(
                        HEALTH_PROBE_PROMPT_ID,
                        HEALTH_PROBE_SYSTEM_PROMPT,
                        {"probe": "cio_auto_resume"},
                    ),
                    PROBE_TIMEOUT_SECONDS,
                )
                ok = not raw.error and bool((raw.content or "").strip())
            except Exception:
                pass
            result = "ok" if ok else "fail"
            logger.info(
                f"LLM_HEALTH_PROBE result={result} samples={self.health_tracker.sample_count()} "
                f"success_ratio={self.health_tracker.success_ratio():.2f}"
            )
            self._probe_metric(result)

        healthy = self.health_tracker.is_healthy()
        if not healthy:
            if self._last_healthy is not False:
                logger.info(
                    f"AUTO_RESUME_WAITING_FOR_LLM_HEALTH eligible={len(eligible)} "
                    f"samples={self.health_tracker.sample_count()} "
                    f"success_ratio={self.health_tracker.success_ratio():.2f}"
                )
            self._last_healthy = False
            return
        self._last_healthy = True

        resumed = 0
        for entry in eligible:
            if resumed >= MAX_RESUMES_PER_TICK:
                break
            if await self._recheck_guarded_reasons(entry):
                continue
            strategy_id = entry.strategy_id
            if entry.service == "ta-bot" and await self.cache.get(
                FREEZE_KEY_PREFIX + strategy_id
            ):
                continue
            if not await self.cache.set_if_absent(
                LOCK_KEY_PREFIX + strategy_id, "1", LOCK_TTL_SECONDS
            ):
                continue
            current = await self.registry.get(strategy_id)
            if current is None or not self._eligible(current, self.clock()):
                continue
            await self._resume_one(current)
            resumed += 1

    async def _resume_one(self, entry: PauseEntry) -> None:
        base_url = self.service_urls.get(entry.service)
        if not base_url:
            await self._give_up(entry, "unroutable")
            return

        try:
            if entry.service == "realtime-strategies":
                response = await self.http_client.get(
                    f"{base_url}/api/v1/strategies/{entry.strategy_id}/state"
                )
            else:
                response = await self.http_client.get(
                    f"{base_url}/api/v1/strategies/{entry.strategy_id}/audit",
                    params={"limit": AUDIT_LIMIT},
                )
            if not 200 <= response.status_code < 300:
                raise ValueError(f"status={response.status_code}")
            body = response.json()
            if (
                not isinstance(body, dict)
                or body.get("success") is not True
                or "data" not in body
            ):
                raise ValueError("audit response is not verifiable")
            if entry.service == "realtime-strategies":
                state = body["data"]
                expected_changed_by = f"petrosa-cio:lifecycle:{entry.strategy_id}"
                if (
                    not isinstance(state, dict)
                    or state.get("state") != "paused"
                    or state.get("changed_by") != expected_changed_by
                ):
                    raise ValueError(
                        "state does not prove CIO lifecycle pause ownership"
                    )
                items = []
            else:
                if not isinstance(body["data"], list) or not body["data"]:
                    raise ValueError("audit response is not verifiable")
                items = body["data"]
            parsed: list[tuple[dict[str, Any], float]] = []
            for item in items:
                if not isinstance(item, dict):
                    raise ValueError("audit item is not an object")
                changed_at = item.get("changed_at")
                if not isinstance(changed_at, str):
                    raise ValueError("audit changed_at is not a string")
                changed = datetime.fromisoformat(changed_at)
                if changed.tzinfo is None:
                    changed = changed.replace(tzinfo=UTC)
                changed_by = item.get("changed_by")
                if not isinstance(changed_by, str):
                    raise ValueError("audit changed_by is not a string")
                parsed.append((item, changed.timestamp()))
                if changed.timestamp() > entry.paused_at and not changed_by.startswith(
                    "petrosa-cio:"
                ):
                    logger.warning(
                        f"AUTO_RESUME_ABORTED_FOREIGN_CHANGE strategy_id={entry.strategy_id} "
                        f"changed_by={changed_by} changed_at={changed.timestamp()}"
                    )
                    self._metric("aborted_foreign_change")
                    await self.registry.remove(entry.strategy_id, "foreign_change")
                    return
            if (
                entry.service != "realtime-strategies"
                and len(items) == AUDIT_LIMIT
                and all(changed_at > entry.paused_at for _, changed_at in parsed)
            ):
                raise ValueError("audit window cannot prove pause ownership")
        except Exception as exc:
            detail = str(exc) or "<empty>"
            logger.warning(
                f"AUTO_RESUME_PRECHECK_UNVERIFIABLE strategy_id={entry.strategy_id} "
                f"detail={detail}"
            )
            self._metric("precheck_unverifiable")
            await self._record_failure(entry, detail)
            return

        method, url, payload = build_resume_request(
            base_url, entry.strategy_id, entry.paused_at, entry.service
        )
        try:
            response = await self.http_client.request(method, url, json=payload)
            if response.status_code == 429:
                retry_after = RATE_LIMIT_DEFAULT_SECONDS
                try:
                    body = response.json()
                    retry_after = int(float(body.get("retry_after")))
                    retry_after = max(
                        RATE_LIMIT_MIN_SECONDS,
                        min(retry_after, RATE_LIMIT_MAX_SECONDS),
                    )
                except Exception:
                    pass
                entry.next_attempt_at = self.clock() + retry_after
                await self.registry.put(entry)
                logger.warning(
                    f"AUTO_RESUME_RATE_LIMITED strategy_id={entry.strategy_id} "
                    f"retry_after_s={retry_after}"
                )
                self._metric("rate_limited")
                return
            body_failed, _ = OutputRouter._response_reports_failure(response)
            if response.status_code >= 400 or body_failed:
                body_text = getattr(response, "text", "")
                await self._record_failure(
                    entry, f"status={response.status_code} body={body_text[:200]}"
                )
                return
        except Exception as exc:
            await self._record_failure(
                entry,
                f"exc_type={type(exc).__name__} detail={str(exc) or '<empty>'}",
            )
            return

        now = self.clock()
        entry.status = "resumed"
        entry.resumed_at = now
        entry.attempts = 0
        entry.next_attempt_at = 0.0
        await self.registry.put(entry)
        logger.info(
            f"AUTO_RESUME_SUCCEEDED strategy_id={entry.strategy_id} service={entry.service} "
            f"paused_for_s={int(now - entry.paused_at)} flap_count={entry.flap_count}"
        )
        self._metric("succeeded")
        await self._publish_lifecycle_event("resumed", entry)

    async def _record_failure(self, entry: PauseEntry, detail: str) -> None:
        entry.attempts += 1
        backoff = RETRY_BASE_SECONDS * 2 ** (entry.attempts - 1)
        entry.next_attempt_at = self.clock() + backoff
        await self.registry.put(entry)
        logger.warning(
            f"AUTO_RESUME_FAILED strategy_id={entry.strategy_id} attempts={entry.attempts} "
            f"next_attempt_in_s={backoff} detail={detail}"
        )
        self._metric("failed")
        if entry.attempts >= MAX_ATTEMPTS:
            await self._give_up(entry, "failed_attempts")

    async def _give_up(self, entry: PauseEntry, reason: str) -> None:
        entry.status = "gave_up"
        entry.gave_up_at = self.clock()
        entry.gave_up_reason = reason
        justification = (
            f"AUTO_RESUME_GAVE_UP strategy_id={entry.strategy_id} reason={reason} "
            f"attempts={entry.attempts} flap_count={entry.flap_count} — strategy left paused, "
            "re-enable manually"
        )
        await self.registry.put(entry)
        logger.error(justification)
        self._metric("gave_up")
        await publish_fr66_alert(
            self.nats_client,
            subject=cio_action_subject("auto_resume_gave_up", entry.strategy_id),
            payload=build_cio_action_alert(
                action="auto_resume_gave_up",
                strategy_id=entry.strategy_id,
                decision_id=None,
                justification=justification,
                severity=SEVERITY_CRITICAL,
            ),
        )
