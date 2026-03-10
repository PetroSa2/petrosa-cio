# PETROSA AUTONOMOUS TRADING INTELLIGENCE FRAMEWORK

---

## SECTION 1 — AGENT PERSONAS

The system runs eight specialized personas. Each is a focused LLM call with a narrow, well-defined prompt and a constrained output schema. Narrow personas mean smaller, cheaper prompts and more predictable outputs.

---

### 1. Market Regime Analyst
**Role:** Reads the current state of the world.
**Responsibility:** Classifies market conditions and produces a regime context object that all other personas consume first.
**Inputs:** petrosa-data-manager `/analysis/regime`, volatility indices, recent price action summary, macro event calendar.
**Outputs:** `RegimeContext { regime: enum[8], volatility: low|medium|high|extreme, trend_strength: 0-1, confidence: 0-1, key_risks: string[] }`
**Example questions:** "Is this a trending or mean-reverting market?" / "Is current volatility elevated vs 30-day norm?" / "Are we in a breakout phase or consolidation?"

---

### 2. Strategy Specialist
**Role:** Understands each strategy's mechanics, expected behavior per regime, and current health.
**Responsibility:** Evaluates whether a specific strategy is appropriate given current conditions. Flags degraded strategies.
**Inputs:** Strategy config from TA-bot/realtime APIs, audit trail, shadow ROI, current win rate, `RegimeContext`.
**Outputs:** `StrategyAssessment { strategy_id, health: healthy|degraded|failing, regime_fit: good|neutral|poor, recommended_params: {}, confidence: 0-1 }`
**Example questions:** "Is RSI Extreme Reversal suited for a breakout regime?" / "Has this strategy's win rate degraded in the last 48 hours?"

---

### 3. Quant Analyst
**Role:** Does the math. No opinions, just numbers.
**Responsibility:** Calculates expected value, probability of success, fee-adjusted return, Kelly sizing, and risk/reward ratio for a specific trade or parameter change.
**Inputs:** Proposed trade parameters, fee schedule, current spread, historical win rate for strategy+regime combination, volatility.
**Outputs:** `QuantAssessment { expected_value: float, probability_success: float, kelly_fraction: float, fee_adjusted_return: float, risk_reward_ratio: float }`
**Example questions:** "What's the EV of this trade after fees?" / "What position size does Kelly suggest at 62% win rate?"

---

### 4. Risk Manager
**Role:** The veto player. Conservative by design.
**Responsibility:** Checks all hard limits. Evaluates portfolio exposure, drawdown, concentration risk, and correlation between open positions. Can issue a BLOCK regardless of other votes.
**Inputs:** Current open positions, global drawdown, max orders config, `RegimeContext`, `QuantAssessment`.
**Outputs:** `RiskVerdict { verdict: allow|warn|block, reason: string, hard_limit_breached: bool, suggested_adjustment: {} }`
**Example questions:** "Does this trade push us over max drawdown?" / "Are we overexposed to BTC across strategies?"

---

### 5. Portfolio Manager
**Role:** Thinks in portfolios, not individual trades.
**Responsibility:** Evaluates trade impact on overall portfolio balance — correlation, sector concentration, strategy diversity, capital allocation.
**Inputs:** All open positions, capital allocation per strategy, `RegimeContext`, proposed trade.
**Outputs:** `PortfolioVerdict { correlation_risk: low|medium|high, allocation_impact: string, suggested_position_scale: float, rationale: string }`
**Example questions:** "If we enter this BTC long, what's our net BTC exposure across all strategies?" / "Are too many strategies running the same direction?"

---

### 6. Treasury Manager
**Role:** Manages capital efficiency and fee economics.
**Responsibility:** Evaluates whether capital is being deployed efficiently. Tracks idle capital, fee drag, and whether the trade's expected return justifies the capital lock-up.
**Inputs:** Available capital, locked capital, fee schedule, `QuantAssessment`, average trade duration per strategy.
**Outputs:** `TreasuryVerdict { capital_efficiency_score: 0-1, opportunity_cost: float, fee_drag_warning: bool, recommendation: string }`
**Example questions:** "Is it worth tying up $10k for an expected $12 gain after fees?" / "Are maker fees being used optimally?"

---

### 7. Execution Specialist
**Role:** Cares only about how to enter/exit cleanly.
**Responsibility:** Evaluates timing, order type, slippage risk, and market depth for execution quality.
**Inputs:** Order book snapshot, current spread, recent volume, proposed size, market regime.
**Outputs:** `ExecutionPlan { order_type: limit|market, suggested_entry_offset: float, slippage_estimate: float, urgency: low|medium|high, split_order: bool }`
**Example questions:** "Should this be a limit or market order given current spread?" / "Is the order size too large relative to current depth?"

---

### 8. Decision Arbiter
**Role:** Final synthesizer. Reads all verdicts and produces the decision.
**Responsibility:** Weighs all persona outputs, resolves conflicts, enforces hard limit overrides (Risk Manager block is always final), and produces a structured decision with full reasoning trace.
**Inputs:** All persona outputs, conversation history for this trigger, hard limit config.
**Outputs:** `Decision { action: execute|modify|skip|escalate, parameters: {}, confidence: 0-1, dissenting_views: [], thought_trace: string (min 200 chars) }`
**Example questions:** N/A — this persona only synthesizes, never queries external data.

---

### Collaboration Model

Personas do **not** talk to each other in free-form dialogue. They operate in a defined sequence where early outputs become inputs to later ones. This is critical for cost control — it prevents unbounded token chains. The Regime Analyst always runs first because its output is a dependency for everyone else.

---

## SECTION 2 — ORCHESTRATOR DESIGN

### Trigger Taxonomy

Triggers fall into three classes with different latency budgets:

| Class | Examples | Latency Budget | Personas Activated |
|---|---|---|---|
| **Hot** | Trade intent received | < 2 seconds | Risk Manager + Arbiter only (fast path) |
| **Warm** | Strategy performance degraded, regime changed | < 30 seconds | Full panel minus Execution |
| **Cold** | Scheduled review, parameter optimization | < 5 minutes | Full panel + Knowledge retrieval |

Hot triggers skip the full panel by design. Most trade intents are routine — they just need a fast risk check. The full panel is reserved for decisions with strategic implications.

---

### Orchestration Pipeline

```
TRIGGER RECEIVED
      │
      ▼
┌─────────────────────┐
│   TRIGGER ROUTER    │  Classifies: Hot / Warm / Cold
│   + Context Builder │  Fetches: regime, positions, relevant config
└─────────────────────┘
      │
      ▼
┌─────────────────────────────────────────────────────┐
│              PERSONA EXECUTION (Sequential)          │
│                                                     │
│  Round 1 (always): Market Regime Analyst            │
│  Round 2 (always): Risk Manager (fast path exits here│
│            if hard limit breached)                  │
│  Round 3 (warm/cold): Strategy Specialist           │
│                       Quant Analyst                 │
│                       Portfolio Manager             │
│                       Treasury Manager              │
│  Round 4 (cold only): Knowledge Retrieval           │
│                       (similar past decisions)      │
│  Round 5 (always): Execution Specialist             │
│                    (only if action = execute)       │
└─────────────────────────────────────────────────────┘
      │
      ▼
┌─────────────────────┐
│   DECISION ARBITER  │  Synthesizes all verdicts
└─────────────────────┘
      │
      ▼
┌─────────────────────┐
│   OUTPUT + LOGGING  │  Structured decision → NATS / MCP / Audit
└─────────────────────┘
```

---

### Routing Logic

The Context Builder builds a `TriggerContext` object before any persona runs. It contains: current regime (cached, max 15min old), open positions summary, relevant strategy config, and the raw trigger payload. Every persona receives this object — no persona fetches its own context. This eliminates redundant API calls and keeps token counts predictable.

**Persona activation rules:**
- Risk Manager block → immediate exit, no further personas run, cost ≈ 2 LLM calls
- Warm path → 5 LLM calls total
- Cold path → 7-8 LLM calls + knowledge retrieval

---

### Decision Aggregation

The Arbiter receives a structured `VerdictBundle` — not raw text from each persona. Each persona outputs a JSON verdict object. The Arbiter's prompt says: "Given these verdicts, produce a final decision. Risk Manager BLOCK is non-negotiable. Resolve all other conflicts by weighted confidence score." The Arbiter never re-reads market data; it only reads verdicts. This keeps the Arbiter prompt small and deterministic.

---

## SECTION 3 — DECISION FRAMEWORK

### Framework Selection by Decision Type

**Trade Entry → Modified ReAct**
ReAct (Reason + Act) works well here because entry decisions need grounded reasoning tied to observable data. Each reasoning step should reference a specific data point. Pure Chain-of-Thought is too unconstrained and hallucinates numbers. The modification: constrain the "Act" step to only call predefined tools (get regime, get position, calculate EV) rather than free-form actions.

**Trade Exit → Chain-of-Thought with hard triggers**
Exit decisions are mostly rule-based (stop loss, take profit, time expiry) with LLM intervention only for edge cases like "position has been open 3x normal duration during a regime change." CoT is sufficient because the reasoning is linear: check rule → check conditions → decide. The LLM should only be invoked for exits when hard rules are insufficient.

**Strategy Parameter Changes → Debate-style**
This is the highest-stakes decision type. A strategy parameter change affects every future trade from that strategy. Two personas should argue opposing positions: Strategy Specialist (proposes change + rationale) vs Risk Manager (argues for status quo unless change clearly reduces risk). The Arbiter resolves. Debate is expensive — justified here because the impact is large and persistent.

**Risk Overrides → Voting with weighted veto**
When the system wants to override a risk limit (e.g., temporarily increase max position size during a high-confidence setup), require a supermajority: Portfolio Manager + Quant Analyst + Arbiter all must agree, AND the Risk Manager must not BLOCK. Three independent "agree" votes plus absence of veto. No single persona can authorize an override alone.

---

### The Reasoning Loop

```python
# Pseudocode — not implementation
def reasoning_loop(trigger):
    context = build_context(trigger)

    # Step 1: Ground truth
    regime = RegimeAnalyst.run(context)
    if regime.confidence < 0.5:
        return Decision(action="skip", reason="regime uncertain")

    # Step 2: Hard limits first
    risk = RiskManager.run(context, regime)
    if risk.hard_limit_breached:
        return Decision(action="block", reason=risk.reason)

    # Step 3: Full analysis (warm/cold only)
    if trigger.class in [WARM, COLD]:
        quant = QuantAnalyst.run(context, regime)
        strategy = StrategySpecialist.run(context, regime)
        portfolio = PortfolioManager.run(context, regime, quant)
        treasury = TreasuryManager.run(context, quant)

    # Step 4: Synthesize
    verdicts = VerdictBundle(regime, risk, quant, strategy, portfolio, treasury)
    decision = Arbiter.run(verdicts)

    log_decision(trigger, context, verdicts, decision)
    return decision
```

---

## SECTION 4 — KNOWLEDGE BASE

### What to Store

The knowledge base has four collections, each serving a different retrieval pattern:

**1. Regime Playbooks** (structured, low churn)
For each of the 8 market regimes: which strategies historically perform well, which to disable, typical parameter adjustments. Written once by humans, refined over time by the learning system. Retrieved by Regime Analyst and Strategy Specialist. Store as structured JSON + searchable text. ~100 documents total.

**2. Strategy Documentation** (structured, low churn)
For each of the 34 strategies: what it does, what parameters mean, valid ranges, regime fit matrix, known edge cases. Retrieved by Strategy Specialist. Critical for cheap models — they don't need to "know" what RSI Extreme Reversal is if the playbook tells them. ~34 documents.

**3. Decision History** (append-only, high value)
Every decision ever made by the system: trigger, context snapshot, verdicts, final decision, outcome (enriched post-trade). This is the system's episodic memory. Retrieved by Knowledge Retrieval step in cold path using semantic similarity: "find the 5 most similar past decisions to this current situation." ~grows unboundedly, needs periodic summarization.

**4. Parameter Change Outcomes** (append-only)
Every strategy parameter change: what was changed, why, what regime, what happened to win rate / PnL in the 48 hours after. The core feedback loop for parameter optimization. ~grows with usage.

---

### Retrieval Architecture

Use a vector database (Qdrant or Weaviate, self-hosted in cluster) for Decision History and Parameter Change Outcomes. Embed each decision record on write using a small embedding model (text-embedding-3-small is cheap and sufficient).

At retrieval time, the Context Builder generates a query vector from the current `TriggerContext`. Top-5 similar past decisions are retrieved and summarized into a `HistoricalContext` block appended to the Arbiter's prompt. This gives the Arbiter pattern-matching ability without requiring a large model with long context.

Regime Playbooks and Strategy Docs are retrieved by exact lookup, not semantic search — they're small enough to cache in memory in the CIO service.

---

## SECTION 5 — LEARNING SYSTEM

### Decision Logging Schema

Every decision generates a `DecisionRecord`:

```json
{
  "id": "uuid",
  "timestamp": "ISO8601",
  "trigger_type": "trade_intent|regime_change|performance_degraded|...",
  "trigger_payload": {},
  "regime_context": {},
  "verdicts": {
    "regime_analyst": {},
    "risk_manager": {},
    "quant_analyst": {},
    "strategy_specialist": {},
    "portfolio_manager": {},
    "treasury_manager": {},
    "execution_specialist": {}
  },
  "decision": {
    "action": "execute|modify|skip|block|escalate",
    "parameters": {},
    "confidence": 0.0,
    "thought_trace": "..."
  },
  "outcome": null,
  "outcome_metrics": {
    "pnl": null,
    "fees_paid": null,
    "duration_minutes": null,
    "exit_reason": null,
    "regime_at_exit": null
  },
  "enriched_at": null
}
```

Outcomes are null at decision time. A background enrichment job runs after each trade closes, fills in `outcome_metrics`, and re-indexes the record in the vector DB.

---

### Learning Loops

**Loop 1 — Retrieval-Based (immediate, zero training cost)**
Similar past decisions are retrieved at query time and injected into the Arbiter's prompt. The system "learns" by analogy — if past decisions with similar context led to bad outcomes, that context is visible to the Arbiter. No model retraining required.

**Loop 2 — Regime Playbook Refinement (weekly, human-in-the-loop)**
A weekly cold-path job queries: "For each regime, what parameter settings correlated with best win rate in the last 30 days?" Output is a proposed update to the Regime Playbooks. A human reviews and approves. This keeps human oversight on the strategic layer while automating the data analysis.

**Loop 3 — Parameter Outcome Correlation (continuous, automated)**
After every parameter change + 48hr window, the system calculates: did win rate improve, stay flat, or degrade? This signal is stored in the Parameter Change Outcomes collection and weighted into future Strategy Specialist recommendations. The system will start favoring parameter adjustments that historically improved outcomes in similar regimes.

**Loop 4 — Confidence Calibration (monthly)**
Compare `decision.confidence` scores to actual outcomes. If the system says 0.9 confidence and wins 60% of the time, it's overconfident. Track calibration curves per trigger type and adjust system prompt confidence framing accordingly. This is a manual tuning step, not automated.

---

## SECTION 6 — TRADE LIFECYCLE DECISIONS

### When the Brain Intervenes

**Entry Decisions**
Trigger: Trade intent arrives on `intent.*`
Fast path: Risk Manager check (< 2s)
Full path: If strategy hasn't been evaluated in the current regime window, run warm path
Conditions for full path: Strategy win rate dropped > 15% in last 24h, or regime changed since last evaluation, or position would exceed 20% of category allocation.

**Trade Parameter Decisions**
Position size: Always calculated by Quant Analyst using Kelly fraction × regime_volatility_scalar. Hard cap at 2× Kelly.
Stop loss: Set by Strategy Specialist from playbook, scaled by ATR if volatility is elevated.
Take profit: Default from strategy config. Arbiter can widen in strong trend regime, tighten in choppy regime.
Leverage: Regime-gated. Leverage > 1 only permitted in `trending` or `breakout` regimes with Risk Manager approval.

**Exit Decisions**
Hard exits (no LLM): Stop loss hit, take profit hit, max trade duration exceeded.
Soft exits (LLM): Trade open > 1.5× average duration, regime changed to adverse classification, portfolio correlation spiked above threshold, strategy declared degraded mid-trade.
Escalation exits: LLM cannot decide (confidence < 0.4, conflicting verdicts) → flag for human review, keep position, alert.

**Strategy Management**
Parameter adjustment trigger: Win rate drops > 15% over 48h window, OR Regime Analyst detects regime shift to a regime with < 0.5 fit score for this strategy.
Strategy deactivation trigger: Win rate drops > 30% over 72h, OR hard drawdown threshold reached for this strategy specifically.
Strategy reactivation: Only on cold-path scheduled review, never automatically during active trading session.

---

## SECTION 7 — COST & PROFIT REASONING

### Expected Value Calculation

Every trade must clear a minimum EV threshold before execution. The Quant Analyst calculates:

```
gross_expected = (win_rate × avg_win) - (loss_rate × avg_loss)
fee_cost = (entry_fee + exit_fee) × position_size
slippage_cost = spread_estimate × position_size × slippage_factor
net_ev = gross_expected - fee_cost - slippage_cost
ev_ratio = net_ev / (position_size × risk_fraction)
```

Minimum `ev_ratio` threshold: 0.003 (0.3% net expected return per unit of risk). Trades below this are skipped regardless of other factors.

The `slippage_factor` is regime-dependent: 1.0 in normal conditions, 1.5 in high volatility, 2.5 in extreme volatility. This automatically makes the system more selective in volatile markets without any explicit rule — the math does it.

Fee regime awareness: maker vs taker fee difference (~0.02% vs 0.04% on Binance) should drive order type selection. The Execution Specialist weights limit orders heavily when urgency is low. On a $10k position, the difference is $20 per round trip — meaningful at volume.

**Fee drag monitoring:** The Treasury Manager tracks rolling 7-day fee drag as a percentage of gross PnL. If fee drag exceeds 40% of gross PnL, it triggers a cold-path review to evaluate whether position sizing should increase (to amortize fees better) or certain low-EV strategies should be paused.

---

## SECTION 8 — CHEAP MODEL STRATEGY

The goal is to run 95% of decisions on a model like Haiku or Mistral 7B, reserving Sonnet/GPT-4-class models for cold-path analysis and escalations only.

### Making Small Models Viable

**Persona specialization is the primary cost lever.** A small model given a narrow, well-structured prompt with a constrained JSON output schema performs comparably to a large model on most sub-problems. The Risk Manager doesn't need to understand trading — it needs to check numbers against limits. That's a small-model task.

**Structured output contracts.** Every persona has a rigid output schema. The prompt says: "Respond ONLY with valid JSON matching this schema: `{...}`. Do not include any text outside the JSON." Small models follow this reliably when the schema is simple. This eliminates parsing overhead and keeps downstream prompts small (they receive compact JSON, not paragraphs).

**Context compression before injection.** The Context Builder summarizes large data (e.g., 100-row audit trail) into compact summaries before any LLM sees them. A background summarization pass runs on the audit trail every 15 minutes, producing: `"Last 20 trades: 14 wins, 6 losses. Win rate 70%. Average win $45, average loss $28. Regime: trending."` This is injected instead of raw data.

**Knowledge base substitutes for model knowledge.** Strategy playbooks in the RAG store mean the model doesn't need to "know" what each strategy does. It reads the playbook. This is what makes small models viable for Strategy Specialist — the knowledge is external, not parametric.

**Two-tier routing.** Hot path (routine trade intercept) → Haiku/small model. Warm path → Haiku for individual personas, Sonnet for Arbiter only. Cold path → Sonnet throughout, knowledge retrieval enabled. Estimated cost distribution: 80% hot, 15% warm, 5% cold. Average cost per decision ≈ dominated by hot path at ~$0.001.

**Prompt caching.** The system prompt for each persona is static and long. Use Anthropic prompt caching — the persona system prompt is the cacheable prefix, the dynamic context is the suffix. On Haiku with caching, repeated persona calls cost ~10% of uncached price.

---

## SECTION 9 — FAILURE MODES

### Hallucination
**Risk:** LLM invents a parameter value, price, or historical win rate.
**Mitigation:** No persona is permitted to generate numbers from "memory." All quantitative inputs come from tool calls or structured context injected by the Context Builder. The Quant Analyst's output is validated against hard ranges before the Arbiter sees it. Any numeric field outside expected bounds triggers a fallback to safe defaults, not an escalation.

### Over-trading
**Risk:** The system finds marginal-EV trades and executes them continuously, generating fees that exceed gross PnL.
**Mitigation:** Minimum EV threshold (Section 7). Global cooldown per symbol: after a completed trade, 15-minute lockout before re-entry on same symbol. Treasury Manager fee drag monitoring (40% threshold). Max orders hard limit is already implemented in the existing CIO.

### Conflicting Agent Decisions
**Risk:** Strategy Specialist says "reduce position size" while Quant Analyst says "increase position size."
**Mitigation:** Conflicts are expected and handled by the Arbiter explicitly. The Arbiter prompt includes: "Identify conflicts between verdicts. For conflicts involving risk, defer to the more conservative verdict. For conflicts involving opportunity, defer to Quant Analyst if confidence > 0.7." Unresolvable conflicts (3+ personas conflicting with roughly equal confidence) → `action: skip`. Skipping is always safe.

### Slow Response Times
**Risk:** Full panel takes > 30s, trade opportunity expires, or NATS intent times out.
**Mitigation:** Hot path must complete in < 2s — this means Risk Manager alone on hot path, single LLM call, no retrieval. If the full panel is needed and the trigger has a TTL, the orchestrator checks TTL before activating warm/cold path. Expired triggers are logged and skipped, never forced through a slow path.

### Feedback Loop / LLM-Induced Market Impact
**Risk:** The system makes a large parameter change, which causes a flurry of orders, which moves the market, which changes the regime signal, which triggers another parameter change.
**Mitigation:** After any parameter change, a 30-minute parameter freeze is imposed on that strategy. Regime Analyst output is cached for 15 minutes — the system cannot "see" a regime it just caused. Parameter changes that affect position size are capped at ±25% per adjustment event.

### Prompt Injection via Market Data
**Risk:** A bad actor embeds instructions in an asset name, news headline, or order book comment that gets injected into a persona prompt.
**Mitigation:** All market data is passed through a sanitization layer before injection. Data fields are typed and length-capped. No market data field is ever injected into a prompt as free-form text without being wrapped in a clearly labeled data block that the prompt instructs the model to treat as untrusted external data.

### Model Drift / Degraded Reasoning
**Risk:** Model behavior changes after a provider update, causing decisions that were previously consistent to become unpredictable.
**Mitigation:** Pin model versions explicitly (`claude-haiku-4-5-20251001`, not `claude-haiku-latest`). Run a weekly regression test suite: 20 known historical scenarios with known correct decisions. If pass rate drops below 90%, freeze the brain and alert.

---

## SECTION 10 — FINAL ARCHITECTURE

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│                   PETROSA AUTONOMOUS TRADING INTELLIGENCE                        │
├──────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  INPUTS                                                                          │
│  ┌─────────────────────────────────────────────────────────────────────┐         │
│  │  NATS intent.*  │  Scheduled triggers  │  Performance monitors     │         │
│  └─────────────────────────────────────────────────────────────────────┘         │
│                                    │                                             │
│                                    ▼                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐         │
│  │                        TRIGGER ROUTER                               │         │
│  │          Classifies: HOT / WARM / COLD                              │         │
│  │          Attaches: TTL, priority, trigger_type                      │         │
│  └─────────────────────────────────────────────────────────────────────┘         │
│                                    │                                             │
│                                    ▼                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐         │
│  │                       CONTEXT BUILDER                               │         │
│  │   Fetches: regime (cached), positions, strategy config              │         │
│  │   Compresses: audit trails, performance history                     │         │
│  │   Retrieves: similar past decisions (cold path only)                │         │
│  │   Sanitizes: all market data fields                                 │         │
│  └─────────────────────────────────────────────────────────────────────┘         │
│                                    │                                             │
│           ┌────────────────────────┼────────────────────────┐                   │
│           ▼                        ▼                        ▼                   │
│        HOT PATH                WARM PATH               COLD PATH                │
│      (< 2 seconds)           (< 30 seconds)          (< 5 minutes)              │
│                                                                                  │
│  ┌────────────────────────────────────────────────────────────────────┐          │
│  │                    PERSONA EXECUTION LAYER                         │          │
│  │  (Sequential, each output feeds next, all use small model)         │          │
│  │                                                                    │          │
│  │  [1] Market Regime Analyst ──────────────────────────────────────┐ │          │
│  │  [2] Risk Manager ──── BLOCK? ──── EXIT IMMEDIATELY             │ │          │
│  │  [3] Strategy Specialist (warm+cold)                             │ │          │
│  │  [4] Quant Analyst (warm+cold)                                   │ │          │
│  │  [5] Portfolio Manager (warm+cold)                               │ │          │
│  │  [6] Treasury Manager (warm+cold)                                │ │          │
│  │  [7] Knowledge Retrieval (cold only) ◄── Vector DB               │ │          │
│  │  [8] Execution Specialist (if action=execute)                    │ │          │
│  └────────────────────────────────────────────────────────────────────┘          │
│                                    │                                             │
│                                    ▼                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐         │
│  │                     DECISION ARBITER                                │         │
│  │       Receives: VerdictBundle (structured JSON from each persona)   │         │
│  │       Resolves: conflicts by conservative bias + confidence weight  │         │
│  │       Produces: Decision { action, params, confidence, trace }      │         │
│  │       Model: Sonnet-class (only LLM call that uses larger model)    │         │
│  └─────────────────────────────────────────────────────────────────────┘         │
│                                    │                                             │
│           ┌────────────────────────┼────────────────────────┐                   │
│           ▼                        ▼                        ▼                   │
│      EXECUTE                   MODIFY                    SKIP/BLOCK             │
│  → TradeEngine              → Strategy API            → Log + continue          │
│  → NATS signals.*           → Audit trail                                       │
│  → Execution plan           → Parameter freeze                                  │
│                                                                                  │
│  ┌─────────────────────────────────────────────────────────────────────┐         │
│  │                      DECISION LOGGER                                │         │
│  │  Writes: DecisionRecord to MongoDB (append-only)                    │         │
│  │  Indexes: Embedding vector into Qdrant                              │         │
│  │  Enriches: After trade close, fills outcome_metrics                 │         │
│  └─────────────────────────────────────────────────────────────────────┘         │
│                                                                                  │
│  KNOWLEDGE LAYER (shared)                                                        │
│  ┌──────────────────────┐  ┌──────────────────────┐  ┌───────────────────────┐  │
│  │  Regime Playbooks    │  │  Strategy Docs (34)   │  │  Decision History     │  │
│  │  (in-memory cache)   │  │  (in-memory cache)    │  │  (Qdrant vector DB)   │  │
│  └──────────────────────┘  └──────────────────────┘  └───────────────────────┘  │
│                                                                                  │
│  LEARNING LOOPS (background)                                                     │
│  ┌──────────────────────────────────────────────────────────────────────┐        │
│  │  • Outcome enrichment job (runs after each trade close)              │        │
│  │  • Param change correlation analysis (48h lag)                       │        │
│  │  • Weekly playbook refinement proposal (human review required)       │        │
│  │  • Monthly confidence calibration review (manual)                    │        │
│  └──────────────────────────────────────────────────────────────────────┘        │
│                                                                                  │
└──────────────────────────────────────────────────────────────────────────────────┘
```

---

### Key Design Principles (Summary)

**Sequential over parallel for personas.** Early outputs inform later ones. Parallelism is tempting but breaks the information dependency chain and produces lower-quality verdicts.

**Risk Manager is the only hard veto.** Every other persona produces weighted input. The Arbiter resolves disagreements. But Risk Manager BLOCK is a circuit breaker — it bypasses the Arbiter entirely.

**Knowledge is external, not parametric.** Small models are viable because they read strategy docs and regime playbooks rather than relying on training data. This also means the system's "knowledge" is auditable and editable.

**Every decision is a learning signal.** The DecisionRecord schema is designed from day one to support outcome enrichment. The system gets smarter with use, not just with retraining.

**Skip is always a valid action.** The system is designed to default to inaction when uncertain. Capital preservation beats missed opportunities in the short term.
