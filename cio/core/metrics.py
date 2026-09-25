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
