# Prompt Engineering & Global Rules

**Source:** `../investigations/004-llm-prompt-guide-reference.md`

## 1. Global Rules

All LLM prompts in the PETROSA framework must adhere to these five non-negotiable rules. They are prepended to every LLM call to ensure consistency and prevent hallucination.

**`PETROSA_BASE_v1.0`**

```
You are a component of PETROSA, an autonomous trading system.

ABSOLUTE RULES:
1. Respond with ONLY a JSON object. No text before or after the JSON.
2. Never invent numbers. If a number is not in your input, output null.
3. All enum fields must exactly match the allowed values listed in your prompt.
4. If required input data is missing, output: {"error": "MISSING_INPUT"}
5. thought_trace: 1 sentence, max 120 characters, must reference a specific
   field name and value from your input.
```

## 2. Prompt Structure

Each persona (Regime Classifier, Strategy Assessor, Action Classifier) follows a standardized prompt structure:

1.  **System Prompt:** Defines the persona's role, the absolute rules, and the specific classification logic (decision tree).
2.  **Input Data:** A compressed, sanitized JSON object containing only the necessary context.
    *   *Regime Classifier:* `signal_summary` (trend, volatility, price action).
    *   *Strategy Assessor:* `strategy_doc` (fit notes, params), `health_signals` (win rate delta), `current_regime`.
    *   *Action Classifier:* `decision_result` (pre-computed flags from Code Engine + previous LLM outputs).
3.  **Output Schema:** A strict JSON schema defining the required fields and allowed enum values.

## 3. Persona Prompts

### Regime Classifier

**Goal:** Classify market regime from compressed signal summary.
**Input:** `signal_summary` (trend_strength, volatility_percentile, price_action_character, etc.)
**Output:** `regime` (enum), `regime_confidence` (high/medium/low), `primary_signal`, `thought_trace`.
**Key Logic:** Decision tree based on volatility and trend strength thresholds.

### Strategy Assessor

**Goal:** Assess strategy health and regime fit.
**Input:** `strategy_doc`, `health_signals`, `current_regime`.
**Output:** `health` (healthy/degraded/failing), `regime_fit` (good/neutral/poor), `activation_recommendation` (run/reduce/pause), `param_change` (optional signal).
**Key Logic:** Health driven by win rate delta and consecutive losses. Regime fit driven by `strategy_doc` lookup.

### Action Classifier

**Goal:** Synthesize flags into a final action.
**Input:** `decision_result` (hard_blocked, ev_passes, cost_viable, regime_confidence, etc.).
**Output:** `action` (execute/modify_params/skip/block/pause_strategy/escalate), `justification`, `thought_trace`.
**Key Logic:** Ordered rule set (1-8). Hard blocks first, then EV/Cost checks, then regime confidence, then activation recommendation.

## 4. Prompt Versioning

Prompts are versioned in Git like code.

*   **Location:** `apps/strategist/prompts/`
*   **File Format:** YAML (`.yaml`) containing metadata + prompt text.
*   **Input Validation:** `required_context_fields` list in YAML.
*   **Output Validation:** `response_model` mapped to Pydantic class in `apps/strategist/models/`.

**Policy:**
*   Patch version bump (1.0 -> 1.0.1) for wording changes.
*   Minor version bump (1.0 -> 1.1) for new enum values or logic rules.
*   Major version bump (1.0 -> 2.0) for logic overhauls.
