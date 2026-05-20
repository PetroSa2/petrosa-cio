"""Tests for the strategy lifecycle FastAPI router (P1.2, #114).

Covers:
  * POST /strategies/register — happy path + duplicate (409) + minted decision_id
  * GET  /strategies/{sid}/lifecycle — returns history with current state
  * GET  /strategies — lists registered ids
  * 404 when the lifecycle store has no record of the strategy
  * 503 when the lifecycle store is missing from app.state
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cio.apps.lifecycle_api import router as lifecycle_router
from cio.core.lifecycle import StrategyLifecycleStore


@pytest.fixture
def app_with_store() -> FastAPI:
    app = FastAPI()
    app.state.lifecycle_store = StrategyLifecycleStore()
    app.include_router(lifecycle_router)
    return app


@pytest.fixture
def client(app_with_store) -> TestClient:
    return TestClient(app_with_store)


# ---------------------------------------------------------------------------
# Registration endpoint
# ---------------------------------------------------------------------------


def test_register_strategy_returns_genesis_event(client):
    resp = client.post(
        "/strategies/register",
        json={
            "strategy_id": "strat_alpha",
            "definition": {"family": "mean_rev", "version": "1.0.0"},
            "decision_id": "dec-alpha",
            "reasoning": {"why": "operator_request"},
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["strategy_id"] == "strat_alpha"
    assert body["current_state"] == "registered"
    assert body["event"]["from_state"] is None
    assert body["event"]["to_state"] == "registered"
    assert body["event"]["action"] is None
    assert body["event"]["decision_id"] == "dec-alpha"


def test_register_mints_decision_id_when_omitted(client):
    resp = client.post(
        "/strategies/register",
        json={"strategy_id": "strat_alpha", "definition": {}},
    )
    assert resp.status_code == 201
    minted = resp.json()["event"]["decision_id"]
    assert minted and len(minted) >= 16


def test_register_duplicate_returns_409(client):
    payload = {"strategy_id": "strat_alpha", "definition": {}}
    assert client.post("/strategies/register", json=payload).status_code == 201
    resp = client.post("/strategies/register", json=payload)
    assert resp.status_code == 409
    assert "already registered" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# History endpoint (feeds FR9)
# ---------------------------------------------------------------------------


def test_lifecycle_history_returns_current_and_history(app_with_store):
    # Mutate the store directly to seed a multi-event history.
    store: StrategyLifecycleStore = app_with_store.state.lifecycle_store
    store.register("strat_alpha", {"family": "mean_rev"})
    store.characterize("strat_alpha", reasoning={"backtest": "passed"})
    store.admit("strat_alpha", reasoning={"size": "full"})

    client = TestClient(app_with_store)
    resp = client.get("/strategies/strat_alpha/lifecycle")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["strategy_id"] == "strat_alpha"
    assert body["current_state"] == "admitted"
    actions = [e["action"] for e in body["history"]]
    assert actions == [None, None, "admit"]
    # Every event in the history carries a decision_id (P0.1 contract).
    assert all(e["decision_id"] for e in body["history"])


def test_lifecycle_history_unknown_strategy_404(client):
    resp = client.get("/strategies/ghost/lifecycle")
    assert resp.status_code == 404
    assert "not registered" in resp.json()["detail"]


def test_list_strategies(client):
    client.post(
        "/strategies/register",
        json={"strategy_id": "a", "definition": {}},
    )
    client.post(
        "/strategies/register",
        json={"strategy_id": "b", "definition": {}},
    )
    resp = client.get("/strategies")
    assert resp.status_code == 200
    assert set(resp.json()["strategy_ids"]) == {"a", "b"}


# ---------------------------------------------------------------------------
# Store missing from app.state (defensive)
# ---------------------------------------------------------------------------


def test_endpoint_returns_503_when_store_missing():
    app = FastAPI()
    app.include_router(lifecycle_router)
    client = TestClient(app)
    resp = client.post(
        "/strategies/register",
        json={"strategy_id": "x", "definition": {}},
    )
    assert resp.status_code == 503
