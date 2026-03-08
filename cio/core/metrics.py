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
