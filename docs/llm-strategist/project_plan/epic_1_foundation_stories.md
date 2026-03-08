# Epic 1: The Foundation - Finalized Stories

**Epic Goal:** Establish a robust, provider-agnostic LLM infrastructure for the `petrosa-cio` service that supports both Gemini and OpenAI, with a focus on a behaviorally honest mocking layer for zero-cost development and deterministic testing.

**Source Documents:**
- `../investigations/001-llm-infrastructure.md`
- `../investigations/004-llm-prompt-guide-reference.md`
- `../decisions/epic_1_grooming_notes.md`

---

## Stories for Epic 1: The Foundation

### S0: Define Core Pydantic Domain Models

*   **Description:** Create Pydantic models for `CodeEngineResult`, `DecisionResult`, `RegimeResult`, `StrategyResult`, `AppliedParamChange`, and other framework structs identified in the documentation (`004-llm-prompt-guide-reference.md`). Include field validation, enum constraints, and null handling.
*   **Acceptance Criteria:**
    *   All core framework data structures are defined as Pydantic models with appropriate typing and validation.
    *   Models align with definitions in `004-llm-prompt-guide-reference.md`.
    *   Safe defaults for each `prompt_id` are defined in the domain model layer and exported as a constant (`SAFE_DEFAULTS: dict[str, BaseModel]`).
    *   S0 defines a mapping from `prompt_id` strings (e.g., 'REGIME_CLASSIFIER') to their corresponding Pydantic `response_model` classes (e.g., `RegimeResult`), stored as a constant.

### S1: Implement `CIO_LLM_Client` Base Structure & Factory

*   **Description:** Create the `CIO_LLM_Client` abstract base class defining `complete`, `complete_with_schema`, `get_cached`. Implement a `ClientFactory.create()` method that selects between `LiteLLMClient` and `MockLLMClient` based on `LLM_PROVIDER`, ensuring the check is centralized.
*   **Acceptance Criteria:**
    *   Base interfaces defined and correctly typed.
    *   `ClientFactory` correctly returns mock/real client.
    *   `LLM_PROVIDER` check is centralized and not duplicated.
    *   `complete()` never raises on LLM transport errors — it returns a `RawLLMResponse` with `error: str | None` populated. All exceptions are caught at the `complete()` boundary.
    *   `complete_with_schema()` takes a `response_model: type[BaseModel]` argument and uses it to deserialize and validate the LLM response.
    *   `complete_with_schema()` returns the safe default if `raw.error` is present or if Pydantic `ValidationError` occurs.
    *   `RawLLMResponse` includes `input_tokens: int`, `output_tokens: int`, `cached_tokens: int`.

### S2: Develop `MockLLMClient` with Prompt Validation & Fixture Routing

*   **Description:** Implement `MockLLMClient` conforming to `CIO_LLM_Client` interface. This includes loading prompt YAML files (located at `apps/strategist/prompts/`) at initialization, validating the incoming prompt's structure against the `required_context_fields` specified in the corresponding YAML file, and routing to appropriate static fixtures or generating parse failures if malformed.
*   **Acceptance Criteria:**
    *   `MockLLMClient` loads prompt YAMLs correctly.
    *   `MockLLMClient` validates incoming context against `required_context_fields` from YAML.
    *   Malformed prompts trigger parse failures (returning safe defaults).
    *   Correctly formed prompts return static mock responses based on `prompt_id`.

### S3: Implement Scenario-Based Response Routing in `MockLLMClient`

*   **Description:** Enhance `MockLLMClient` to route responses based on specific input context patterns. Implement the following minimum viable scenario set:
    | Input condition | Expected output |
    |---|---|
    | `hard_blocked = true` | `action = "block"` |
    | `win_rate_delta <= -0.30` | `health = "failing"`, `activation_recommendation = "pause"` |
    | `win_rate_delta` between `-0.30` and `-0.10` | `health = "degraded"`, `activation_recommendation = "reduce"` |
    | `volatility_percentile > 85` and `trend_strength < 0.60` | `regime = "high_volatility"`, `regime_confidence = "high"` |
    | `ev_passes = false` | `action = "skip"` |
    | `regime_confidence = "low"` | `action = "skip"` |
    | `strategy_health = "healthy"`, `regime_fit = "good"`, `regime_confidence = "high"`, `ev_passes = true` | `action = "execute"` |
*   **Acceptance Criteria:**
    *   Given specific input contexts from the table above, `complete_with_schema` returns predefined, behaviorally honest mock `StrategyResult`, `RegimeResult` or `DecisionResult` objects that simulate the framework's logic.

### S4: Simulate Parse Failures & Test Safe Defaults in `MockLLMClient`

*   **Description:** Add a mechanism (e.g., `LLM_MOCK_FAIL_RATE` env var or special header) to `MockLLMClient` to deterministically simulate LLM parse failures. Implement test cases verifying that when a parse failure is triggered, `complete_with_schema` returns the prompt's registered safe default, logs the failure with `prompt_id` and raw response, and does not raise an exception.
*   **Acceptance Criteria:**
    *   `MockLLMClient` can simulate parse failures.
    *   When parse failure is triggered, `complete_with_schema` returns the prompt's registered safe default, logs the failure with `prompt_id` and raw response, and does not raise an exception.

### S5: Implement Cache Interface and Key Format in `CIO_LLM_Client`

*   **Description:** Define and implement `get_cached()` and `put_cached()` methods within the `CIO_LLM_Client` (and its `MockLLMClient` implementation). Establish standardized cache key formats like `regime:{asset_class}:{session_key}` and `strategy:{strategy_id}:{regime}`. The `MockLLMClient` must be stateful across calls within a single test session, utilizing a pre-seedable fixture dictionary to simulate cache hits and misses.
*   **Acceptance Criteria:**
    *   `get_cached` and `put_cached` methods are available and functional.
    *   Cache keys are standardized.
    *   `MockLLMClient` maintains state to simulate cache hits/misses for testing purposes.
    *   A concrete API for seeding the mock's cache is defined, e.g., `mock_client.seed_cache(cache_key: str, data: Any)`.

---
