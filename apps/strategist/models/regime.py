from typing import Dict, Optional, Tuple
from pydantic import BaseModel, Field
from datetime import datetime

from .enums import RegimeEnum, DataManagerRegimeEnum, ConfidenceLevel, VolatilityLevel

# Locked Mapping from Data-Manager's specific regimes to CIO's internal framework regimes
REGIME_MAPPING: Dict[DataManagerRegimeEnum, RegimeEnum] = {
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

def _map_confidence(conf_float: float) -> ConfidenceLevel:
    """Helper to convert API confidence float to internal enum based on business rules."""
    if conf_float >= CONFIDENCE_THRESHOLDS["high"]:
        return ConfidenceLevel.HIGH
    if conf_float >= CONFIDENCE_THRESHOLDS["medium"]:
        return ConfidenceLevel.MEDIUM
    return ConfidenceLevel.LOW

class RegimeAPIData(BaseModel):
    """Inner 'data' block from petrosa-data-manager /regime API."""
    regime: DataManagerRegimeEnum
    volatility_level: VolatilityLevel
    volume_level: str
    trend_direction: str
    confidence: str        # Received as string (e.g., "0.85") from API

class RegimeAPIMetadata(BaseModel):
    """Metadata block from petrosa-data-manager /regime API."""
    timestamp: datetime
    collection: str

class RegimeAPIResponse(BaseModel):
    """Raw response structure from petrosa-data-manager /analysis/regime API."""
    pair: str
    metric: str
    data: RegimeAPIData
    metadata: RegimeAPIMetadata

class RegimeResult(BaseModel):
    """
    Internal framework representation of market regime.
    Used by LLM personas and Decision Arbiter.
    """
    regime: RegimeEnum
    regime_confidence: ConfidenceLevel
    primary_signal: str = Field(..., description="The main data point that drove this classification")
    thought_trace: str = Field(..., description="Short explanation of the classification reasoning")

    @classmethod
    def from_api_response(cls, response: RegimeAPIResponse) -> "RegimeResult":
        """
        Translates raw Data-Manager API response into the internal CIO framework.
        
        Conversion Logic:
        1. Handle 'unknown' case specifically as 'choppy' + 'low'.
        2. Map Data-Manager regime string to internal RegimeEnum.
        3. Convert confidence float string (e.g., "0.85") into high|medium|low enum.
        """
        api_data = response.data
        api_regime = api_data.regime
        
        # 1. Handle explicit 'unknown' case
        if api_regime == DataManagerRegimeEnum.UNKNOWN:
            return cls(
                regime=RegimeEnum.CHOPPY,
                regime_confidence=ConfidenceLevel.LOW,
                primary_signal="data_manager_unknown",
                thought_trace="Data-manager returned unknown regime; defaulting to safe choppy/low state."
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
            primary_signal=f"{api_regime.value}_conf_{conf_float}",
            thought_trace=(
                f"Mapped {api_regime} (conf={conf_float}) to {internal_regime}/{confidence.value}."
            )
        )
