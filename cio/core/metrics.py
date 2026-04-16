from prometheus_client import Counter, Histogram

# LLM Performance Metrics
LLM_LATENCY = Histogram(
    "cio_llm_latency_seconds", "Latency of LLM calls in seconds", ["prompt_id", "model"]
)

LLM_TOKENS = Counter(
    "cio_llm_tokens_total",
    "Total number of tokens used",
    ["prompt_id", "model", "token_type"],
)

# CIO Decision Metrics
DECISION_ACTIONS = Counter(
    "cio_decision_actions_total",
    "Total number of final decisions by action type",
    ["action_type", "strategy_id"],
)

# LLM Validation Failures
LLM_VALIDATION_FAILURES = Counter(
    "cio_llm_validation_failures_total",
    "Total number of LLM response schema validation failures",
    ["prompt_id", "model"],
)

LLM_FALLBACK_SKIPS = Counter(
    "cio_llm_fallback_skips_total",
    "Total SAFE_DEFAULT fallbacks that force SKIP-like behavior",
    ["prompt_id", "reason"],
)
