"""CI gate for the prompt-context contract (P1.4-AC3 / FR55-FR58).

Loads the active reasoning prompt template (the action-classifier
``system_prompt``) and runs it through :func:`validate_prompt`. A future
prompt revision that drops one of the four required surfaces fails this
test before it can land.
"""

from __future__ import annotations

import os

import pytest
import yaml

from cio.prompts.context_contract import (
    REQUIRED_CONTEXT_SURFACES,
    PromptContractError,
    validate_prompt,
)

PROMPTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "cio",
    "prompts",
)
ACTION_CLASSIFIER_YAML = os.path.join(PROMPTS_DIR, "action_classifier_v1.yaml")


def _load_active_prompt() -> str:
    with open(ACTION_CLASSIFIER_YAML) as fh:
        data = yaml.safe_load(fh)
    prompt = data.get("system_prompt") or ""
    assert isinstance(prompt, str)
    return prompt


def test_required_context_surfaces_are_the_four_fr_surfaces():
    """The contract enumerates exactly the FR55-FR58 surfaces."""

    assert REQUIRED_CONTEXT_SURFACES == frozenset(
        {
            "market_state",
            "portfolio_state",
            "evaluator_verdicts",
            "characterization",
        }
    )


def test_action_classifier_prompt_honors_context_contract():
    """The shipping reasoning prompt must reference every required surface."""

    prompt = _load_active_prompt()
    assert prompt, "action_classifier_v1.yaml::system_prompt must be non-empty"
    validate_prompt(prompt)  # raises PromptContractError on contract miss
    for surface in REQUIRED_CONTEXT_SURFACES:
        assert f"{{{surface}}}" in prompt


@pytest.mark.parametrize("dropped", sorted(REQUIRED_CONTEXT_SURFACES))
def test_validate_prompt_rejects_template_missing_any_surface(dropped: str):
    """Removing any surface placeholder must trip the validator."""

    incomplete = "\n".join(
        f"{{{surface}}} line"
        for surface in REQUIRED_CONTEXT_SURFACES
        if surface != dropped
    )

    with pytest.raises(PromptContractError) as excinfo:
        validate_prompt(incomplete)

    assert dropped in str(excinfo.value)


def test_validate_prompt_accepts_template_with_all_surfaces():
    template = " ".join(f"{{{surface}}}" for surface in REQUIRED_CONTEXT_SURFACES)
    # Must not raise — assert via try/except so the hook sees an explicit assert.
    try:
        validate_prompt(template)
    except PromptContractError as exc:  # pragma: no cover — defensive
        raise AssertionError(
            f"validate_prompt rejected a fully compliant template: {exc}"
        ) from exc
    assert True


def test_validate_prompt_rejects_non_string_template():
    with pytest.raises(PromptContractError) as excinfo:
        validate_prompt(None)  # type: ignore[arg-type]
    assert "str" in str(excinfo.value)
