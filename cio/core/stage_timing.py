"""Per-stage elapsed milliseconds of one decision (petrosa-cio#312).

A decision runs in one asyncio task chain, so a ``ContextVar`` holding a shared dict follows it through the
tasks it spawns. ``begin()`` starts the record at the NATS handler, ``stage(name)`` times a block into it, and
``summary()`` renders it for the decision log line. Outside a decision (tests, tools) ``stage`` is a no-op.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_STAGES: ContextVar[dict[str, float] | None] = ContextVar("cio_stage_ms", default=None)


def begin() -> dict[str, float]:
    """Start the stage record of a decision in the current context and return it."""
    record: dict[str, float] = {}
    _STAGES.set(record)
    return record


@contextmanager
def stage(name: str) -> Iterator[None]:
    """Add the elapsed time of the block to the decision's ``name`` stage (no-op without ``begin()``)."""
    record = _STAGES.get()
    started = time.perf_counter()
    try:
        yield
    finally:
        if record is not None:
            record[name] = (
                record.get(name, 0.0) + (time.perf_counter() - started) * 1000.0
            )


def summary(record: dict[str, float] | None = None) -> str:
    """``name=ms`` pairs in the order the stages ran, ``-`` when nothing was recorded."""
    record = _STAGES.get() if record is None else record
    if not record:
        return "-"
    return ",".join(f"{name}={ms:.0f}ms" for name, ms in record.items())
