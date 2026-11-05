"""petrosa-cio#395: the engine rejects a signal whose stop tradeengine refused, for that reason."""

from unittest.mock import patch

from test_engine import build_test_context

from cio.core.context_builder import SIGNAL_STOP_SURFACE
from cio.core.engine import CodeEngine
from cio.models import ContextGap
from cio.models.enums import RejectionSource

REASON = "stop_not_protective: side=LONG stop_loss=0.18208 reference_price=0.1819"


def _context(**kwargs):
    context = build_test_context(**kwargs)
    context.pre_decision_context.gaps = [
        ContextGap(surface=SIGNAL_STOP_SURFACE, reason=REASON)
    ]
    return context


def test_a_refused_stop_blocks_with_the_signal_reason_not_portfolio_unavailable():
    with (
        patch("cio.core.engine.RISK_GATE_CONTEXT_FALLBACK") as fallback,
        patch("cio.core.engine.RISK_GATE_REAL_BREACH") as real_breach,
    ):
        result = CodeEngine.run(_context(drawdown=0.0, portfolio_state_available=True))

    assert result.hard_blocked is True
    assert result.block_context_fallback is False
    assert result.block_rejection_source == RejectionSource.SIGNAL_STOP_NOT_PROTECTIVE
    assert result.block_reason.startswith("SIGNAL_STOP_NOT_PROTECTIVE: ")
    assert REASON in result.block_reason
    assert "PORTFOLIO_CONTEXT_UNAVAILABLE" not in result.block_reason
    fallback.add.assert_not_called()  # never counted as a tradeengine /state outage
    real_breach.add.assert_not_called()


def test_the_signal_reason_wins_over_the_fail_safe_defaults():
    # The context builder still returns blocking defaults (drawdown 100%); the reason stays the signal's.
    result = CodeEngine.run(_context(drawdown=1.0, portfolio_state_available=True))

    assert result.block_rejection_source == RejectionSource.SIGNAL_STOP_NOT_PROTECTIVE
    assert "Global drawdown" not in result.block_reason


def test_without_the_gap_the_portfolio_outage_path_is_unchanged():
    context = build_test_context(portfolio_state_available=False)

    result = CodeEngine.run(context)

    assert result.block_reason.startswith("PORTFOLIO_CONTEXT_UNAVAILABLE: ")
    assert result.block_context_fallback is True
    assert result.block_rejection_source is None
