"""Tests for ``cio.apps.admission_api`` (petrosa-cio#156, FR54-B precursor)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from cio.apps import admission_api


@dataclass
class _RecorderTracker:
    """Test double matching the methods admission_api invokes."""

    calls: list[dict[str, Any]] = field(default_factory=list)

    async def record_admit(
        self,
        *,
        strategy_id: str,
        position_size_usd: float,
        leverage: float,
    ) -> None:
        self.calls.append(
            {
                "strategy_id": strategy_id,
                "position_size_usd": position_size_usd,
                "leverage": leverage,
            }
        )


def _make_app(tracker: Any | None) -> FastAPI:
    app = FastAPI()
    if tracker is not None:
        app.state.portfolio_tracker = tracker
    app.include_router(admission_api.router)
    return app


# ─── happy path ──────────────────────────────────────────────────────────────


def test_register_returns_201_and_invokes_record_admit() -> None:
    tracker = _RecorderTracker()
    client = TestClient(_make_app(tracker))
    response = client.post(
        "/api/admission/register",
        json={
            "strategy_id": "momentum-v3",
            "position_size_usd": 10000.0,
            "leverage": 3.0,
            "strategy_revision_id": "rev-abc",
            "submitted_by": "alice",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "admitted"
    assert body["strategy_id"] == "momentum-v3"
    assert body["position_size_usd"] == 10000.0
    assert body["leverage"] == 3.0
    assert body["strategy_revision_id"] == "rev-abc"
    assert body["submitted_by"] == "alice"
    assert tracker.calls == [
        {
            "strategy_id": "momentum-v3",
            "position_size_usd": 10000.0,
            "leverage": 3.0,
        }
    ]


# ─── idempotent re-register ─────────────────────────────────────────────────


def test_register_then_re_register_with_same_keys_overwrites_in_tracker() -> None:
    tracker = _RecorderTracker()
    client = TestClient(_make_app(tracker))
    payload = {
        "strategy_id": "momentum-v3",
        "position_size_usd": 10000.0,
        "leverage": 3.0,
    }
    a = client.post("/api/admission/register", json=payload)
    b = client.post(
        "/api/admission/register",
        json={**payload, "position_size_usd": 15000.0},
    )
    assert a.status_code == 201
    assert b.status_code == 201  # tracker.record_admit is a replace, not an insert
    assert len(tracker.calls) == 2
    assert tracker.calls[1]["position_size_usd"] == 15000.0


# ─── validation ─────────────────────────────────────────────────────────────


def test_empty_strategy_id_is_422() -> None:
    tracker = _RecorderTracker()
    client = TestClient(_make_app(tracker))
    response = client.post(
        "/api/admission/register",
        json={"strategy_id": "", "position_size_usd": 1.0, "leverage": 1.0},
    )
    assert response.status_code == 422
    assert tracker.calls == []


def test_leverage_below_1_is_422() -> None:
    tracker = _RecorderTracker()
    client = TestClient(_make_app(tracker))
    response = client.post(
        "/api/admission/register",
        json={"strategy_id": "x", "position_size_usd": 1.0, "leverage": 0.5},
    )
    assert response.status_code == 422


def test_negative_size_is_422() -> None:
    tracker = _RecorderTracker()
    client = TestClient(_make_app(tracker))
    response = client.post(
        "/api/admission/register",
        json={"strategy_id": "x", "position_size_usd": -1.0, "leverage": 1.0},
    )
    assert response.status_code == 422


def test_missing_required_fields_is_422() -> None:
    tracker = _RecorderTracker()
    client = TestClient(_make_app(tracker))
    response = client.post(
        "/api/admission/register",
        json={"strategy_id": "x"},  # missing size + leverage
    )
    assert response.status_code == 422


# ─── unwired tracker → 503 ──────────────────────────────────────────────────


def test_unwired_tracker_returns_503() -> None:
    client = TestClient(_make_app(tracker=None))
    response = client.post(
        "/api/admission/register",
        json={"strategy_id": "x", "position_size_usd": 1.0, "leverage": 1.0},
    )
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["title"] == "PortfolioTracker not wired"


# ─── audit fields are optional ──────────────────────────────────────────────


def test_audit_fields_optional_default_to_none() -> None:
    tracker = _RecorderTracker()
    client = TestClient(_make_app(tracker))
    response = client.post(
        "/api/admission/register",
        json={"strategy_id": "x", "position_size_usd": 100.0, "leverage": 1.0},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["strategy_revision_id"] is None
    assert body["submitted_by"] is None
