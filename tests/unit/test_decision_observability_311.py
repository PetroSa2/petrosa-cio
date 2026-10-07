from types import SimpleNamespace

from cio.core.router import _log_final_decision_observability
from cio.models.enums import ActionType
from cio.models.net_ev import DrawdownDecision, NetEvGate, SizingRecord


def test_final_decision_logs_structured_sizing_and_drawdown(caplog):
    decision = SimpleNamespace(
        sizing=SizingRecord(
            p_post=0.6,
            k=30.0,
            k_source="fallback",
            prob_net_ev_positive=0.7,
            kelly_fraction=0.1,
            kelly_size_usd=50.0,
            f_q=0.25,
            equity_usd=1000.0,
            probe_usd=10.0,
            final_size_usd=10.0,
            binding="probe",
            reason="cold_start",
        ),
        drawdown=DrawdownDecision(
            action="reduce",
            sigma=0.02,
            components_missing=["equity_sigma"],
            drawdown=0.03,
            z_reduce=1.5,
            z_halt=3.0,
            reduce_threshold=0.03,
            halt_threshold=0.06,
            sigma_source="realized",
        ),
        net_ev_gate=NetEvGate(result="pass", reason="positive edge"),
    )
    context = SimpleNamespace(
        strategy_id="strategy-a",
        decision_id="decision-a",
        trigger_payload={"symbol": "BTCUSDT"},
    )

    with caplog.at_level("INFO"):
        _log_final_decision_observability(context, decision, ActionType.EXECUTE)

    messages = [record.message for record in caplog.records]
    sizing = next(message for message in messages if message.startswith("SIZING "))
    drawdown = next(message for message in messages if message.startswith("DRAWDOWN "))
    gate = next(message for message in messages if message.startswith("NET_EV_GATE "))
    keep_kill = next(message for message in messages if message.startswith("KEEP_KILL "))
    assert "strategy=\"strategy-a\"" in sizing
    assert "decision=\"decision-a\"" in sizing
    assert "symbol=\"BTCUSDT\"" in sizing
    assert "final_size_usd=10.0" in sizing
    assert "components_missing=[\"equity_sigma\"]" in drawdown
    assert "action=\"reduce\"" in drawdown
    assert "result=\"pass\"" in gate
    assert "action=\"execute\"" in keep_kill
