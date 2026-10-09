# System Architecture & Orchestration

**Source:** `../investigations/004-llm-prompt-guide-reference.md` (Version 1.0)

## 1. System Overview

PETROSA is an autonomous quantitative trading intelligence system running on Kubernetes. It manages 34 live trading strategies (28 in TA-bot, 6 in realtime-strategies) and makes parameter adjustment and trade execution decisions without human intervention.

The CIO (Chief Intelligence Officer) is the autonomous brain. It receives trade intents from strategies via NATS, intercepts them, and decides: execute, modify, skip, or block. It also monitors strategy health and proactively adjusts parameters when market conditions change.

### High-Level Data Flow

```
strategies → NATS intent.* → CIO → TradeEngine
                              ↕
                      Strategy APIs (TA-bot, Realtime)
                              ↕
                      petrosa-data-manager
```

**Core Responsibilities:**
- **Intercept:** Capture trade signals before execution.
- **Enforce:** Apply hard risk limits (drawdown, order counts).
- **Decide:** Evaluate market regime and strategy health to determine action.
- **Act:** Execute trades, modify strategy parameters, or block unsafe actions.

## 2. Design Principles

Four principles govern every design decision in this framework.

**Principle 1 — Code owns numbers, LLMs own language.**
Any output that is a number computed from other numbers is produced by a Python function. LLMs are called only when the input is natural language context (regime descriptions, strategy documentation, qualitative health signals) and the output is a classification. LLMs hallucinate floats. This is not a limitation to work around — it is a constraint to design for.

**Principle 2 — Minimize LLM calls per decision.**
The transition pipeline has at most three LLM calls: Regime Classifier, Strategy Assessor, and
Action Classifier. The Strategy Assessor is scheduled for removal: its qualitative output does not
justify another latency, spend, and failure boundary when deterministic strategy statistics and
existing risk gates are available. The target pipeline has two calls, and the HOT path remains at
zero when cached assessments are valid.

**Principle 3 — Schemas must have four fields or fewer.**
Every LLM output schema has at most four top-level fields. Above this limit, small models begin omitting fields, inventing field names, or producing malformed JSON. Smaller schemas mean fewer parse failures and cheaper outputs.

**Principle 4 — Determinism through hard gates, not LLM judgment.**
All hard limits (drawdown, order counts, minimum EV) are enforced by code before any LLM is called. LLMs are never in a position to "decide" whether to override a safety limit. The code blocks the action before the LLM sees the request.

## 3. Code vs LLM Decision Matrix

Before any prompt is written, every decision must be classified. This table is the authoritative reference:

| Decision | Owner | Reason |
|---|---|---|
| Hard limit checks (drawdown, max orders) | **Code** | Binary, deterministic, safety-critical |
| Expected value calculation | **Code** | Arithmetic — LLMs hallucinate floats |
| Kelly fraction | **Code** | Formula with no ambiguity |
| Fee + slippage + funding cost | **Code** | Arithmetic |
| Position size | **Code** | Derived from Kelly × scale factors |
| Stop loss / take profit levels | **Code** | Strategy default × lookup multiplier |
| Capital utilisation ratio | **Code** | Division |
| Portfolio concentration check | **Code** | Sum and comparison |
| Trigger routing (HOT/WARM/COLD) | **Code** | Switch statement on trigger_type enum |
| Order type selection | **Code** | Rule: spread_pct < threshold → limit |
| Parameter value computation | **Code** | Direction signal × step size × schema bounds |
| Hard exit triggers | **Code** | Stop loss / take profit / time expiry comparisons |
| Market regime classification | **LLM** | Multi-signal pattern recognition |
| Strategy qualitative fit assessment | **Retire** | The live assessor is advisory and duplicates deterministic health/risk signals |
| Action classification | **LLM** | Synthesising flags + qualitative assessments |
| Parameter change justification | **LLM** | Audit trace grounded in strategy docs |

The current split is roughly 75% code, 25% LLM. The target split removes the advisory strategy-fit
call; code continues to handle all quantitative and safety-critical work.

## 4. Strategy Analysis Stage Decision

The repository contains a live `StrategyAssessor` call in the transition pipeline, but the MCP
configuration server described by the old integration documentation no longer exists. Rebuilding
that MCP surface would restore configuration tooling, not decision quality. The assessor receives
strategy statistics and regime fields already available from the Data Manager and returns
qualitative health, fit, activation, and optional parameter-change suggestions. Those outputs add
latency and an LLM failure mode while deterministic EV, sizing, drawdown, pause, and keep/kill
controls remain authoritative.

**Recommendation: remove the strategy-specific LLM analysis stage; do not rebuild MCP.** A separate
implementation ticket must first replace the assessor-derived audit fields with deterministic
values or explicit unavailable markers, then remove its call, model, prompt, cache key, and tests.
The existing net-EV gate, risk limits, sizing, context-gap handling, and keep/kill controls remain
unchanged. The Strategy Assessor should not be removed in this spike because the current runtime
still calls it.

**Effort estimate:** rebuilding MCP is approximately 3–5 engineer-days for a server contract,
audit/auth wiring, integration tests, deployment, and operator documentation, with no identified
decision benefit. Removing the stage is approximately 1–2 engineer-days for the runtime contract,
audit-field migration, focused tests, and documentation; it is the lower-risk option.

**Alternatives considered:** retain the assessor unchanged (rejected: redundant cost and failure
surface); retain it as advisory-only (interim compatibility only, not a target design); or replace
it with deterministic health and regime-fit rules (preferred implementation boundary).

## 5. Orchestration Pipeline

```
TRIGGER RECEIVED (NATS intent.* or scheduled)
              │
              ▼
┌─────────────────────────────────────────────────────────┐
│                   TRIGGER GATE (code)                   │
│                                                         │
│  1. Parse trigger_type and payload                      │
│  2. Run hard limit checks against RiskLimits config     │
│  3. If any hard limit breached → BLOCK, exit immediately│
│  4. Check TTL — if expired → discard, log, exit         │
│  5. Route to HOT / WARM / COLD path                     │
└────────────────────────┬────────────────────────────────┘
                         │
          ┌──────────────┼──────────────┐
          │              │              │
        HOT            WARM           COLD
       path            path           path
          │              │              │
          ▼              ▼              ▼
┌──────────────────────────────────────────────────────────┐
│                   CODE ENGINE (all paths)                │
│                                                         │
│  - EV Calculation                                       │
│  - Cost Analysis                                        │
│  - Position Sizing                                      │
│  - Trade Parameter Generation                           │
└──────────────────────────────┬───────────────────────────┘
                               │  CodeEngineResult
                               ▼
┌──────────────────────────────────────────────────────────┐
│              LLM CALLS (conditional on path)             │
│                                                          │
│  [1] REGIME CLASSIFIER (Haiku, cached; transition)       │
│  [2] STRATEGY ASSESSOR (Haiku, cached; scheduled removal)│
│  [3] ACTION CLASSIFIER (Haiku, unique per call)          │
└──────────────────────────────┬───────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────┐
│              DECISION ASSEMBLER (code)                   │
│                                                          │
│  Combines CodeEngineResult + LLM classification outputs  │
│  Applies activation_recommendation scale to position     │
│  Converts param direction signal to concrete value       │
│  Produces DecisionResult struct                          │
└──────────────────────────────┬───────────────────────────┘
                               │  DecisionResult
                               ▼
┌──────────────────────────────────────────────────────────┐
│              OUTPUT ROUTER (code)                        │
│                                                          │
│  Execute / Modify / Skip / Block / Pause / Escalate      │
└──────────────────────────────┬───────────────────────────┘
```
