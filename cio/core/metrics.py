from opentelemetry import metrics

meter = metrics.get_meter("cio")

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

# CIO Decision Metrics
DECISION_ACTIONS = meter.create_counter(
    "cio_decision_actions_total",
    description="Total number of final decisions by action type",
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
