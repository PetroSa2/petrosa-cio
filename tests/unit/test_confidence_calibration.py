from fastapi import FastAPI
from fastapi.testclient import TestClient

from cio.apps.calibration_api import router
from cio.core.confidence_calibration import (
    CalibrationConfig,
    build_report,
    effective_confidence,
)


def test_predictive_and_balanced_brier_values() -> None:
    predictive = build_report(
        [
            {"strategy_id": "s", "confidence": 1.0, "net_pnl": 1},
            {"strategy_id": "s", "confidence": 0.0, "net_pnl": -1},
        ],
        CalibrationConfig(minimum_sample=1, reliability_bound=0.1),
    )
    assert predictive["groups"][1]["brier_score"] == 0.0
    balanced = build_report(
        [
            {"strategy_id": "s", "confidence": 0.5, "net_pnl": 1},
            {"strategy_id": "s", "confidence": 0.5, "net_pnl": -1},
        ],
        CalibrationConfig(minimum_sample=1, reliability_bound=0.1),
    )
    assert balanced["groups"][1]["brier_score"] == 0.25


def test_net_costs_define_loss_and_low_sample_is_first() -> None:
    report = build_report(
        [
            {"strategy_id": "small", "confidence": 0.9, "gross_pnl": 1, "costs": 2},
            {"strategy_id": "large", "confidence": 0.1, "net_pnl": -1},
        ],
        CalibrationConfig(minimum_sample=2, reliability_bound=0.1),
    )
    assert report["groups"][1]["sample_ok"] is False
    assert report["groups"][1]["realized_net_win_rate"] == 0.0
    assert report["groups"][1]["outcome_source"] == "executed"


def test_uncalibrated_confidence_uses_neutral_prior() -> None:
    config = CalibrationConfig(neutral_prior=0.5)
    assert effective_confidence(0.61, {"calibrated": False}, config) == 0.5
    assert effective_confidence(0.95, {"calibrated": False}, config) == 0.5


def test_endpoint_is_read_only_and_returns_report() -> None:
    app = FastAPI()
    app.include_router(router)

    class Service:
        async def report(self):
            return {"outcome_source": "executed", "groups": []}

    app.state.confidence_calibration = Service()
    response = TestClient(app).get("/api/v1/calibration/confidence")
    assert response.status_code == 200
    assert response.json()["outcome_source"] == "executed"


def test_endpoint_reports_unavailable_source() -> None:
    app = FastAPI()
    app.include_router(router)

    class Service:
        async def report(self):
            raise RuntimeError("source unavailable")

    app.state.confidence_calibration = Service()
    response = TestClient(app).get("/api/v1/calibration/confidence")
    assert response.status_code == 503
