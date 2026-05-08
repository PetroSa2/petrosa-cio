# Spike #97 — CIO 60s signal dedup / arbiter suppression vs desired trade throughput

Ticket: `PetroSa2/petrosa-cio#97`

## What exists today (code truth)

### Where suppression happens

- **Arbiter decision**: `cio/core/arbiter.py` (`SignalArbiter.check`)
  - Returns `(allowed=False, reason=...)` for:
    - **Dedup**: `SIGNAL_DEDUPLICATED: ...`
    - **Conflict resolution**: `signal_conflict_resolved: ... suppressed in favour of ...`
- **Listener enforcement**: `cio/core/listener.py` (`NATSListener._handle_message`)
  - If arbiter returns `allowed=False`, listener logs:
    - `ARBITER_SUPPRESSED: {arb_reason}`
  - Then returns early (signal does **not** reach context builder / enforcer / router).

### Deduplication (60s)

- **Window**: `_DEDUP_TTL_SECONDS = 60`
- **Key**: `arbiter:dedup:{symbol}:{canonical_action}`
- **Dedup condition**: if Redis `GET(dedup_key)` returns truthy → suppress
- **Canonical action**: `_normalise_action()` maps producer-specific sides to buy/sell via:
  - buy: `buy|long|bullish`
  - sell: `sell|short|bearish`
  - otherwise lowercased passthrough

### Conflict detection / bias (5 minutes)

- **Window**: `_CONFLICT_TTL_SECONDS = 300` (5 min)
- **Key**: `arbiter:bias:{symbol}`
- **Value**: `{canonical_action}:{confidence:.6f}:{strategy_id}`
- **Rule**:
  - If stored canonical action exists and differs from incoming canonical action:
    - If incoming `confidence <= stored_confidence` → suppress (reason includes `signal_conflict_resolved`)
    - Else → allow and overwrite bias

### Payload fields that affect arbitration

`cio/core/listener.py` extracts:

- **symbol**: `payload["symbol"]` (or `""`)
- **action** (first non-empty of):
  - `payload["side"]`
  - `payload["action"]`
  - `payload["signal_type"]`
  - `""`
- **strategy_id** (first non-empty of):
  - `payload["strategy_id"]`
  - `payload["strategy"]`
  - `"unknown"`
- **confidence**:
  - `float(payload.get("confidence", 0.5))`
  - non-numeric → logs warning and defaults to `0.5`

### Important behavioral note (throughput risk)

Dedup is keyed on **(symbol, canonical_action)** only — **not** on `(strategy_id)` or intent “subject”.

Implication:
- If strategy A emits `BTCUSDT buy`, then within 60s strategy B emitting `BTCUSDT long` (canonical buy) is suppressed, even if it has higher confidence.

This matches the unit tests (`tests/unit/test_signal_arbiter.py`) and is “by design” today.

## How to quantify suppression rate (reproducible)

You need a log export that includes CIO logs for a time window (e.g. Kubernetes pod logs).

The listener logs a “received” line for each message it successfully parses:

- `Received NATS payload on ...`

Suppressed messages will also log:

- `ARBITER_SUPPRESSED: SIGNAL_DEDUPLICATED: ...`
- `ARBITER_SUPPRESSED: signal_conflict_resolved: ... suppressed in favour of ...`

### CLI (quick, approximate)

If you have plain text logs in `cio.log`:

- Total parsed messages:
  - `rg -c "Received NATS payload on" cio.log`
- Total arbiter suppressions:
  - `rg -c "ARBITER_SUPPRESSED:" cio.log`
- Dedup suppressions:
  - `rg -c "ARBITER_SUPPRESSED: SIGNAL_DEDUPLICATED:" cio.log`
- Conflict suppressions:
  - `rg -c "ARBITER_SUPPRESSED: signal_conflict_resolved: .* suppressed in favour of" cio.log`

### Script (recommended)

Use:

- `python3 scripts/measure_arbiter_suppression.py path/to/cio.log`

It prints totals and percentages, including dedup vs conflict breakdown.

## Recommendation (based on current design + likely intent)

### Keep vs tune vs make configurable

- **Dedup 60s** looks like intentional backpressure, but it is a *coarse* key (symbol + canonical action).
- If the desired behavior is “avoid exact duplicates per strategy” or “avoid repeating the same intent while still allowing higher-confidence overrides”, the current design is too aggressive.

Suggested next-step options (not implemented in this spike):

1) **Make dedup key include `strategy_id`** (less cross-strategy suppression)
   - Pros: higher throughput; fewer “starvation” incidents
   - Cons: more duplicate intents in burst windows; may increase downstream load

2) **Shorten TTL / make it configurable**
   - Pros: preserve mechanism, reduce starvation risk
   - Cons: tuning required; may not fully solve cross-strategy starvation

3) **Replace GET-then-SET with atomic SETNX / Lua**
   - Pros: correctness across replicas
   - Cons: doesn’t change throughput shape; only improves race behavior

Given the ticket is a spike, the actionable output is: **document exact keys/windows and measure real suppression rate** using the script above on a representative log window.
