"""Signal distillation for bursty alert streams."""

from __future__ import annotations

import time
from dataclasses import dataclass

from core.alerting.manager import AlertEvent


@dataclass(slots=True)
class _Bucket:
    template: AlertEvent
    first_ts: float
    last_ts: float
    count: int


class AlertDistiller:
    """Aggregates repetitive alerts into summary notifications."""

    def __init__(self, aggregation_window_seconds: int = 300):
        self.aggregation_window_seconds = aggregation_window_seconds
        self._buckets: dict[str, _Bucket] = {}

    @staticmethod
    def _key(event: AlertEvent) -> str:
        return "|".join(
            [
                event.source,
                event.category,
                event.severity,
                event.symbol or "",
                event.message,
            ]
        )

    def ingest(self, event: AlertEvent, now: float | None = None) -> None:
        now_ts = now if now is not None else time.time()
        key = self._key(event)

        if key not in self._buckets:
            self._buckets[key] = _Bucket(
                template=event,
                first_ts=now_ts,
                last_ts=now_ts,
                count=1,
            )
            return

        bucket = self._buckets[key]
        bucket.last_ts = now_ts
        bucket.count += 1

    def flush(
        self, now: float | None = None, *, force: bool = False
    ) -> list[AlertEvent]:
        now_ts = now if now is not None else time.time()
        emitted: list[AlertEvent] = []
        to_delete: list[str] = []

        for key, bucket in self._buckets.items():
            elapsed = now_ts - bucket.last_ts
            if not force and elapsed < self.aggregation_window_seconds:
                continue

            template = bucket.template
            if bucket.count == 1:
                emitted.append(template)
            else:
                summary_context = dict(template.context)
                summary_context[
                    "aggregation_window_seconds"
                ] = self.aggregation_window_seconds
                emitted.append(
                    AlertEvent(
                        source=template.source,
                        category=template.category,
                        message=(
                            f"Summary Alert: {bucket.count} similar events for "
                            f"{template.symbol or 'global'} - {template.message}"
                        ),
                        severity=template.severity,
                        symbol=template.symbol,
                        count=bucket.count,
                        context=summary_context,
                    )
                )

            to_delete.append(key)

        for key in to_delete:
            del self._buckets[key]

        return emitted
