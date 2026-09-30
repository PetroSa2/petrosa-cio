import json
import logging
import statistics
import time
from collections import Counter
from threading import Lock

from opentelemetry import metrics

meter = metrics.get_meter("cio")

logger = logging.getLogger(__name__)

DECISION_LABELS = frozenset({"action", "reason", "outcome"})
LLM_CALL_LABELS = frozenset({"route", "outcome", "provider"})
LLM_LATENCY_LABELS = frozenset({"route", "provider"})
PETROSA_CIO_DECISIONS_NAME = "petrosa_cio_decisions_total"
PETROSA_CIO_LLM_CALLS_NAME = "petrosa_cio_llm_calls_total"
PETROSA_CIO_LLM_LATENCY_NAME = "petrosa_cio_llm_latency_seconds"

PETROSA_CIO_DECISIONS_TOTAL = meter.create_counter(
    PETROSA_CIO_DECISIONS_NAME,
    description="CIO decisions by action, reason, and outcome",
)
PETROSA_CIO_LLM_CALLS_TOTAL = meter.create_counter(
    PETROSA_CIO_LLM_CALLS_NAME,
    description="CIO LLM calls by route, outcome, and provider",
)
PETROSA_CIO_LLM_LATENCY_SECONDS = meter.create_histogram(
    PETROSA_CIO_LLM_LATENCY_NAME,
    description="CIO LLM call latency in seconds",
    unit="s",
)


class SummaryLog:
    """Bounded, process-local aggregation for the five-minute SUMMARY record."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._started = clock()
        self._decisions: Counter[str] = Counter()
        self._llm_calls: Counter[str] = Counter()
        self._latencies: list[float] = []
        self._lock = Lock()

    def decision(self, action: str, reason: str, outcome: str) -> None:
        with self._lock:
            self._decisions[f"{action}:{reason}:{outcome}"] += 1

    def llm_call(self, outcome: str, latency_seconds: float) -> None:
        with self._lock:
            self._llm_calls[outcome] += 1
            self._latencies.append(max(0.0, latency_seconds))

    def emit(self, *, force: bool = False) -> dict[str, object] | None:
        with self._lock:
            window = self._clock() - self._started
            if not force and window < 300:
                return None
            latencies = sorted(self._latencies)
            summary: dict[str, object] = {
                "event": "SUMMARY",
                "window_seconds": 300,
                "service": "petrosa-cio",
                "decisions": dict(self._decisions),
                "llm_calls": dict(self._llm_calls),
                "latency_seconds": {
                    "p50": _percentile(latencies, 0.50),
                    "p95": _percentile(latencies, 0.95),
                },
            }
            self._started = self._clock()
            self._decisions.clear()
            self._llm_calls.clear()
            self._latencies.clear()
        logger.info(json.dumps(summary, separators=(",", ":")))
        return summary


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    return (
        round(
            statistics.quantiles(values, n=100, method="inclusive")[
                int(percentile * 100) - 1
            ],
            6,
        )
        if len(values) > 1
        else values[0]
    )


SUMMARY = SummaryLog()

# LLM Performance Metrics
LLM_LATENCY = meter.create_histogram(
    "cio_llm_latency_seconds",
    description="Latency of LLM calls in seconds",
    unit="s",
)

LLM_TOKENS = meter.create_counter(
    "cio_llm_tokens_total",
    description="Total number of tokens used",
)

LLM_CALLS = meter.create_counter(
    "cio_llm_calls_total",
    description="Total number of LLM calls by route and outcome",
)

# CIO Decision Metrics
DECISION_ACTIONS = meter.create_counter(
    "cio_decision_actions_total",
    description="Total number of final decisions by action type",
)

NON_ACTIONABLE_INTENTS = meter.create_counter(
    "cio_non_actionable_intents_total",
    description="Trade intents skipped before reasoning because their token is non-actionable",
)

# LLM Validation Failures
LLM_VALIDATION_FAILURES = meter.create_counter(
    "cio_llm_validation_failures_total",
    description="Total number of LLM response schema validation failures",
)

LLM_FALLBACK_SKIPS = meter.create_counter(
    "cio_llm_fallback_skips_total",
    description="Total SAFE_DEFAULT fallbacks that force SKIP-like behavior",
)

# #187 — recovered responses: the raw LLM content failed strict
# model_validate_json() (prose-wrapped JSON and/or an over-length string
# field vs. the prompt's stated char budget) but was salvaged by
# brace-extraction + string-field clamping, avoiding a SAFE_DEFAULTS(SKIP)
# fallback that a structurally sound decision did not warrant.
LLM_RESPONSE_RECOVERED = meter.create_counter(
    "cio_llm_response_recovered_total",
    description=(
        "Total LLM responses salvaged via brace-extraction/field-clamping "
        "after failing strict schema validation on first pass"
    ),
)

# Risk-Gate Provenance Metrics (#172) — distinguishes a hard block caused by
# a context-fetch failure (ContextBuilder fell back to conservative safe
# defaults) from a hard block backed by real, live portfolio/risk data.
# Without this split, a `/state` fetch outage is indistinguishable in
# monitoring from a genuine drawdown/order-limit breach.
RISK_GATE_CONTEXT_FALLBACK = meter.create_counter(
    "cio_risk_gate_context_fallback_total",
    description=(
        "Risk-gate hard blocks caused by portfolio/risk context-fetch "
        "fallback defaults, not a real risk breach"
    ),
)

RISK_GATE_REAL_BREACH = meter.create_counter(
    "cio_risk_gate_real_breach_total",
    description="Risk-gate hard blocks backed by live portfolio/risk data",
)

# #189 — the model self-reporting its own documented input-contract sentinel
# (ABSOLUTE RULE 4 in every CIO prompt: `{"error": "MISSING_INPUT"}` when
# required fields are absent) is NOT a schema parse failure — it is the model
# behaving exactly as instructed. Pre-#189 this was indistinguishable from a
# genuinely malformed/undisciplined response and inflated
# cio_llm_fallback_skips_total / LLM_PARSE_FAILURE_SKIP with a condition that
# a fallback-model retry can never fix (same incomplete context, same model,
# same self-reported error). Tracked separately so on-call can tell "context
# builder is producing incomplete input" apart from "model output is
# malformed".
LLM_MISSING_INPUT_SKIPS = meter.create_counter(
    "cio_llm_missing_input_skips_total",
    description=(
        "Total SAFE_DEFAULT fallbacks triggered by the model's own "
        "self-reported MISSING_INPUT contract sentinel, not a schema "
        "parse failure"
    ),
)

LLM_UNAVAILABLE_DECISIONS = meter.create_counter(
    "cio_llm_unavailable_decisions_total",
    description="Decisions forced by an LLM outage (a persona stage returned SAFE_DEFAULTS)",
)

AUTO_RESUME_EVENTS = meter.create_counter(
    "cio_auto_resume_events_total",
    description="Auto-resume outcomes for LLM_UNAVAILABLE pauses (attribute: result)",
)

LLM_HEALTH_PROBES = meter.create_counter(
    "cio_llm_health_probes_total",
    description="Active LLM health probes sent by the auto-resume loop (attribute: result)",
)
