"""Integration tests for /api/dashboard routes (#654, P5.1a follow-up)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cio.apps.dashboard_api import router as dashboard_router
from cio.core.decision_store import DecisionRecord, DecisionStore

_UTC = UTC


def _make_app(*, decision_store=None, evaluator_subscriber=None) -> FastAPI:
    app = FastAPI()
    app.include_router(dashboard_router)
    if decision_store is not None:
        app.state.decision_store = decision_store
    if evaluator_subscriber is not None:
        app.state.evaluator_subscriber = evaluator_subscriber
    return app


# ---------------------------------------------------------------------------
# /api/dashboard/decisions/recent
# ---------------------------------------------------------------------------


class TestDecisionsRecent:
    def test_returns_decisions_within_window(self):
        store = DecisionStore()
        now = datetime.now(_UTC)
        store.record(
            DecisionRecord(
                strategy_id="strat-1",
                action="EXECUTE",
                reasoning_trace="trace A",
                confidence=0.9,
                timestamp=now - timedelta(hours=1),
            )
        )
        store.record(
            DecisionRecord(
                strategy_id="strat-1",
                action="SKIP",
                reasoning_trace="trace B",
                confidence=0.3,
                timestamp=now - timedelta(hours=30),  # outside 24h window
            )
        )
        client = TestClient(_make_app(decision_store=store))
        resp = client.get("/api/dashboard/decisions/recent?window=24h")
        assert resp.status_code == 200
        body = resp.json()
        assert body["window"] == "24h"
        decisions = body["decisions"]
        assert len(decisions) == 1
        assert decisions[0]["action"] == "EXECUTE"
        assert decisions[0]["strategy_id"] == "strat-1"
        assert "decision_id" in decisions[0]
        assert "timestamp" in decisions[0]

    def test_filters_by_strategy_id(self):
        store = DecisionStore()
        now = datetime.now(_UTC)
        store.record(
            DecisionRecord(
                strategy_id="strat-A",
                action="EXECUTE",
                reasoning_trace="trace",
                confidence=0.8,
                timestamp=now - timedelta(hours=1),
            )
        )
        store.record(
            DecisionRecord(
                strategy_id="strat-B",
                action="PAUSE_STRATEGY",
                reasoning_trace="trace",
                confidence=0.5,
                timestamp=now - timedelta(hours=1),
            )
        )
        client = TestClient(_make_app(decision_store=store))
        resp = client.get(
            "/api/dashboard/decisions/recent?window=24h&strategy_id=strat-A"
        )
        assert resp.status_code == 200
        decisions = resp.json()["decisions"]
        assert all(d["strategy_id"] == "strat-A" for d in decisions)
        assert len(decisions) == 1

    def test_invalid_window_returns_400(self):
        store = DecisionStore()
        client = TestClient(_make_app(decision_store=store))
        resp = client.get("/api/dashboard/decisions/recent?window=99x")
        assert resp.status_code == 400
        body = resp.json()
        assert body["detail"]["status"] == 400
        assert "window" in body["detail"]["detail"]

    def test_missing_store_returns_503(self):
        client = TestClient(_make_app())
        resp = client.get("/api/dashboard/decisions/recent")
        assert resp.status_code == 503
        assert resp.json()["detail"]["status"] == 503

    def test_response_includes_required_fields(self):
        store = DecisionStore()
        store.record(
            DecisionRecord(
                strategy_id="s1",
                action="EXECUTE",
                reasoning_trace="why",
                confidence=0.7,
                timestamp=datetime.now(_UTC) - timedelta(minutes=5),
            )
        )
        client = TestClient(_make_app(decision_store=store))
        resp = client.get("/api/dashboard/decisions/recent")
        assert resp.status_code == 200
        d = resp.json()["decisions"][0]
        for f in (
            "decision_id",
            "strategy_id",
            "action",
            "reasoning_trace",
            "confidence",
            "timestamp",
        ):
            assert f in d, f"missing field: {f}"


# ---------------------------------------------------------------------------
# /api/dashboard/evaluator/verdicts
# ---------------------------------------------------------------------------


class TestEvaluatorVerdicts:
    def _make_subscriber(self, verdicts: dict) -> MagicMock:
        sub = MagicMock()
        sub._verdicts = verdicts
        return sub

    def test_returns_all_verdicts(self):
        now = datetime.now(_UTC)
        sub = self._make_subscriber(
            {
                "ingest": ("healthy", "all good", now),
                "strategies": ("degraded", "latency", now),
            }
        )
        client = TestClient(_make_app(evaluator_subscriber=sub))
        resp = client.get("/api/dashboard/evaluator/verdicts")
        assert resp.status_code == 200
        body = resp.json()
        assert "subsystems" in body
        assert len(body["subsystems"]) == 2
        fields = {"subsystem", "verdict", "last_tick_at", "evidence"}
        for item in body["subsystems"]:
            assert fields <= item.keys()

    def test_filters_by_subsystem(self):
        now = datetime.now(_UTC)
        sub = self._make_subscriber(
            {
                "ingest": ("healthy", "ok", now),
                "strategies": ("unhealthy", "down", now),
            }
        )
        client = TestClient(_make_app(evaluator_subscriber=sub))
        resp = client.get("/api/dashboard/evaluator/verdicts?subsystem=ingest")
        assert resp.status_code == 200
        items = resp.json()["subsystems"]
        assert len(items) == 1
        assert items[0]["subsystem"] == "ingest"
        assert items[0]["verdict"] == "healthy"

    def test_unknown_subsystem_returns_404(self):
        sub = self._make_subscriber({})
        client = TestClient(_make_app(evaluator_subscriber=sub))
        resp = client.get("/api/dashboard/evaluator/verdicts?subsystem=nonexistent")
        assert resp.status_code == 404
        assert resp.json()["detail"]["status"] == 404

    def test_missing_subscriber_returns_503(self):
        client = TestClient(_make_app())
        resp = client.get("/api/dashboard/evaluator/verdicts")
        assert resp.status_code == 503

    def test_content_type_is_json(self):
        sub = self._make_subscriber({"cio": ("healthy", "ok", datetime.now(_UTC))})
        client = TestClient(_make_app(evaluator_subscriber=sub))
        resp = client.get("/api/dashboard/evaluator/verdicts")
        assert "application/json" in resp.headers["content-type"]


# Suppress pytest collection warning for unused import
_ = pytest
