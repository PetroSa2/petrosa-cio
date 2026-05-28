# Prompt-Context Contract (P1.4-AC3 / FR55-FR58)

## Why this contract exists

CIO's reasoning prompt is the bridge between the typed `PreDecisionContext`
bundle (assembled by `cio/core/context_builder.py::build_pre_decision_context`)
and the LLM that arbitrates the final action. If a prompt revision silently
drops a context surface, the LLM loses the grounding for an entire FR — and
the symptom (degraded decisions) is invisible until a postmortem.

The contract closes that hole with two gates:

- **CI gate** (`tests/unit/test_prompt_contract.py`) — every PR that touches
  the prompt template must keep all four surfaces.
- **Runtime gate** (`cio/main.py::_enforce_prompt_context_contract`) — the
  service refuses to start if the active prompt template is missing a
  surface; a bad deploy fails closed at boot.

## The contract surface set

`REQUIRED_CONTEXT_SURFACES` in `cio/prompts/context_contract.py` enumerates
the four surfaces the reasoning prompt must reference, one per the FRs that
introduced them:

| Surface              | FR    | Source field on `PreDecisionContext` |
|----------------------|-------|--------------------------------------|
| `market_state`       | FR55  | `market_state` + `market_state_available` |
| `portfolio_state`    | FR56  | `portfolio_state` + `portfolio_state_available` |
| `evaluator_verdicts` | FR57  | `evaluator_verdicts` + `evaluator_verdicts_available` |
| `characterization`   | FR58  | `characterization` + `characterization_available` |

`validate_prompt(prompt_template: str) -> None` asserts each surface appears
in the template as the literal placeholder `{surface_name}`. The placeholder
is documentary: the prompt is delivered to the LLM verbatim, not Python-
formatted. The marker exists so revisions surface the surface contract to the
reader (and to the validators) without depending on string interpolation.

## How to extend the contract

When a new FR adds a context surface to `PreDecisionContext`, evolve the
contract in lockstep:

1. **Extend `PreDecisionContext`** in `cio/models/context.py` with the new
   typed field (and its `*_available` boolean if the surface can degrade).
2. **Add the surface name** to `REQUIRED_CONTEXT_SURFACES` in
   `cio/prompts/context_contract.py`.
3. **Update the prompt template** at
   `cio/prompts/action_classifier_v1.yaml::system_prompt` — add a new line
   under the `PRE-DECISION CONTEXT SURFACES` block:
   ```
   - {new_surface}        — <one-line description> (FR<n>)
   ```
4. **Update the test parametrization** in
   `tests/unit/test_prompt_contract.py` if you assert on the exact set of
   surfaces (`test_required_context_surfaces_are_the_four_fr_surfaces`).
5. **Re-run the suite locally** with `pytest tests/unit/test_prompt_contract.py
   -q`. The CI gate runs the same checks in the `ci-checks` workflow.

## Failure modes the contract catches

- A copy-paste revision that drops one of the four bullets from the YAML.
- A wholesale rewrite of the prompt that forgets to re-list the surfaces.
- A typo in a placeholder name (e.g. `{market}` instead of `{market_state}`).

## Failure modes the contract does **not** catch

- The prompt mentions a surface but the LLM ignores it — that's a behavioural
  drift, tracked separately under the evaluator subsystem (FR17/FR23).
- The `PreDecisionContext` is assembled but the surface is `None` because of
  an upstream outage — that's the missing-context handling story (AC2,
  shipped earlier in this epic).

## References

- Parent epic: [petrosa-cio#122](https://github.com/PetroSa2/petrosa-cio/issues/122)
- This story: [petrosa-cio#133](https://github.com/PetroSa2/petrosa-cio/issues/133)
- Sibling stories: AC1 PreDecisionContext bundle (#131 → #142), AC2
  missing-context handling (#132 → #143).
- PRD: FR55 / FR56 / FR57 / FR58.
