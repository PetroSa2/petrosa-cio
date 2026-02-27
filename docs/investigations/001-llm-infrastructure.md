# Investigation: LLM Infrastructure for Sovereign Governance

- **Status:** Proposed
- **Priority:** High
- **Owner:** John (PM) / Winston (Architect)
- **Target Component:** `petrosa-cio` (Nurse/Strategist)

## 🎯 Objective
Establish a robust, provider-agnostic LLM infrastructure for the `petrosa-cio` service that supports both **Gemini** and **OpenAI**. The primary goal is to enable complex reasoning for governance policy enforcement and strategist commands while ensuring zero-cost development through a standardized mocking layer.

## 💡 Background
The `petrosa-cio` service acts as the sovereign gatekeeper for Petrosa. Current operations are deterministic (e.g., `NurseEnforcer`). To scale governance, we need LLM capabilities to:
1. Distill complex alert streams (`AlertDistiller`).
2. Reason over intent payloads against high-level policy.
3. Power the MCP-compatible strategist server for dynamic configuration and semantic memory retrieval.

## 🛠 Proposed Tech Stack
- **Gateway Abstraction:** `litellm` - Provides a unified OpenAI-style API for 100+ providers (including Gemini and OpenAI).
- **Structured Reasoning:** `instructor` - Enhances LLM clients with Pydantic validation to ensure outputs match our internal contracts (`EnforcerResult`, tool calls).
- **Mocking Strategy:** Use `litellm`'s built-in `mock_response` and file-based fixtures for deterministic testing without API costs.

## 📋 Tasks

### 1. Provider Abstraction (Winston)
- [ ] Prototype a `CIO_LLM_Client` using `litellm`.
- [ ] Implement seamless switching via `LLM_PROVIDER` environment variable.
- [ ] Verify Gemini-specific features (e.g., long context) vs. OpenAI features.

### 2. Structured Command Reasoning (Mary)
- [ ] Define Pydantic models for "CIO Commands" (e.g., `GovernanceDecision`, `StrategyAdjustment`).
- [ ] Use `instructor` to map LLM outputs to these models.
- [ ] Enforce "Thought Trace" requirements (min 100 chars) as seen in current `MCPServer`.

### 3. Mocking & Developer Experience (Amelia)
- [ ] Create a `tests/fixtures/llm_responses/` directory for static mock data.
- [ ] Implement a `MockLLMClient` that loads these fixtures based on the prompt/model.
- [ ] Ensure `pytest` suites run 100% locally with `LLM_PROVIDER=mock`.

### 4. Integration Analysis (Mary/John)
- [ ] Evaluate cost-per-reasoning-step for Gemini-Pro vs. GPT-4o-mini for `petrosa-cio` tasks.
- [ ] Assess latency impact on the `NurseEnforcer` pipeline.

## 🧪 Success Criteria
- [ ] `petrosa-cio` can switch between Gemini and OpenAI with a single config change.
- [ ] Developers can run the full suite of LLM-dependent tests without an API key.
- [ ] LLM outputs are strictly validated against Pydantic models before being used in governance decisions.
