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
