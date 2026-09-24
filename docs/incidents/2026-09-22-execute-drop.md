# CIO execute-decision drop: 2026-09-22

## Scope and conclusion

This investigation covers the fall in `execute` decisions beginning around
2026-09-22 06:00 UTC. It uses the production observations recorded in
[petrosa-cio#250](https://github.com/PetroSa2/petrosa-cio/issues/250), the
deployed-code history, and the linked follow-up investigations.

The primary confirmed cause is an input-contract failure in data-manager:
`win_rate_delta` and `consecutive_losses` are always `None`, so the CIO
strategy assessor receives incomplete context and returns `MISSING_INPUT`.
That drives the safe `pause_strategy` path. LLM transport failures are a
confirmed additional contributor, especially later on 2026-09-23/24, while
the portfolio `/state` outage explains the later `block` spike rather than
the initial pause/skip transition.

No code or deployment change coincided with the initial symptom. The issue
records no petrosa-cio, tradeengine, or GitOps commits between 2026-09-21
18:00 UTC and 2026-09-22 09:00 UTC, so a deploy-triggered regression is not
supported by the incident timeline.

## Observed impact

The source query was:

```promql
sum by (action) (increase(cio_decision_actions_total[6h]))
```

The six-hour windows recorded in [#250](https://github.com/PetroSa2/petrosa-cio/issues/250)
were:

| Window ending (UTC) | execute | pause_strategy | skip | block | no action |
| --- | ---: | ---: | ---: | ---: | ---: |
| 2026-09-20 09:00 | 128 | 1,494 | 291 | 1 | 1,940 |
| 2026-09-21 15:00 | 47 | 858 | 161 | 0 | 1,121 |
| 2026-09-22 03:00 | 28 | 872 | 164 | 0 | 1,109 |
| 2026-09-22 09:00 | 0 | 585 | 89 | 0 | 688 |
| 2026-09-22 21:00 | 3 | 936 | 121 | 0 | 1,105 |
| 2026-09-23 15:00 | 0 | 479 | 58 | 0 | 603 |
| 2026-09-24 09:00 | 6 | 517 | 191 | 760 | 1,517 |

The issue also records that upstream TA/realtime signals and data-manager
intents continued flowing, while executed orders fell from roughly 10–55 per
six hours to 0–2. This is a decision-quality failure, not an upstream signal
silence or a confirmed order-execution regression.

## Candidate-cause findings

### 1. LLM behaviour or endpoint change — confirmed contributor, not the initial trigger

**Verdict:** later LLM transport failures are confirmed, but the available
evidence does not place them at the start of the 2026-09-22 06:00 UTC drop.

Evidence:

- The [#242 investigation](https://github.com/PetroSa2/petrosa-cio/issues/242)
  records `meta/llama-3.2-11b-vision-instruct` returning HTTP 500 inference
  connection errors on 2026-09-23 16:57:52 and 16:58:39 UTC, followed at
  16:58:41 by `LLM transport error` and `LLM_PARSE_FAILURE_SKIP`.
- The same investigation records audit timeouts at 16:41:29, 16:41:59,
  16:46:37, 16:49:24, 16:51:25, and 16:54:02 UTC.
- The [#248 investigation](https://github.com/PetroSa2/petrosa-cio/issues/248)
  measures 24 of 104 classifier calls timing out in the 2026-09-24 19:33–20:32
  UTC window. Each took about 7 seconds, matching
  `LLM_CALL_TIMEOUT_SECONDS`; the fallback resolved to the same route.
- Commit [`4e5f911`](https://github.com/PetroSa2/petrosa-cio/commit/4e5f911ada15ef9cbf007da6503d50dfff86b840)
  shipped per-call timeouts and concurrent upstream persona calls on
  2026-09-23 02:29:49 UTC, after the initial drop was already observed.

The confirmed implementation follow-up is
[petrosa-cio#248](https://github.com/PetroSa2/petrosa-cio/issues/248), which
retries transient timeouts and improves failure diagnostics. The policy
follow-up [petrosa-cio#253](https://github.com/PetroSa2/petrosa-cio/issues/253)
makes persona outages deterministic `pause_strategy` decisions.

### 2. Missing inputs — confirmed primary cause

**Verdict:** confirmed. The strategy-assessor input contract is broken
upstream and directly maps to the observed pause-heavy action mix.

Evidence:

- [petrosa-data-manager#318](https://github.com/PetroSa2/petrosa-data-manager/issues/318)
  shows that `GET /analysis/performance/{strategy_id}` hardcodes
  `win_rate_delta=None` and `consecutive_losses=None` on every response branch,
  including the real-data P&L calculator path.
- `cio/personas/strategy_assessor.py` requires both fields. The resulting
  `PETROSA_PROMPT_STRATEGY_ASSESSOR` response is therefore
  `{"error":"MISSING_INPUT"}` even for strategies with trading history.
- Commit [`51e7dfd`](https://github.com/PetroSa2/petrosa-cio/commit/51e7dfda98745fc8aef7327ff49df6e534e6a419)
  traced the recurring cascade through three prior CIO incidents and added
  `STRATEGY_STATS_STRUCTURAL_GAP` observability. It explicitly filed #318 as
  the required data-manager fix.
- The [#253 production window](https://github.com/PetroSa2/petrosa-cio/issues/253)
  records 11 assessor `LLM_MISSING_INPUT_SKIP` events, all attributed to #318.

The implementation follow-up is
[petrosa-data-manager#318](https://github.com/PetroSa2/petrosa-data-manager/issues/318).
Until it lands, a valid upstream response can still be semantically
incomplete and bias the CIO away from `execute`.

### 3. Upstream evaluator and portfolio state — separate later cause

**Verdict:** ruled out as the primary cause of the 2026-09-22 pause/skip drop;
confirmed as a separate cause of the 2026-09-24 `block` spike.

Evidence:

- The [#249 investigation](https://github.com/PetroSa2/petrosa-cio/issues/249)
  records 68 of 104 decisions as fallback blocks in the 2026-09-24 20:00–21:00
  UTC window after tradeengine `/state` fetch failures. Those blocks use
  fallback risk values and are not `pause_strategy` or `skip` decisions.
- The issue itself identifies the 2026-09-24 `block` column as a separate
  cause tracked by #249.
- [petrosa-data-manager#353](https://github.com/PetroSa2/petrosa-data-manager/issues/353)
  records the streaming gap detector failing at 2026-09-24 18:35:30 UTC with
  `nats: must use coroutine for subscriptions`, followed by batch-only mode.
  That timestamp is after the initial 2026-09-22 transition.

The related remediation is [petrosa-cio#249](https://github.com/PetroSa2/petrosa-cio/issues/249)
for a short last-known-good portfolio cache. The detector fix is tracked by
[petrosa-data-manager#353](https://github.com/PetroSa2/petrosa-data-manager/issues/353).

### 4. Strategy mix — inconclusive

**Verdict:** not supported as the primary explanation, but not fully
disproven by the available aggregate data.

The incident report says TA signals, realtime signals, and data-manager
intents continued flowing during the drop. That rules out a total producer
silence, but the aggregate action counter does not identify which strategies
produced each intent or compare their pre-incident mix with the incident mix.
No per-strategy production query or decision export was attached to #250, so
there is insufficient evidence to claim that a strategy-mix change caused the
transition.

### 5. Pause-path amplification and other factors — confirmed secondary effects

**Verdict:** confirmed as amplifiers, not the initial cause.

- [petrosa-cio#238](https://github.com/PetroSa2/petrosa-cio/issues/238) records
  `FAILED_TO_APPLY` pause requests caused by the unsupported `enabled`
  parameter and subsequent 429 cooldowns.
- [petrosa-cio#245](https://github.com/PetroSa2/petrosa-cio/issues/245) documents
  that the current pause path sends `{"parameters":{"enabled":false}}`
  through the tuning endpoint and shares its freeze key with tuning changes.
- Commit [`69924d9`](https://github.com/PetroSa2/petrosa-cio/commit/69924d97f1aee6403247cf4008673a4e52d9f3a7)
  changed alert and NATS transport handling on 2026-09-22 19:09:15 UTC. It
  did not change decision selection, the strategy-assessor contract, or the
  LLM route, so it cannot explain the 06:00 UTC onset.

These effects can make pauses persistent or noisy after the decision engine
has selected them, but they do not create the missing strategy-stat fields.

## Root-cause statement and follow-ups

The 2026-09-22 execute collapse is best explained by a pre-existing,
structurally incomplete strategy-stat response from data-manager. CIO treats
the missing required fields as `MISSING_INPUT`, and its safe policy steers the
strategy-assessor path toward `pause_strategy`; continued upstream intent
flow confirms that this happened after signal production.

LLM endpoint failures and timeouts were independently real and later made
`skip`/pause outcomes worse. Portfolio `/state` failures and the dead
streaming gap detector explain the later 2026-09-24 `block`/health symptoms,
not the initial execute-to-pause/skip transition.

Implementation work is already split into the correct follow-ups:

1. [petrosa-data-manager#318](https://github.com/PetroSa2/petrosa-data-manager/issues/318)
   computes the missing strategy-stat fields.
2. [petrosa-cio#248](https://github.com/PetroSa2/petrosa-cio/issues/248)
   retries transient LLM failures and makes timeout evidence readable.
3. [petrosa-cio#253](https://github.com/PetroSa2/petrosa-cio/issues/253)
   makes all persona-stage outages produce attributable pauses.
4. [petrosa-cio#249](https://github.com/PetroSa2/petrosa-cio/issues/249)
   prevents brief portfolio-state fetch blips from becoming fake drawdown
   blocks.

This spike intentionally makes no runtime change beyond this write-up.
