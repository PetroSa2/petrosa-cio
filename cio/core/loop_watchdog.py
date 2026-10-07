"""Event-loop stall watchdog (petrosa-cio#312).

A blocked loop stops answering the liveness probe and kills the pod, and it leaves no log line behind: the
blocked code cannot log. This runs in a thread of its own: it asks the loop to answer a ping every
``interval_s`` and, when the answer takes longer than ``threshold_s``, logs ``EVENT_LOOP_BLOCKED`` with the
stack the loop thread is stuck in (read from ``sys._current_frames``), then ``EVENT_LOOP_RECOVERED`` with the
total stall when the loop answers again. It never touches the loop except to schedule the ping.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
import traceback

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLD_S = 5.0
DEFAULT_INTERVAL_S = 1.0


def _float_env(name: str, default: float) -> float:
    try:
        value = float(os.environ[name])
    except (KeyError, ValueError):
        return default
    return value if value > 0 else default


class LoopWatchdog:
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        *,
        threshold_s: float | None = None,
        interval_s: float | None = None,
    ) -> None:
        self._loop = loop
        self.threshold_s = threshold_s or _float_env(
            "CIO_LOOP_WATCHDOG_THRESHOLD_S", DEFAULT_THRESHOLD_S
        )
        self.interval_s = interval_s or _float_env(
            "CIO_LOOP_WATCHDOG_INTERVAL_S", DEFAULT_INTERVAL_S
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop_thread_id: int | None = None
        self.stalls = 0
        self.max_stall_s = 0.0

    def start(self) -> None:
        """Start watching; call from the loop's own thread so its stack can be found."""
        if self._thread is not None:
            return
        self._loop_thread_id = threading.get_ident()
        self._thread = threading.Thread(
            target=self._run, name="cio-loop-watchdog", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _stack(self) -> str:
        frame = sys._current_frames().get(self._loop_thread_id or -1)
        if frame is None:
            return "unavailable"
        return "".join(traceback.format_stack(frame)[-12:])

    def _run(self) -> None:
        while not self._stop.is_set():
            answered = threading.Event()
            sent = time.monotonic()
            try:
                self._loop.call_soon_threadsafe(answered.set)
            except RuntimeError:  # the loop is closed
                return
            if not answered.wait(self.threshold_s):
                self.stalls += 1
                logger.error(
                    "EVENT_LOOP_BLOCKED for >= %.1fs; the loop thread is in:\n%s",
                    self.threshold_s,
                    self._stack(),
                )
                while not answered.wait(self.interval_s):
                    if self._stop.is_set():
                        return
                stalled = time.monotonic() - sent
                self.max_stall_s = max(self.max_stall_s, stalled)
                logger.error("EVENT_LOOP_RECOVERED after %.1fs", stalled)
            self._stop.wait(self.interval_s)
