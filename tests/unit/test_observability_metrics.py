import json
import logging

from cio.core.metrics import (
    DECISION_LABELS,
    LLM_CALL_LABELS,
    LLM_LATENCY_LABELS,
    PETROSA_CIO_DECISIONS_NAME,
    PETROSA_CIO_DECISIONS_TOTAL,
    PETROSA_CIO_LLM_CALLS_NAME,
    PETROSA_CIO_LLM_CALLS_TOTAL,
    PETROSA_CIO_LLM_LATENCY_NAME,
    PETROSA_CIO_LLM_LATENCY_SECONDS,
    SummaryLog,
)


def test_cio_metric_catalog_has_bounded_labels():
    assert PETROSA_CIO_DECISIONS_NAME == "petrosa_cio_decisions_total"
    assert PETROSA_CIO_LLM_CALLS_NAME == "petrosa_cio_llm_calls_total"
    assert PETROSA_CIO_LLM_LATENCY_NAME == "petrosa_cio_llm_latency_seconds"
    assert PETROSA_CIO_DECISIONS_TOTAL is not None
    assert PETROSA_CIO_LLM_CALLS_TOTAL is not None
    assert PETROSA_CIO_LLM_LATENCY_SECONDS is not None
    assert DECISION_LABELS == {"action", "reason", "outcome"}
    assert LLM_CALL_LABELS == {"route", "outcome", "provider"}
    assert LLM_LATENCY_LABELS == {"route", "provider"}


def test_summary_record_is_bounded_and_uses_300_second_window(caplog):
    now = [0.0]
    summary = SummaryLog(clock=lambda: now[0])
    summary.decision("skip", "decision", "completed")
    summary.llm_call("success", 0.25)
    now[0] = 300.0

    with caplog.at_level(logging.INFO, logger="cio.core.metrics"):
        record = summary.emit()

    assert record is not None
    assert record["event"] == "SUMMARY"
    assert record["window_seconds"] == 300
    assert record["service"] == "petrosa-cio"
    payload = json.loads(caplog.records[-1].message)
    assert payload["latency_seconds"] == {"p50": 0.25, "p95": 0.25}
    assert set(payload) == {
        "event",
        "window_seconds",
        "service",
        "decisions",
        "llm_calls",
        "latency_seconds",
    }
