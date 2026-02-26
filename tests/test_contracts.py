"""
Unit tests for contracts.
"""

from datetime import datetime

from contracts.signal import Signal, SignalStrength, StrategyMode


def test_signal_creation():
    signal_data = {
        "strategy_id": "test_strat",
        "symbol": "BTCUSDT",
        "action": "buy",
        "confidence": 0.8,
        "price": 50000.0,
        "quantity": 0.1,
        "current_price": 49900.0,
        "source": "test_source",
        "strategy": "test_strategy",
    }

    signal = Signal(**signal_data)
    assert signal.symbol == "BTCUSDT"
    assert signal.action == "buy"
    assert signal.confidence == 0.8
    assert isinstance(signal.timestamp, datetime)
    assert signal.strategy_mode == StrategyMode.DETERMINISTIC
    assert signal.strength == SignalStrength.MEDIUM


def test_signal_invalid_confidence():
    import pytest
    from pydantic import ValidationError

    signal_data = {
        "strategy_id": "test_strat",
        "symbol": "BTCUSDT",
        "action": "buy",
        "confidence": 1.5,  # Invalid
        "price": 50000.0,
        "quantity": 0.1,
        "current_price": 49900.0,
        "source": "test_source",
        "strategy": "test_strategy",
    }

    with pytest.raises(ValidationError):
        Signal(**signal_data)
