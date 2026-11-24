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
    return select_system_prompt(yaml.safe_load(PROMPT.read_text()), "standard")


def test_regime_gap_is_priced_in_at_probe_size_not_a_veto():
    prompt = _standard_prompt()
    for reason in (
        "regime_low_confidence",
        "regime_stale",
        "regime_missing",
        "regime_age_unknown",
    ):
        assert reason in prompt
    assert "probe size" in prompt
    assert "A regime gap alone is not a reason to skip, block or pause." in prompt


def test_absent_characterization_alone_is_not_a_skip_reason():
    prompt = _standard_prompt()
    assert "characterization_available=false" in prompt
    assert "It alone is not a reason to skip." in prompt


def test_gap_policy_keeps_the_prompt_context_contract():
    data = yaml.safe_load(PROMPT.read_text())
    validate_prompt(data["system_prompt"])
    assert data["version"] == "1.1.0"
