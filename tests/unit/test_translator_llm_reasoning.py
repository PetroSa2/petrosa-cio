"""CIO's routed signals always carry top-level llm_reasoning (PetroSa2/petrosa-tradeengine#807)."""

from __future__ import annotations

from petrosa_contracts import Signal
from test_leverage_signal_propagation import _make_context, _make_decision

from cio.models.decision import LLM_UNAVAILABLE_TRACE
from cio.output.translator import (
    DETERMINISTIC_ROUTING_REASONING,
    LLM_REASONING_MAX_CHARS,
    TradeEngineTranslator,
    routed_llm_reasoning,
)


def test_execute_carries_justification_and_trace_as_llm_reasoning():
    decision = _make_decision()
    decision.justification = "Breakout confirmed with healthy stats"
    decision.thought_trace = "regime ok; ev ok"

    payload = TradeEngineTranslator.to_legacy_signal(_make_context(), decision)

    assert payload is not None
    assert payload["llm_reasoning"] == (
        "Breakout confirmed with healthy stats | trace: regime ok; ev ok"
    )
    Signal.model_validate(payload)


def test_llm_unavailable_execute_carries_explicit_deterministic_reasoning():
    decision = _make_decision()
    decision.justification = (
        "LLM_UNAVAILABLE: PETROSA_PROMPT_REGIME_CLASSIFIER unavailable; "
        "deterministic gates passed; executing at probe size"
    )
    decision.thought_trace = LLM_UNAVAILABLE_TRACE

    reasoning = routed_llm_reasoning(decision)

    assert reasoning.startswith(DETERMINISTIC_ROUTING_REASONING)
    assert "PETROSA_PROMPT_REGIME_CLASSIFIER" in reasoning


def test_reasoning_is_never_empty_and_is_bounded():
    decision = _make_decision()
    decision.justification = None
    decision.thought_trace = None
    assert routed_llm_reasoning(decision) == DETERMINISTIC_ROUTING_REASONING

    decision.justification = "x" * 5000
    decision.thought_trace = "y"
    assert len(routed_llm_reasoning(decision)) == LLM_REASONING_MAX_CHARS
