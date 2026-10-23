import pytest

from cio.core.pause_payload import (
    build_ta_bot_pause_payload,
    build_ta_bot_resume_payload,
)


@pytest.mark.parametrize(
    "strategy_id",
    [
        "ichimoku_cloud_momentum",
        "order_flow_imbalance",
        "fox_trap_reversal",
        "liquidity_grab_reversal",
    ],
)
def test_ta_bot_pause_payload_matches_application_config_contract(strategy_id):
    payload = build_ta_bot_pause_payload(
        {
            "enabled_strategies": [strategy_id, "momentum_pulse"],
            "symbols": ["BTCUSDT"],
        },
        strategy_id,
        "CIO_PAUSE: test",
    )

    assert payload == {
        "enabled_strategies": ["momentum_pulse"],
        "changed_by": f"petrosa-cio:{strategy_id}",
        "reason": "CIO_PAUSE: test",
        "validate_only": False,
    }


def test_ta_bot_pause_payload_rejects_invalid_target_schema():
    with pytest.raises(ValueError, match="enabled_strategies") as exc_info:
        build_ta_bot_pause_payload(
            {"enabled_strategies": "not-a-list"},
            "fox_trap_reversal",
            "CIO_PAUSE: test",
        )

    assert "enabled_strategies" in str(exc_info.value)


def test_ta_bot_resume_payload_restores_only_missing_strategy():
    payload = build_ta_bot_resume_payload(
        {"enabled_strategies": ["momentum_pulse"], "symbols": ["BTCUSDT"]},
        "fox_trap_reversal",
        "CIO_AUTO_RESUME: guarded pause expired",
    )

    assert payload == {
        "enabled_strategies": ["momentum_pulse", "fox_trap_reversal"],
        "changed_by": "petrosa-cio:resume:fox_trap_reversal",
        "reason": "CIO_AUTO_RESUME: guarded pause expired",
        "validate_only": False,
    }


def test_ta_bot_resume_payload_does_not_duplicate_strategy():
    payload = build_ta_bot_resume_payload(
        {"enabled_strategies": ["fox_trap_reversal"]},
        "fox_trap_reversal",
        "CIO_AUTO_RESUME: guarded pause expired",
    )

    assert payload["enabled_strategies"] == ["fox_trap_reversal"]
