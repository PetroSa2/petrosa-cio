"""Run the real CIO producer path against the pinned consumer contract."""

from __future__ import annotations

import importlib.metadata
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).parent / "unit"))

from petrosa_contracts import Signal  # noqa: E402
from test_leverage_signal_propagation import (  # noqa: E402
    _make_context,
    _make_decision,
)

from cio.output.translator import TradeEngineTranslator  # noqa: E402

CONTRACT_VERSION = "0.1.0"


def _payload() -> dict[str, object]:
    payload = TradeEngineTranslator.to_legacy_signal(_make_context(), _make_decision())
    assert payload is not None
    return payload


def test_real_producer_payload_matches_pinned_consumer_contract():
    assert importlib.metadata.version("petrosa-contracts") == CONTRACT_VERSION
    signal = Signal.model_validate(_payload())
    assert signal.symbol == "BTCUSDT"
    assert signal.action == "buy"


def test_producer_only_field_is_rejected_as_extra_forbidden():
    with pytest.raises(ValidationError) as raised:
        Signal.model_validate({**_payload(), "producer_only_regression": True})

    assert any(error["type"] == "extra_forbidden" for error in raised.value.errors())


def test_missing_consumer_required_field_is_rejected():
    payload = _payload()
    payload.pop("symbol")
    with pytest.raises(ValidationError) as raised:
        Signal.model_validate(payload)

    assert any(error["type"] == "missing" for error in raised.value.errors())


def test_low_confidence_cio_execute_meets_tradeengine_trust_condition():
    """A routed CIO execute at confidence 0.65 is accepted, not dropped as "LLM rejected".

    tradeengine (``LLMProcessor.process``) skips its placeholder confidence > 0.7 gate only when
    ``source == "petrosa-cio"`` and ``llm_reasoning`` is set (PetroSa2/petrosa-tradeengine#807).
    """
    context = _make_context(
        trigger_payload={
            "symbol": "BTCUSDT",
            "side": "long",
            "current_price": 50000.0,
            "confidence": 0.65,
        }
    )
    payload = TradeEngineTranslator.to_legacy_signal(context, _make_decision())
    assert payload is not None
    signal = Signal.model_validate(payload)

    assert signal.confidence == 0.65
    assert signal.strategy_mode == "llm_reasoning"
    assert signal.source == "petrosa-cio" and signal.llm_reasoning
