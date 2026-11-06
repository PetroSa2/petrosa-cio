"""Before/after: what CIO would decide per strategy on netting stats vs exchange-truth stats (dm#842).

Read-only and offline: it takes the two data-manager bundles (netting = what CIO reads today, ground_truth = the
exchange-truth snapshot served in the same contracts), CIO's own recent Net-EV gate inputs per strategy (the
``NET_EV_GATE`` log lines: ``s_eff``, ``p_be``, ``c``, ``alpha``) and the current pause state, and runs **CIO's
own functions** on both sources:

- ``classify_strategy_history`` on ``/analysis/performance/{strategy}`` (computed / insufficient / unavailable);
- ``keep_kill_job.parse_scorecard`` + ``keep_kill.evaluate_strategies`` on ``/analysis/strategy-net-r`` (keep / kill /
  watch / not_evaluated);
- the Net-EV posterior of ``net_ev`` with the prior strength ``estimate_prior_strength`` over each source's
  ``/analysis/rounds``, at the strategy's median gate levels and costs: pass/fail, P(edge), expected net R.

Levels and costs are held fixed between the two sources, so every difference in the table comes from the stats
alone. Output: a Markdown report with UNPAUSES first (a strategy paused today that the exchange-truth source would
let trade, with its expected net edge and the evidence behind it), then every flip, then the full table.

Usage::

    python scripts/stats_source_compare.py inputs.json > report.md

``inputs.json``: ``{"netting": bundle, "ground_truth": bundle, "gates": [{strategy, s_eff, p_be, c, alpha}],
"paused": [strategy ids], "reduce_step": float|null, "equity_usd": float|null}``, a bundle being
``{"performance": {strategy: body}, "rounds": body, "net_r": body}``.
"""

from __future__ import annotations

import json
import statistics
import sys
from typing import Any

from cio.core.context_builder import classify_strategy_history
from cio.core.keep_kill import evaluate_strategies
from cio.core.keep_kill_job import parse_scorecard
from cio.core.net_ev import beta_cdf, estimate_prior_strength

SOURCES = ("netting", "ground_truth")


def _gate_levels(gates: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """The strategy's median Net-EV inputs over its recent gate evaluations."""
    by: dict[str, list[dict[str, Any]]] = {}
    for g in gates:
        by.setdefault(str(g["strategy"]), []).append(g)
    out: dict[str, dict[str, float]] = {}
    for strategy, rows in by.items():
        out[strategy] = {
            key: statistics.median(float(r[key]) for r in rows)
            for key in ("s_eff", "p_be", "c", "alpha")
        }
        out[strategy]["samples"] = float(len(rows))
    return out


def net_ev(
    levels: dict[str, float], wins: int | None, losses: int | None, k: float
) -> dict[str, Any]:
    """CIO's Net-EV posterior for fixed levels: Beta(k p_be + w, k (1 - p_be) + l) against p_be."""
    p_be, s_eff, cost, alpha = (
        levels["p_be"],
        levels["s_eff"],
        levels["c"],
        levels["alpha"],
    )
    known = wins is not None and losses is not None
    w, lo = (wins, losses) if known else (0, 0)
    if p_be >= 1.0:
        return {"result": "fail", "prob_edge": 0.0, "net_ev_r": -1.0, "n": w + lo}
    a = k * p_be + w
    b = k * (1.0 - p_be) + lo
    mean = a / (a + b)
    take_profit = (s_eff + cost) / p_be - s_eff
    reward_risk = take_profit / s_eff
    prob_edge = 1.0 - beta_cdf(p_be, a, b)
    return {
        "result": "pass" if prob_edge >= 1.0 - alpha else "fail",
        "prob_edge": prob_edge,
        "net_ev_r": mean * (reward_risk + 1.0) - 1.0 - cost / s_eff,
        "n": w + lo,
        "counts_known": known,
    }


def evaluate_source(
    bundle: dict[str, Any],
    levels: dict[str, dict[str, float]],
    *,
    reduce_step: float | None,
    equity_usd: float | None,
) -> dict[str, dict[str, Any]]:
    """Per strategy: CIO's classification, keep/kill record and Net-EV verdict on this source."""
    rounds = (bundle.get("rounds") or {}).get("strategies") or {}
    prior = estimate_prior_strength(
        [
            (
                int(r.get("wins") or 0),
                int(r.get("wins") or 0) + int(r.get("losses") or 0),
            )
            for r in rounds.values()
        ]
    )
    records = {
        rec.strategy_id: rec
        for rec in evaluate_strategies(
            parse_scorecard(bundle.get("net_r") or {}),
            reduce_step=reduce_step,
            equity_usd=equity_usd,
        )
    }
    net_r = (bundle.get("net_r") or {}).get("strategies") or {}
    performance = bundle.get("performance") or {}
    out: dict[str, dict[str, Any]] = {}
    for strategy in sorted(set(performance) | set(rounds) | set(records) | set(levels)):
        body = performance.get(strategy) or {}
        stats = body.get("stats") or {}
        rnd = rounds.get(strategy) or {}
        wins = rnd.get("wins") if rnd else stats.get("wins")
        losses = rnd.get("losses") if rnd else stats.get("losses")
        rec = records.get(strategy)
        nr = net_r.get(strategy) or {}
        closed = nr.get("closed_rounds")
        unknown = nr.get("rounds_without_risk")
        out[strategy] = {
            "history": classify_strategy_history(body) if body else "unavailable",
            "wins": stats.get("wins"),
            "losses": stats.get("losses"),
            "win_rate": stats.get("win_rate"),
            "consecutive_losses": stats.get("consecutive_losses"),
            "realized_pnl": stats.get("realized_pnl"),
            "keep_kill": rec.action if rec else "not_evaluated",
            "keep_kill_reason": rec.reason if rec else "no_scorecard_row",
            "keep_kill_fallbacks": list(rec.fallbacks) if rec else [],
            "net_r_n": rec.n if rec else 0,
            "closed_rounds": closed,
            "unknown_r_share": (unknown / closed) if closed else None,
            "net_ev": (
                net_ev(levels[strategy], wins, losses, prior.value)
                if strategy in levels
                else None
            ),
            "prior_k": prior.value,
        }
    return out


def _allowed(row: dict[str, Any]) -> bool:
    """Would CIO let this strategy open new trades: not killed and the Net-EV gate passes."""
    gate = row.get("net_ev")
    return row["keep_kill"] != "kill" and gate is not None and gate["result"] == "pass"


def compare(inputs: dict[str, Any]) -> dict[str, Any]:
    levels = _gate_levels(inputs.get("gates") or [])
    kwargs = {
        "reduce_step": inputs.get("reduce_step"),
        "equity_usd": inputs.get("equity_usd"),
    }
    by_source = {s: evaluate_source(inputs[s], levels, **kwargs) for s in SOURCES}
    paused = set(inputs.get("paused") or [])
    strategies = sorted(set(by_source["netting"]) | set(by_source["ground_truth"]))
    rows, flips, unpauses, paused_rows = [], [], [], []
    for strategy in strategies:
        before = by_source["netting"].get(strategy) or {}
        after = by_source["ground_truth"].get(strategy) or {}
        changed = [
            key
            for key in ("history", "keep_kill", "allowed")
            if (
                before.get(key)
                if key != "allowed"
                else bool(before) and _allowed(before)
            )
            != (after.get(key) if key != "allowed" else bool(after) and _allowed(after))
        ]
        gate_before = (before.get("net_ev") or {}).get("result")
        gate_after = (after.get("net_ev") or {}).get("result")
        if gate_before != gate_after:
            changed.append("net_ev")
        row = {
            "strategy": strategy,
            "before": before,
            "after": after,
            "changed": changed,
            "paused": strategy in paused,
        }
        rows.append(row)
        if changed:
            flips.append(row)
        newly_allowed = (
            bool(after) and _allowed(after) and not (bool(before) and _allowed(before))
        )
        # Keep/kill never lifts a pause (it only pauses, on kill, and only in enforce mode), so a strategy paused
        # today stays paused on either source: it is listed apart, never as tradeable. Where risk enters is an
        # unpaused strategy whose Net-EV gate opens.
        if newly_allowed and strategy not in paused:
            unpauses.append(row)
        if strategy in paused:
            paused_rows.append(row)
    return {
        "rows": rows,
        "flips": flips,
        "unpauses": unpauses,
        "paused": paused_rows,
        "levels": levels,
    }


def _f(value: Any, digits: int = 2) -> str:
    if value is None:
        return "–"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _gate(row: dict[str, Any] | None) -> str:
    gate = (row or {}).get("net_ev")
    if not gate:
        return "no gate data"
    return f"{gate['result']} (P={_f(gate['prob_edge'])}, {_f(gate['net_ev_r'])}R, n={gate['n']})"


def render(result: dict[str, Any]) -> str:
    out = ["# Strategy stats: netting (today) vs exchange truth (dm#842)", ""]
    out.append("## UNPAUSES IN PRACTICE: newly tradeable on exchange truth")
    out.append(
        "An unpaused strategy whose Net-EV gate opens (fail on netting, pass on truth) and that keep/kill doesn't "
        "kill. Keep/kill never lifts a pause, so strategies paused today are listed separately below."
    )
    if not result["unpauses"]:
        out.append("None: no strategy becomes tradeable on the exchange-truth source.")
    for row in result["unpauses"]:
        a = row["after"]
        out.append(
            f"- **{row['strategy']}**{' (paused today)' if row['paused'] else ''}: "
            f"Net-EV {_gate(a)}; keep/kill {a['keep_kill']} ({a['keep_kill_reason']}, {a['net_r_n']} rounds of known R"
            f", unknown-R share {_f(a['unknown_r_share'])}); W/L {a['wins']}/{a['losses']}"
        )
    out += ["", "## Paused today: stay paused on either source", ""]
    if not result["paused"]:
        out.append("None paused today.")
    for row in result["paused"]:
        a = row["after"]
        out.append(
            f"- **{row['strategy']}**: stays paused (no stats path lifts a pause). On truth: keep/kill "
            f"{a.get('keep_kill')} ({a.get('keep_kill_reason')}, {a.get('net_r_n')} rounds of known R, unknown-R "
            f"share {_f(a.get('unknown_r_share'))}); Net-EV {_gate(a)}; it would trade only if a person resumes it."
        )
    out += ["", "## Every flip", ""]
    if not result["flips"]:
        out.append("None.")
    for row in result["flips"]:
        b, a = row["before"], row["after"]
        out.append(
            f"- **{row['strategy']}** ({', '.join(row['changed'])}): history {b.get('history')} → {a.get('history')}; "
            f"keep/kill {b.get('keep_kill')} → {a.get('keep_kill')}; Net-EV {_gate(b)} → {_gate(a)}"
        )
    out += [
        "",
        "## Full table",
        "",
        "| Strategy | Netting W/L (WR, CL) | Truth W/L (WR, CL) | Netting keep/kill | Truth keep/kill (known-R n, unknown share) | Netting Net-EV | Truth Net-EV |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in result["rows"]:
        b, a = row["before"], row["after"]
        out.append(
            f"| {row['strategy']} | {b.get('wins')}/{b.get('losses')} ({_f(b.get('win_rate'))}, {b.get('consecutive_losses')}) "
            f"| {a.get('wins')}/{a.get('losses')} ({_f(a.get('win_rate'))}, {a.get('consecutive_losses')}) "
            f"| {b.get('keep_kill')} | {a.get('keep_kill')} ({a.get('net_r_n')}, {_f(a.get('unknown_r_share'))}) "
            f"| {_gate(b)} | {_gate(a)} |"
        )
    return "\n".join(out) + "\n"


def main(argv: list[str]) -> int:
    with open(argv[1], encoding="utf-8") as handle:
        inputs = json.load(handle)
    sys.stdout.write(render(compare(inputs)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
