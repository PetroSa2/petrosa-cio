"""
Trading Configuration Models for Runtime Parameter Management.

This module defines the data models for trading configurations that control
execution parameters, risk management, and strategy behavior on a per-symbol
and per-position-side basis.
"""

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class TradingConfig(BaseModel):
    """
    Trading configuration model.

    Represents a complete configuration for trading execution,
    either global (applied to all symbols) or symbol/side-specific.

    Configuration Hierarchy:
    - Global: Applies to all symbols and all sides
    - Symbol: Applies to specific symbol, both LONG and SHORT
    - Symbol-Side: Applies to specific symbol and specific side only
    """

    id: str | None = Field(None, description="Configuration ID")

    # Scope identifiers
    strategy_id: str | None = Field(
        None, description="Strategy identifier for strategy-scoped configurations"
    )
    symbol: str | None = Field(
        None, description="Trading symbol (None for global configs, e.g., 'BTCUSDT')"
    )
    side: Literal["LONG", "SHORT"] | None = Field(
        None, description="Position side (None for global/symbol configs)"
    )

    # Configuration data
    parameters: dict[str, Any] = Field(
        ..., description="Trading parameters as key-value pairs"
    )

    # Versioning
    version: int = Field(
        1, description="Configuration version number (incremented on updates)"
    )

    # Timestamps
    created_at: datetime = Field(
        default_factory=datetime.utcnow, description="When configuration was created"
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        description="When configuration was last updated",
    )

    # Audit
    created_by: str = Field(
        ..., description="Who/what created this config (e.g., 'llm_agent_v1', 'admin')"
    )

    # Additional metadata
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional metadata (notes, performance metrics, etc.)",
    )

    @field_validator("side")
    @classmethod
    def validate_side(cls, v: str | None) -> str | None:
        """Validate position side."""
        if v is not None and v not in ["LONG", "SHORT"]:
            raise ValueError("Side must be 'LONG' or 'SHORT'")
        return v

    def get_scope_key(self) -> str:
        """Get unique scope key for this config."""
        if self.side:
            return f"{self.symbol}:{self.side}"
        elif self.symbol:
            return self.symbol
        return "global"

    model_config = {
        "json_schema_extra": {
            "example": {
                "id": "507f1f77bcf86cd799439011",  # pragma: allowlist secret
                "symbol": "BTCUSDT",
                "side": "LONG",
                "parameters": {
                    "leverage": 10,
                    "position_size_pct": 0.1,
                    "stop_loss_pct": 2.0,
                    "take_profit_pct": 5.0,
                },
                "version": 3,
                "created_at": "2025-10-20T10:30:00Z",
                "updated_at": "2025-10-20T14:45:00Z",
                "created_by": "llm_agent_v1",
                "metadata": {
                    "notes": "Optimized for BTC long positions",
                    "performance": "+15% win rate",
                },
            }
        }
    }


class TradingConfigAudit(BaseModel):
    """
    Audit trail record for trading configuration changes.

    Tracks all modifications to trading configurations for compliance,
    debugging, and performance analysis.
    """

    id: str | None = Field(None, description="Audit record ID")

    # Scope of change
    config_type: Literal["global", "symbol", "symbol_side"] = Field(
        ..., description="Type of configuration that was changed"
    )
    symbol: str | None = Field(None, description="Symbol if applicable")
    side: Literal["LONG", "SHORT"] | None = Field(
        None, description="Side if applicable"
    )

    # Change details
    action: Literal["create", "update", "delete"] = Field(
        ..., description="Action performed"
    )

    # Before/after state
    parameters_before: dict[str, Any] | None = Field(
        None, description="Parameters before the change"
    )
    parameters_after: dict[str, Any] | None = Field(
        None, description="Parameters after the change"
    )
    version_before: int | None = Field(
        None, description="Version number before the change"
    )
    version_after: int | None = Field(
        None, description="Version number after the change"
    )

    # Audit metadata
    changed_by: str = Field(..., description="Who/what made the change")
    reason: str | None = Field(None, description="Reason for the change")
    timestamp: datetime = Field(
        default_factory=datetime.utcnow, description="When the change occurred"
    )

    # Additional context
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="Additional audit metadata"
    )

    def get_change_summary(self) -> str:
        """Get human-readable summary of the change."""
        scope = f"{self.symbol or 'global'}"
        if self.side:
            scope += f"-{self.side}"
        return f"{self.action.upper()} {scope} by {self.changed_by}"

    model_config = {
        "json_schema_extra": {
            "example": {
                "id": "audit-123",
                "config_type": "symbol_side",
                "symbol": "BTCUSDT",
                "side": "LONG",
                "action": "update",
                "parameters_before": {"leverage": 10},
                "parameters_after": {"leverage": 15},
                "version_before": 2,
                "version_after": 3,
                "changed_by": "llm_agent_v1",
                "reason": "Increasing leverage for bullish market conditions",
                "timestamp": "2025-10-20T15:00:00Z",
                "metadata": {"ip_address": "192.168.1.1"},
            }
        }
    }


class LeverageStatus(BaseModel):
    """
    Leverage status tracking model.

    Tracks the difference between configured leverage (in our system)
    and actual leverage (on Binance exchange) for each symbol.
    """

    id: str | None = Field(None, description="Status record ID")

    symbol: str = Field(..., description="Trading symbol")

    # Leverage values
    configured_leverage: int = Field(
        ..., description="Leverage configured in our system", ge=1, le=125
    )
    actual_leverage: int | None = Field(
        None, description="Actual leverage on Binance (None if unknown)", ge=1, le=125
    )

    # Sync status
    last_sync_at: datetime | None = Field(
        None, description="When leverage was last synced with Binance"
    )
    last_sync_success: bool = Field(
        False, description="Whether last sync attempt was successful"
    )
    last_sync_error: str | None = Field(
        None, description="Error message if sync failed"
    )

    # Metadata
    updated_at: datetime = Field(
        default_factory=datetime.utcnow, description="When this record was last updated"
    )

    def is_synced(self) -> bool:
        """Check if configured and actual leverage match."""
        if self.actual_leverage is None:
            return False
        return self.configured_leverage == self.actual_leverage

    def needs_sync(self) -> bool:
        """Check if leverage needs to be synced."""
        return not self.is_synced()

    model_config = {
        "json_schema_extra": {
            "example": {
                "id": "lev-status-btc",
                "symbol": "BTCUSDT",
                "configured_leverage": 10,
                "actual_leverage": 10,
                "last_sync_at": "2025-10-20T15:00:00Z",
                "last_sync_success": True,
                "last_sync_error": None,
                "updated_at": "2025-10-20T15:00:00Z",
            }
        }
    }
