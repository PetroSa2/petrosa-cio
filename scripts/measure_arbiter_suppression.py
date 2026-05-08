#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Counts:
    received: int = 0
    suppressed: int = 0
    dedup: int = 0
    conflict: int = 0


RECEIVED = "Received NATS payload on"
SUPPRESSED = "ARBITER_SUPPRESSED:"
DEDUP = "ARBITER_SUPPRESSED: SIGNAL_DEDUPLICATED:"
CONFLICT = "ARBITER_SUPPRESSED: signal_conflict_resolved: "
CONFLICT_SUFFIX = " suppressed in favour of "


def pct(part: int, whole: int) -> float:
    if whole <= 0:
        return 0.0
    return 100.0 * (part / whole)


def measure_lines(lines: list[str]) -> Counts:
    received = 0
    suppressed = 0
    dedup = 0
    conflict = 0

    for line in lines:
        if RECEIVED in line:
            received += 1
        if SUPPRESSED in line:
            suppressed += 1
        if DEDUP in line:
            dedup += 1
        if CONFLICT in line and CONFLICT_SUFFIX in line:
            conflict += 1

    return Counts(
        received=received, suppressed=suppressed, dedup=dedup, conflict=conflict
    )


def measure_file(path: Path) -> Counts:
    with path.open("r", encoding="utf-8", errors="replace") as f:
        return measure_lines(f)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Measure CIO arbiter suppression rates from exported logs."
    )
    ap.add_argument("logfile", type=Path, help="Path to a CIO log export file")
    args = ap.parse_args()

    if not args.logfile.exists():
        print(f"error: file not found: {args.logfile}", file=sys.stderr)
        return 1
    if not args.logfile.is_file():
        print(f"error: not a regular file: {args.logfile}", file=sys.stderr)
        return 1

    try:
        c = measure_file(args.logfile)
    except OSError as e:
        print(f"error: unable to read {args.logfile}: {e}", file=sys.stderr)
        return 1

    other = max(0, c.suppressed - c.dedup - c.conflict)

    print(f"logfile: {args.logfile}")
    print(f"received:   {c.received}")
    print(
        f"suppressed: {c.suppressed} ({pct(c.suppressed, c.received):.2f}% of received)"
    )
    print(f"  dedup:    {c.dedup} ({pct(c.dedup, c.suppressed):.2f}% of suppressed)")
    print(
        f"  conflict: {c.conflict} ({pct(c.conflict, c.suppressed):.2f}% of suppressed)"
    )
    print(f"  other:    {other} ({pct(other, c.suppressed):.2f}% of suppressed)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
