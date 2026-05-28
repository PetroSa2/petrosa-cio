"""Prompt-context contract (P1.4-AC3, FR55-FR58).

Enforces that any reasoning prompt template references each of the four
context surfaces produced by ``cio.models.context.PreDecisionContext``.
Validation is invoked at CI time (``tests/unit/test_prompt_contract.py``)
and at runtime startup (``cio/main.py``). See
``cio/docs/prompt-context-contract.md`` for the contract narrative and the
extension procedure when a new FR adds a surface.
"""

from __future__ import annotations

REQUIRED_CONTEXT_SURFACES: frozenset[str] = frozenset(
    {
        "market_state",
        "portfolio_state",
        "evaluator_verdicts",
        "characterization",
    }
)


class PromptContractError(ValueError):
    """Raised when a prompt template fails the context-surface contract."""


def validate_prompt(prompt_template: str) -> None:
    """Assert that ``prompt_template`` references every required surface.

    Each surface in :data:`REQUIRED_CONTEXT_SURFACES` must appear at least
    once in the template as the literal placeholder ``{surface_name}``.
    The placeholder is documentary — the prompt is delivered to the LLM
    verbatim, not Python-formatted — so the contract only inspects for
    the textual marker.
    """

    if not isinstance(prompt_template, str):
        raise PromptContractError(
            f"prompt_template must be str, got {type(prompt_template).__name__}"
        )

    missing = sorted(
        surface
        for surface in REQUIRED_CONTEXT_SURFACES
        if f"{{{surface}}}" not in prompt_template
    )
    if missing:
        raise PromptContractError(
            "Prompt-context contract violation: prompt template is missing "
            f"placeholder(s) for required surface(s): {missing}. "
            "Each of REQUIRED_CONTEXT_SURFACES must appear as '{surface_name}' "
            "(see cio/docs/prompt-context-contract.md)."
        )
