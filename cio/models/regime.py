import os
from datetime import datetime

from pydantic import BaseModel, Field

from cio.models.enums import (
    ConfidenceLevel,
    DataManagerRegimeEnum,
    RegimeEnum,
    VolatilityLevel,
)

# Locked Mapping from Data-Manager's specific regimes to CIO's internal framework regimes
REGIME_MAPPING: dict[DataManagerRegimeEnum, RegimeEnum] = {
    DataManagerRegimeEnum.STABLE_ACCUMULATION: RegimeEnum.RECOVERY,
    DataManagerRegimeEnum.BULLISH_ACCELERATION: RegimeEnum.TRENDING_BULL,
    DataManagerRegimeEnum.BEARISH_ACCELERATION: RegimeEnum.TRENDING_BEAR,
    DataManagerRegimeEnum.BREAKOUT_PHASE: RegimeEnum.BREAKOUT_PHASE,
    DataManagerRegimeEnum.CONSOLIDATION: RegimeEnum.RANGING,
    DataManagerRegimeEnum.BALANCED_MARKET: RegimeEnum.RANGING,
    DataManagerRegimeEnum.TURBULENT_ILLIQUIDITY: RegimeEnum.HIGH_VOLATILITY,
    DataManagerRegimeEnum.TRANSITIONAL: RegimeEnum.CHOPPY,
}

CONFIDENCE_THRESHOLDS = {
    "high": 0.80,
    "medium": 0.70,
}

#: The regime confidence below which a regime counts as LOW (unavailable: probe size only, no hard block).
#: A labelled fallback, overridable with ``CIO_REGIME_MIN_CONFIDENCE`` (petrosa-cio#294, operator ruling on #309).
DEFAULT_REGIME_MIN_CONFIDENCE = CONFIDENCE_THRESHOLDS["medium"]


def regime_min_confidence() -> tuple[float, str]:
    """(value, source): the minimum regime confidence, from ``CIO_REGIME_MIN_CONFIDENCE`` (source ``env``),
    else the 0.70 fallback (source ``fallback``). An unreadable or out-of-range value is the fallback."""
    try:
        value = float(os.environ["CIO_REGIME_MIN_CONFIDENCE"])
    except (KeyError, ValueError):
        return DEFAULT_REGIME_MIN_CONFIDENCE, "fallback"
    if 0.0 < value <= 1.0:
        return value, "env"
    return DEFAULT_REGIME_MIN_CONFIDENCE, "fallback"


def _map_confidence(conf_float: float) -> ConfidenceLevel:
    """Helper to convert API confidence float to internal enum based on business rules."""
    minimum = regime_min_confidence()[0]
    if conf_float >= max(CONFIDENCE_THRESHOLDS["high"], minimum):
        return ConfidenceLevel.HIGH
    if conf_float >= minimum:
        return ConfidenceLevel.MEDIUM
    return ConfidenceLevel.LOW


class RegimeAPIData(BaseModel):
    """Inner 'data' block from petrosa-data-manager /regime API."""

    regime: DataManagerRegimeEnum
    volatility_level: VolatilityLevel
    volume_level: str
    trend_direction: str
    confidence: str | float  # Support both string and float from API


class RegimeAPIMetadata(BaseModel):
    """Metadata block from petrosa-data-manager /regime API."""

    timestamp: datetime
    collection: str


class RegimeAPIResponse(BaseModel):
    """Raw response structure from petrosa-data-manager /analysis/regime API."""

    pair: str
    metric: str
    data: RegimeAPIData | None = None
    metadata: RegimeAPIMetadata | None = None


class RegimeResult(BaseModel):
    """
    Internal framework representation of market regime.
    Used by LLM personas and Decision Arbiter.
    """

    regime: RegimeEnum
    regime_confidence: ConfidenceLevel
    volatility_level: VolatilityLevel
    # The data-manager regime name this result was mapped from (None when the regime did not come from
    # data-manager); slippage is measured per data-manager regime.
    data_manager_regime: str | None = None
    # When data-manager computed the regime (its metadata timestamp); None when unknown. A regime older
    # than max(3 x the analyzer interval, 1 h) is stale (petrosa-cio#294).
    computed_at: datetime | None = None
    # The numeric confidence data-manager reported (None when it did not come from data-manager).
    confidence_value: float | None = None
    primary_signal: str = Field(
        ..., description="The main data point that drove this classification"
    )
    thought_trace: str = Field(
        ...,
        description="Short explanation of the classification reasoning (optional for minimal LLM profile)",
    )

    @classmethod
    def from_api_response(cls, response: RegimeAPIResponse) -> "RegimeResult":
        """
        Translates raw Data-Manager API response into the internal CIO framework.

        Conversion Logic:
        1. Handle missing data case.
        2. Handle 'unknown' case specifically as 'choppy' + 'low'.
        3. Map Data-Manager regime string to internal RegimeEnum.
        4. Convert confidence float string (e.g., "0.85") into high|medium|low enum.
        """
        api_data = response.data

        # 1. Handle missing data
        if not api_data:
            return cls(
                regime=RegimeEnum.CHOPPY,
                regime_confidence=ConfidenceLevel.LOW,
                volatility_level=VolatilityLevel.MEDIUM,
                primary_signal="data_manager_empty",
                thought_trace="Data-manager returned empty data block; defaulting to safe choppy/low state.",
            )

        api_regime = api_data.regime

        # 2. Handle explicit 'unknown' case
        if api_regime == DataManagerRegimeEnum.UNKNOWN:
            return cls(
                regime=RegimeEnum.CHOPPY,
                regime_confidence=ConfidenceLevel.LOW,
                volatility_level=VolatilityLevel.MEDIUM,
                primary_signal="data_manager_unknown",
                thought_trace="Data-manager returned unknown regime; defaulting to safe choppy/low state.",
            )

        # 2. Extract confidence
        try:
            conf_float = float(api_data.confidence)
        except (ValueError, TypeError):
            conf_float = 0.0

        confidence = _map_confidence(conf_float)

        # 3. Map regime using the locked translation table
        internal_regime = REGIME_MAPPING.get(api_regime, RegimeEnum.CHOPPY)

        return cls(
            regime=internal_regime,
            regime_confidence=confidence,
            volatility_level=api_data.volatility_level,
            primary_signal=f"{api_regime.value}_conf_{conf_float}",
            data_manager_regime=api_regime.value,
            confidence_value=conf_float,
            computed_at=response.metadata.timestamp if response.metadata else None,
            thought_trace=(
                f"Mapped {api_regime} (conf={conf_float}) to {internal_regime}/{confidence.value}."
            ),
        )
