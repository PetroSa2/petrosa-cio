"""petrosa-cio#418: the action classifier must not read a regime or characterization gap as a veto."""

from pathlib import Path

import yaml

from cio.prompts.context_contract import validate_prompt
from cio.prompts.loader import select_system_prompt

PROMPT = (
    Path(__file__).resolve().parents[2]
    / "cio"
    / "prompts"
    / "action_classifier_v1.yaml"
)


def _standard_prompt() -> str:
    """The live (``standard``) system prompt with whitespace collapsed, so line wrapping is irrelevant."""
    text = select_system_prompt(yaml.safe_load(PROMPT.read_text()), "standard")
    return " ".join(text.split())


def test_regime_gap_is_priced_in_at_probe_size_not_a_veto():
    prompt = _standard_prompt()
    for reason in (
        "regime_low_confidence",
        "regime_stale",
        "regime_missing",
        "regime_age_unknown",
    ):
        assert reason in prompt
    assert "A regime gap in pre_decision_context gaps" in prompt
    assert "already limits the order to probe size" in prompt
    assert "Such a gap alone is not a reason to skip, block or pause." in prompt


def test_characterization_without_revision_is_not_a_skip_reason_and_fetch_failure_is_a_gap():
    prompt = _standard_prompt()
    assert (
        "characterization null with characterization_available=true means the intent carries no strategy revision"
        in prompt
    )
    assert "It alone is not a reason to skip." in prompt
    assert (
        "characterization_available=false means the characterization lookup failed"
        in prompt
    )
    assert "Weigh it as a data gap with the other evidence." in prompt


def test_gap_policy_keeps_the_prompt_context_contract():
    data = yaml.safe_load(PROMPT.read_text())
    validate_prompt(data["system_prompt"])
    assert data["version"] != "1.0.0"
