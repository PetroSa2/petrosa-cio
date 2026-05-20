"""Tests for the per-action authority FastAPI router (P1.3, #115).

Covers GET/PUT authority state, audit endpoint, pending-queue listing,
approve / reject endpoints, and the 503/404/422 paths.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cio.apps.authority_api import router as authority_router
from cio.core.authority import AuthorityStore
from cio.models.enums import ActionType


@pytest.fixture
def app_with_store() -> FastAPI:
    app = FastAPI()
    app.state.authority_store = AuthorityStore()
    app.include_router(authority_router)
    return app


@pytest.fixture
def client(app_with_store) -> TestClient:
    return TestClient(app_with_store)


# ---------------------------------------------------------------------------
# State CRUD
# ---------------------------------------------------------------------------


def test_list_authority_returns_default_enabled_for_every_action(client):
    resp = client.get("/authority")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    states = {e["action"]: e["state"] for e in body["states"]}
    # Every ActionType is present and ENABLED by default.
    assert states[ActionType.EXECUTE.value] == "enabled"
    assert states[ActionType.ADMIT.value] == "enabled"
    assert len(states) == len(list(ActionType))


def test_get_authority_for_single_action(client):
    resp = client.get(f"/authority/{ActionType.EXECUTE.value}")
    assert resp.status_code == 200
    assert resp.json() == {"action": "execute", "state": "enabled"}


def test_put_authority_records_change_and_updates_state(client):
    resp = client.put(
        f"/authority/{ActionType.EXECUTE.value}",
        json={
            "state": "operator_approval_required",
            "operator_id": "op-yuri",
            "reason": "manual review week",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["action"] == "execute"
    assert body["from_state"] == "enabled"
    assert body["to_state"] == "operator_approval_required"
    assert body["operator_id"] == "op-yuri"
    assert body["reason"] == "manual review week"

    # Confirm the change propagates.
    follow_up = client.get(f"/authority/{ActionType.EXECUTE.value}")
    assert follow_up.json()["state"] == "operator_approval_required"


def test_put_authority_missing_operator_returns_422(client):
    resp = client.put(
        f"/authority/{ActionType.EXECUTE.value}",
        json={"state": "disabled", "operator_id": "", "reason": "r"},
    )
    assert resp.status_code == 422


def test_audit_endpoint_returns_change_history(client):
    client.put(
        f"/authority/{ActionType.EXECUTE.value}",
        json={
            "state": "disabled",
            "operator_id": "op-a",
            "reason": "step 1",
        },
    )
    client.put(
        f"/authority/{ActionType.EXECUTE.value}",
        json={
            "state": "enabled",
            "operator_id": "op-b",
            "reason": "step 2",
        },
    )
    resp = client.get("/authority/audit")
    assert resp.status_code == 200
    changes = resp.json()["changes"]
    assert len(changes) == 2
    assert changes[0]["operator_id"] == "op-a"
    assert changes[1]["operator_id"] == "op-b"


# ---------------------------------------------------------------------------
# Pending queue
# ---------------------------------------------------------------------------


def _seed_pending(app: FastAPI) -> str:
    store: AuthorityStore = app.state.authority_store
    pending = store.enqueue_pending(
        action=ActionType.EXECUTE,
        strategy_id="strat_alpha",
        decision_id="dec-115",
        correlation_id="corr-115",
        context_payload={"symbol": "BTCUSDT"},
        decision_payload={"action": "execute", "justification": "j"},
    )
    return pending.queue_id


def test_list_pending_returns_diverted_decisions(app_with_store):
    queue_id = _seed_pending(app_with_store)
    client = TestClient(app_with_store)
    resp = client.get("/authority/pending")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["pending"]) == 1
    assert body["pending"][0]["queue_id"] == queue_id
    assert body["pending"][0]["action"] == "execute"
    assert body["pending"][0]["decision_id"] == "dec-115"


def test_approve_pending_returns_resolution_and_clears_queue(app_with_store):
    queue_id = _seed_pending(app_with_store)
    client = TestClient(app_with_store)
    resp = client.post(
        f"/authority/pending/{queue_id}/approve",
        json={"operator_id": "op-yuri", "reason": "looks fine"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["queue_id"] == queue_id
    assert body["approved"] is True
    assert body["operator_id"] == "op-yuri"
    assert body["pending"]["action"] == "execute"
    # Queue is now empty.
    assert client.get("/authority/pending").json()["pending"] == []


def test_reject_pending_returns_resolution_and_clears_queue(app_with_store):
    queue_id = _seed_pending(app_with_store)
    client = TestClient(app_with_store)
    resp = client.post(
        f"/authority/pending/{queue_id}/reject",
        json={"operator_id": "op-yuri", "reason": "too risky"},
    )
    assert resp.status_code == 200
    assert resp.json()["approved"] is False
    assert client.get("/authority/pending").json()["pending"] == []


def test_resolve_unknown_queue_returns_404(client):
    resp = client.post(
        "/authority/pending/ghost/approve",
        json={"operator_id": "op-yuri", "reason": ""},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Defensive: missing store
# ---------------------------------------------------------------------------


def test_endpoint_returns_503_when_store_missing():
    app = FastAPI()
    app.include_router(authority_router)
    client = TestClient(app)
    resp = client.get("/authority")
    assert resp.status_code == 503
