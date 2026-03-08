# Epic 1: The Foundation - Grooming Notes

**Date:** 2026-03-08 (Session Date)

## Overview:

This document summarizes the detailed grooming session for **Epic 1: The Foundation**, following its approval during the Party Mode Kickoff. The session focused on refining stories based on feedback from Yurisa2, addressing potential ambiguities, and establishing clear definitions of done.

## Key Refinements and Discussions:

### 1. Merging S2 and S3 (Original numbering)

-   **Decision:** Original S2 (`MockLLMClient` with static fixtures) and S3 (`MockLLMClient` with prompt validation) were merged into a single, refined **S2**.
-   **Rationale:** To avoid throwaway work and ensure the `MockLLMClient` is behaviorally honest from the start, validating prompt structure from the beginning.

### 2. Splitting S6 (Original numbering)

-   **Decision:** Original S6 (Caching) was split into two stories: **S5a** (Cache Interface and Key Format) and **S5b** (Cache TTL and Invalidation Triggers).
-   **Rationale:** **S5a** is a blocker for early persona development, while **S5b** can be deferred to a later epic or hardening sprint, focusing on immediate needs first.

### 3. Adding Missing Story: S0 (Core Pydantic Models)

-   **Decision:** A new **S0: Define Core Pydantic Domain Models** was introduced as a prerequisite.
-   **Rationale:** The framework's core data structures (`CodeEngineResult`, `DecisionResult`, etc.) need to exist as validated Pydantic models before dependent stories can proceed effectively.

### 4. Tightened Acceptance Criteria (ACs)

-   **S3 (Scenario-Based Routing):** ACs were made more specific, including concrete examples of input conditions and expected outputs (e.g., `hard_blocked = true` → `action = "block"`). A happy-path `action = "execute"` scenario was added.
-   **S4 (Parse Failures):** ACs explicitly require testing that the validator returns safe defaults on parse failure and logs the event without raising exceptions.

### 5. `MockLLMClient` Stateful Confirmation

-   **Question:** Does `MockLLMClient` need to be stateful between calls within a single test?
-   **Decision:** Yes, the `MockLLMClient` **must be stateful across calls within a single test session, utilizing a pre-seedable fixture dictionary** to accurately simulate cache hits and misses for testing caching logic. This requirement is explicitly captured in **S5**.

### 6. Prompt YAML Files and Schema Ownership

-   **Question:** Who owns the prompt YAML files and where do they live? Where do their validation schemas come from?
-   **Decision:**
    *   **Location:** `apps/strategist/prompts/` within the `petrosa-cio` project.
    *   **Ownership:** CIO/strategist team, versioned in Git.
    *   **Output Validation (Layer 1):** Uses Pydantic models from **S0** as the authoritative source. `complete_with_schema()` in **S1** will use a `response_model: type[BaseModel]` argument for deserialization and validation.
    *   **Input Structure Validation (Layer 2):** Uses a `required_context_fields` list defined within each prompt YAML file. The `MockLLMClient` (in **S2**) will read this metadata to validate incoming prompts.
-   **S0 Update:** The scope of **S0** was expanded to include a mapping from `prompt_id` to its corresponding Pydantic `response_model` class.

## Revised Story Order for Epic 1:

The final, groomed story list is documented in `../project_plan/epic_1_foundation_stories.md`.

## Definition of Done for Epic 1:

-   `ClientFactory.create()` returns `MockLLMClient` when `LLM_PROVIDER=mock`, `LiteLLMClient` otherwise.
-   All specified scenario cases in **S3** have passing tests.
-   A test exists that triggers a parse failure and asserts the safe default is returned with no exception raised.
-   `RawLLMResponse` includes token counts and the mock returns plausible values.
-   `SAFE_DEFAULTS` is defined once in **S0** and imported by both the validator and the mock.
-   Zero hardcoded strings for prompt IDs, cache key formats, or enum values — everything references the Pydantic models from **S0**.

## Related Documents:

-   `party_mode_kickoff.md`
-   `../project_plan/epic_1_foundation_stories.md`
-   `../investigations/001-llm-infrastructure.md`
-   `../investigations/004-llm-prompt-guide-reference.md`
