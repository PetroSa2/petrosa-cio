from pydantic import BaseModel, Field


class CodeEngineResult(BaseModel):
    """
    Deterministic quantitative calculations from the Code Engine.
    These fields provide the 'ground truth' for all subsequent reasoning.
    """

    # 1. Risk Gates
    hard_blocked: bool = Field(
        False, description="If True, all further reasoning is bypassed"
    )
    block_reason: str | None = None
    block_context_fallback: bool = Field(
        False,
        description=(
            "True when hard_blocked was caused by the portfolio/risk "
            "surface's context-fetch fallback (safe conservative defaults "
            "returned by ContextBuilder._fetch_portfolio_and_risk on "
            "failure), NOT a real drawdown/open-orders breach observed "
            "from live tradeengine data. See #172 — this flag exists so a "
            "context-fetch outage is never misdiagnosed as a real risk "
            "breach (or vice versa)."
        ),
    )

    # 2. EV Analysis
    gross_ev: float | None = None
    ev_unavailable: bool = Field(
        False, description="True if required stats (win_rate) are missing"
    )

    # 3. Position Sizing
    kelly_fraction: float | None = None
    kelly_position_usd: float | None = None

    # 4. Recommended Parameters (Initial values from defaults + volatility adjustments)
    recommended_sl_pct: float | None = None
    recommended_tp_pct: float | None = None
    leverage: float = 1.0

    # 5. Metadata/Audit
    risk_warnings: list[str] = Field(default_factory=list)
