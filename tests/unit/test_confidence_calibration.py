from fastapi import FastAPI
from fastapi.testclient import TestClient

from cio.apps.calibration_api import router
from cio.core.confidence_calibration import (
    CalibrationConfig,
    ConfidenceCalibrationService,
    _records_from_response,
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


def test_data_manager_response_contract_requires_executed_records() -> None:
    records = _records_from_response(
        {
            "records": [
                {
                    "strategy_id": "s",
                    "confidence": 0.75,
                    "net_pnl": 2.0,
                }
            ]
        }
    )
    assert records[0]["strategy_id"] == "s"


def test_data_manager_response_contract_rejects_unknown_shape() -> None:
    try:
        _records_from_response({"outcomes": []})
    except ValueError as exc:
        assert "records" in str(exc)
    else:
        raise AssertionError("invalid calibration response was accepted")


def test_service_uses_only_the_documented_records_field() -> None:
    class Client:
        async def get(self, url):
            return Response()

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "records": [{"strategy_id": "s", "confidence": 0.75, "net_pnl": 1.0}]
            }

    import asyncio

    report = asyncio.run(ConfidenceCalibrationService(client=Client()).report())
    assert report["outcome_source"] == "executed"


def test_response_contract_rejects_invalid_record_fields() -> None:
    invalid_records = [
        ["not an object"],
        [{"confidence": 0.5, "net_pnl": 1.0}],
        [{"strategy_id": "s", "net_pnl": 1.0}],
        [{"strategy_id": "s", "confidence": 0.5}],
    ]
    for records in invalid_records:
        try:
            _records_from_response({"records": records})
        except ValueError:
            continue
        raise AssertionError("invalid calibration record was accepted")


def test_calibrated_confidence_is_clamped_and_invalid_confidence_is_skipped() -> None:
    config = CalibrationConfig(neutral_prior=0.5)
    assert effective_confidence(2.0, {"calibrated": True}, config) == 1.0
    report = build_report(
        [{"strategy_id": "s", "confidence": 1.5, "net_pnl": 1.0}], config
    )
    assert report["groups"][0]["n"] == 1


def test_empty_report_has_executed_overall_group() -> None:
    report = build_report([], CalibrationConfig(minimum_sample=1))
    assert report["groups"][0]["strategy_id"] == "overall"
