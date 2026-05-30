"""``/healthz/envelopes`` operator observability endpoint (P4.6-AC3.f, #154, FR62).

Exposes the cio-side envelope cache so an operator can see, per
``strategy_or_portfolio_key``, which envelope ``version`` + ``source`` the
process is currently treating as active. Useful for confirming that a
freshly-approved operator envelope has propagated to cio (cache miss →
fetched fresh) vs. is still being served from a stale cache (``fresh: false``).

The reader is the :class:`cio.core.envelope_fetcher.EnvelopeFetcher`
instance attached to ``app.state.envelope_fetcher`` at startup. If the
fetcher isn't wired (e.g. local dev without data-manager reachable),
the endpoint returns an empty entries dict with ``status="unwired"``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

router = APIRouter(tags=["healthz"])


@router.get("/healthz/envelopes")
def envelopes_health(request: Request) -> dict[str, Any]:
    fetcher = getattr(request.app.state, "envelope_fetcher", None)
    if fetcher is None:
        return {
            "status": "unwired",
            "detail": (
                "EnvelopeFetcher not attached to app.state — local-dev mode or "
                "data-manager dependency not initialized at startup."
            ),
            "entries": {},
        }
    snapshot = fetcher.cache_snapshot()
    return {
        "status": "ok",
        "entries": snapshot,
        "count": len(snapshot),
    }
