"""The netting vs exchange-truth before/after report runs CIO's own functions (petrosa-data-manager#842)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "stats_source_compare.py"
_spec = importlib.util.spec_from_file_location("stats_source_compare", _PATH)
compare_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(compare_module)


def _perf(wins, losses, cl, delta_window=6):
    total = wins + losses
    return {
        "stats": {
            "win_rate": wins / total if total else None,
            "win_rate_delta": None,
            "win_rate_delta_window": delta_window if total else None,
            "wins": wins,
            "losses": losses,
            "consecutive_losses": cl if total else None,
        },
        "metadata": {"source": "data-manager-pnl-calculator", "fills_replayed": 10},
    }


def _bundle(strategies):
    """strategies: name -> (wins, losses, consecutive losses, net_r list, rounds_without_risk)."""
    return {
        "performance": {
            s: _perf(w, lo, cl) for s, (w, lo, cl, _, _) in strategies.items()
        },
        "rounds": {
            "strategies": {
                s: {"wins": w, "losses": lo}
                for s, (w, lo, _, _, _) in strategies.items()
            }
        },
        "net_r": {
            "strategies": {
                s: {
                    "net_r": [str(r) for r in nr],
                    "cumulative_net_loss_usd": "0",
                    "closed_rounds": len(nr) + unknown,
                    "rounds_without_risk": unknown,
                }
                for s, (_, _, _, nr, unknown) in strategies.items()
            }
        },
    }


GATES = [
    {"strategy": "iceberg", "s_eff": 0.02, "p_be": 0.35, "c": 0.0007, "alpha": 0.2},
    {"strategy": "iceberg", "s_eff": 0.02, "p_be": 0.37, "c": 0.0007, "alpha": 0.2},
    {"strategy": "spread", "s_eff": 0.02, "p_be": 0.30, "c": 0.0007, "alpha": 0.2},
]


def _inputs(netting, truth, paused=()):
    return {
        "netting": _bundle(netting),
        "ground_truth": _bundle(truth),
        "gates": GATES,
        "paused": list(paused),
        "reduce_step": 0.016,
        "equity_usd": 9623.0,
    }


def test_net_ev_posterior_matches_cios_formula():
    levels = {"s_eff": 0.02, "p_be": 0.3, "c": 0.0006, "alpha": 0.2}
    strong = compare_module.net_ev(levels, wins=40, losses=20, k=30.0)
    weak = compare_module.net_ev(levels, wins=2, losses=20, k=30.0)
    unknown = compare_module.net_ev(levels, wins=None, losses=None, k=30.0)
    assert strong["result"] == "pass" and strong["net_ev_r"] > 0
    assert weak["result"] == "fail" and weak["net_ev_r"] < 0
    # no counts: the posterior is the prior centred on p_be; P(edge) is near 0.5, far below 1 - alpha
    assert unknown["result"] == "fail" and 0.4 < unknown["prob_edge"] < 0.6


def test_an_unpaused_strategy_whose_gate_opens_is_the_unpause():
    netting = {"spread": (6, 6, 3, [0.1] * 12, 0)}
    truth = {"spread": (40, 10, 0, [0.5] * 50, 0)}
    result = compare_module.compare(_inputs(netting, truth))
    assert [r["strategy"] for r in result["unpauses"]] == ["spread"]
    report = compare_module.render(result)
    assert report.index("## UNPAUSES IN PRACTICE") < report.index("## Every flip")
    assert "**spread**" in report and "rounds of known R" in report


def test_a_paused_strategy_stays_paused_and_is_listed_apart():
    """Keep/kill never lifts a pause: a paused strategy is shown with its truth verdict, never as tradeable."""
    netting = {"spread": (6, 6, 3, [0.1] * 12, 0)}
    truth = {"spread": (40, 10, 0, [0.5] * 50, 0)}
    result = compare_module.compare(_inputs(netting, truth, paused=["spread"]))
    assert result["unpauses"] == []
    assert [r["strategy"] for r in result["paused"]] == ["spread"]
    report = compare_module.render(result)
    assert "**spread**: stays paused" in report
    assert "unknown-R share" in report


def test_mostly_unknown_r_is_not_evaluated_by_keep_kill():
    """5 rounds of known R out of 25: CIO's keep/kill never says keep.

    Below DERIVED_MIN_ROUNDS (10) it takes its labelled fallback, where keep needs >= 100 rounds with a positive
    lower bound; inside the band the action is "watch", flagged fewer_than_10_closed_rounds (insufficient history).
    """
    truth = {"iceberg": (10, 15, 1, [0.4] * 5, 20)}
    rows = compare_module.evaluate_source(
        _bundle(truth),
        compare_module._gate_levels(GATES),
        reduce_step=0.016,
        equity_usd=9623.0,
    )
    row = rows["iceberg"]
    assert row["keep_kill"] != "keep"
    assert row["keep_kill"] == "watch"
    assert row["keep_kill_reason"] == "fallback_inside_the_band"
    assert row["keep_kill_fallbacks"] == ["fewer_than_10_closed_rounds"]
    assert row["net_r_n"] == 5
    assert row["unknown_r_share"] == pytest.approx(0.8)


def test_flips_are_listed_and_identical_sources_have_none():
    same = {"iceberg": (3, 3, 0, [0.1] * 6, 0)}
    assert compare_module.compare(_inputs(same, same))["flips"] == []
    worse = {"iceberg": (10, 15, 1, [-0.5] * 25, 0)}
    result = compare_module.compare(_inputs(same, worse))
    flipped = {r["strategy"]: r["changed"] for r in result["flips"]}
    assert "iceberg" in flipped
    assert result["unpauses"] == []


def test_classification_uses_cios_own_rules():
    rows = compare_module.evaluate_source(
        _bundle({"iceberg": (10, 15, 1, [0.1] * 25, 0)}),
        compare_module._gate_levels(GATES),
        reduce_step=None,
        equity_usd=None,
    )
    assert rows["iceberg"]["history"] == "computed"
    assert rows["iceberg"]["net_ev"]["n"] == 25


def test_an_unreachable_breakeven_always_fails():
    levels = {"s_eff": 0.02, "p_be": 1.0, "c": 0.02, "alpha": 0.2}
    gate = compare_module.net_ev(levels, wins=50, losses=0, k=30.0)
    assert gate["result"] == "fail" and gate["net_ev_r"] == -1.0


def test_main_renders_a_report_with_no_flips_and_none_paused(tmp_path, capsys):
    same = {"iceberg": (3, 3, 0, [0.1] * 6, 0)}
    path = tmp_path / "inputs.json"
    import json

    inputs = _inputs(same, same)
    inputs["gates"] = []  # no gate data for any strategy
    path.write_text(json.dumps(inputs))
    assert compare_module.main(["stats_source_compare.py", str(path)]) == 0
    report = capsys.readouterr().out
    assert "None: no strategy becomes tradeable" in report
    assert "None paused today." in report
    assert "## Every flip\n\nNone." in report
    assert "no gate data" in report
    assert "| iceberg | 3/3 (0.50, 0)" in report


def test_number_formatting():
    assert compare_module._f(None) == "–"
    assert compare_module._f(0.12345) == "0.12"
    assert compare_module._f(7) == "7"
