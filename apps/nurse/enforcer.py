"""Deterministic Nurse policy enforcement."""

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from apps.nurse.defaults import (
    get_default_parameters,
    merge_parameters,
    validate_parameters,
)
from apps.nurse.guard import RegimeGuard
from contracts.trading_config import TradingConfig, TradingConfigAudit

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class EnforcerResult:
    """Outcome of intent policy enforcement."""

    approved: bool
    reason: str | None = None
    metadata: dict[str, Any] | None = None


class ConfigSnapshotter:
    """Stores pre-change snapshots for rollback workflows."""

    def __init__(self, history_collection: Any | None = None):
        self.history_collection = history_collection

    async def snapshot(
        self,
        *,
        model_name: str,
        payload: dict[str, Any],
        source_audit_id: str | None = None,
    ) -> dict[str, Any]:
        document = {
            "model": model_name,
            "payload": payload,
            "source_audit_id": source_audit_id,
            "snapshot_at": datetime.now(UTC).isoformat(),
        }
        if self.history_collection is not None:
            await self.history_collection.insert_one(document)
        return document


class NurseEnforcer:
    """Simple deterministic validation gate for incoming intent payloads."""

    VALID_ACTIONS = {"buy", "sell", "hold", "close"}

    def __init__(self, regime_guard: RegimeGuard | None = None):
        self.regime_guard = regime_guard or RegimeGuard()

    @staticmethod
    def scale_position_size(
        intent_payload: dict[str, Any], scale_factor: float
    ) -> float:
        quantity = float(intent_payload.get("quantity", 0.0))
        return quantity * scale_factor

    async def enforce(self, intent_payload: dict[str, Any]) -> EnforcerResult:
        action = str(intent_payload.get("action", "")).lower()
        if action not in self.VALID_ACTIONS:
            return EnforcerResult(approved=False, reason="invalid_action")

        confidence = intent_payload.get("confidence")
        if confidence is not None:
            try:
                confidence_value = float(confidence)
            except (TypeError, ValueError):
                return EnforcerResult(approved=False, reason="invalid_confidence")
            if not 0.0 <= confidence_value <= 1.0:
                return EnforcerResult(approved=False, reason="invalid_confidence")

        guard_decision = await self.regime_guard.evaluate(intent_payload)
        metadata = dict(guard_decision.metadata)
        metadata["saved_capital"] = guard_decision.saved_capital

        if guard_decision.scale_factor < 1.0:
            metadata["scaled_quantity"] = self.scale_position_size(
                intent_payload, guard_decision.scale_factor
            )
            metadata["scale_factor"] = guard_decision.scale_factor

        if not guard_decision.approved:
            metadata["veto_type"] = "semantic"
            return EnforcerResult(
                approved=False,
                reason=guard_decision.veto_reason,
                metadata=metadata,
            )

        return EnforcerResult(approved=True, metadata=metadata)


class TradingConfigManager:
    """
    Trading configuration manager with cache + layered resolution.

    This is a migration target from tradeengine/config_manager.py adapted
    for petrosa-cio runtime layout and data adapters.
    """

    def __init__(
        self,
        mongodb_client: Any | None = None,
        redis_client: Any | None = None,
        cache_ttl_seconds: int = 60,
    ):
        self.mongodb_client = mongodb_client
        self.redis_client = redis_client
        self.cache_ttl_seconds = cache_ttl_seconds
        self._cache: dict[str, tuple[dict[str, Any], float]] = {}
        self._cache_refresh_task: asyncio.Task[Any | None] = None
        self._running = False

    async def start(self) -> None:
        if self.mongodb_client and hasattr(self.mongodb_client, "connect"):
            await self.mongodb_client.connect()
        if self.redis_client and hasattr(self.redis_client, "connect"):
            await self.redis_client.connect()
        self._running = True
        self._cache_refresh_task = asyncio.create_task(self._cache_refresh_loop())

    async def stop(self) -> None:
        self._running = False
        if self._cache_refresh_task:
            self._cache_refresh_task.cancel()
            try:
                await self._cache_refresh_task
            except asyncio.CancelledError:
                pass
        if self.mongodb_client and hasattr(self.mongodb_client, "disconnect"):
            await self.mongodb_client.disconnect()
        if self.redis_client and hasattr(self.redis_client, "disconnect"):
            await self.redis_client.disconnect()

    async def _cache_refresh_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self.cache_ttl_seconds)
                now = time.time()
                expired = [
                    key
                    for key, (_, ts) in self._cache.items()
                    if now - ts > self.cache_ttl_seconds
                ]
                for key in expired:
                    del self._cache[key]
            except Exception as exc:  # pragma: no cover - background resilience
                logger.error(f"Cache refresh loop error: {exc}")
                await asyncio.sleep(5)

    def _get_cache_key(
        self,
        symbol: str | None,
        side: str | None,
        strategy_id: str | None = None,
    ) -> str:
        return f"{symbol or 'global'}:{side or 'all'}:{strategy_id or 'all_strategies'}"

    async def get_config(
        self,
        symbol: str | None = None,
        side: str | None = None,
        strategy_id: str | None = None,
    ) -> dict[str, Any]:
        cache_key = self._get_cache_key(symbol, side, strategy_id)
        if cache_key in self._cache:
            cfg, ts = self._cache[cache_key]
            if time.time() - ts < self.cache_ttl_seconds:
                return cfg.copy()

        resolved = get_default_parameters()

        try:
            if self.mongodb_client and getattr(self.mongodb_client, "connected", False):
                global_cfg = await self.mongodb_client.get_global_config()
                if global_cfg:
                    resolved = merge_parameters(resolved, global_cfg.parameters)

                if symbol:
                    symbol_cfg = await self.mongodb_client.get_symbol_config(symbol)
                    if symbol_cfg:
                        resolved = merge_parameters(resolved, symbol_cfg.parameters)

                if strategy_id:
                    strategy_cfg = await self.mongodb_client.get_strategy_config(
                        strategy_id
                    )
                    if strategy_cfg:
                        resolved = merge_parameters(resolved, strategy_cfg.parameters)

                if symbol and side:
                    symbol_side_cfg = await self.mongodb_client.get_symbol_side_config(
                        symbol, side
                    )
                    if symbol_side_cfg:
                        resolved = merge_parameters(
                            resolved, symbol_side_cfg.parameters
                        )
        except Exception as exc:  # pragma: no cover - fallback behavior
            logger.error(f"Error resolving config: {exc}")

        self._cache[cache_key] = (resolved, time.time())
        return resolved.copy()

    async def set_config(
        self,
        parameters: dict[str, Any],
        changed_by: str,
        symbol: str | None = None,
        side: str | None = None,
        strategy_id: str | None = None,
        reason: str | None = None,
        validate_only: bool = False,
    ) -> tuple[bool, TradingConfig | None, list[str]]:
        potential_pnl = parameters.get("potential_pnl", 0.0)
        blocked_reason = parameters.get("blocked_reason")
        clean_parameters = {
            k: v for k, v in parameters.items() if k not in {"potential_pnl", "blocked_reason"}
        }

        is_valid, errors = validate_parameters(clean_parameters)
        if not is_valid:
            return False, None, errors
        if validate_only:
            return True, None, []

        config_type = (
            "symbol_side" if symbol and side else "symbol" if symbol else "global"
        )

        existing = None
        if self.mongodb_client and getattr(self.mongodb_client, "connected", False):
            if config_type == "global":
                existing = await self.mongodb_client.get_global_config()
            elif config_type == "symbol":
                existing = await self.mongodb_client.get_symbol_config(symbol)
            else:
                existing = await self.mongodb_client.get_symbol_side_config(
                    symbol, side
                )

        version = (existing.version + 1) if existing else 1
        now = datetime.utcnow()
        metadata = {
            "potential_pnl": float(potential_pnl),
            "blocked_reason": blocked_reason,
        }
        new_config = TradingConfig(
            strategy_id=strategy_id,
            symbol=symbol,
            side=side,
            parameters=clean_parameters,
            version=version,
            created_at=existing.created_at if existing else now,
            updated_at=now,
            created_by=changed_by,
            metadata=metadata,
        )

        success = False
        if self.mongodb_client and getattr(self.mongodb_client, "connected", False):
            if config_type == "global":
                success = await self.mongodb_client.set_global_config(new_config)
            elif config_type == "symbol":
                success = await self.mongodb_client.set_symbol_config(new_config)
            else:
                success = await self.mongodb_client.set_symbol_side_config(new_config)

        if not success:
            return False, None, ["Failed to save configuration"]

        audit = TradingConfigAudit(
            config_type=config_type,  # type: ignore[arg-type]
            symbol=symbol,
            side=side,  # type: ignore[arg-type]
            action="update" if existing else "create",
            parameters_before=existing.parameters if existing else None,
            parameters_after=clean_parameters,
            version_before=existing.version if existing else None,
            version_after=version,
            changed_by=changed_by,
            reason=reason,
            timestamp=now,
            metadata=metadata,
        )
        if self.mongodb_client and getattr(self.mongodb_client, "connected", False):
            await self.mongodb_client.add_audit_record(audit)

        self.invalidate_cache(symbol=symbol, side=side, strategy_id=strategy_id)
        return True, new_config, []

    async def get_previous_config(
        self,
        symbol: str | None = None,
        side: str | None = None,
    ) -> dict[str, Any] | None:
        if not self.mongodb_client or not getattr(
            self.mongodb_client, "connected", False
        ):
            return None
        history = await self.mongodb_client.get_config_history(
            symbol=symbol, side=side, limit=2
        )
        if len(history) < 1:
            return None
        latest = history[0]
        if latest.get("action") == "update" and latest.get("parameters_before"):
            return latest["parameters_before"]
        if len(history) >= 2 and history[1].get("parameters_after"):
            return history[1]["parameters_after"]
        return None

    async def get_config_by_version(
        self,
        version: int,
        symbol: str | None = None,
        side: str | None = None,
    ) -> dict[str, Any] | None:
        if version < 1:
            return None
        if not self.mongodb_client or not getattr(
            self.mongodb_client, "connected", False
        ):
            return None
        config_type = (
            "symbol_side" if symbol and side else "symbol" if symbol else "global"
        )
        record = await self.mongodb_client.get_audit_record_by_version(
            version=version,
            config_type=config_type,
            symbol=symbol,
            side=side,
        )
        if record and record.get("parameters_after"):
            return record["parameters_after"]
        return None

    async def get_config_by_id(
        self,
        audit_id: str,
        symbol: str | None = None,
        side: str | None = None,
    ) -> dict[str, Any] | None:
        if not self.mongodb_client or not getattr(
            self.mongodb_client, "connected", False
        ):
            return None
        record = await self.mongodb_client.get_audit_record_by_id(audit_id)
        if not record:
            return None
        if symbol != record.get("symbol"):
            return None
        if side != record.get("side"):
            return None
        return record.get("parameters_after")

    async def rollback_config(
        self,
        changed_by: str,
        symbol: str | None = None,
        side: str | None = None,
        target_version: int | None = None,
        rollback_id: str | None = None,
        reason: str | None = None,
    ) -> tuple[bool, TradingConfig | None, list[str]]:
        if rollback_id:
            params = await self.get_config_by_id(rollback_id, symbol=symbol, side=side)
            if not params:
                return False, None, [f"Audit record {rollback_id} not found"]
        elif target_version is not None:
            if target_version < 1:
                return False, None, ["Invalid version number (must be >= 1)"]
            params = await self.get_config_by_version(
                target_version, symbol=symbol, side=side
            )
            if not params:
                return False, None, [f"Version {target_version} not found"]
        else:
            params = await self.get_previous_config(symbol=symbol, side=side)
            if not params:
                return False, None, ["No previous configuration found"]

        return await self.set_config(
            parameters=params,
            changed_by=changed_by,
            symbol=symbol,
            side=side,
            reason=reason or "rollback",
        )

    async def delete_config(
        self,
        changed_by: str,
        symbol: str | None = None,
        side: str | None = None,
        reason: str | None = None,
    ) -> tuple[bool, list[str]]:
        config_type = (
            "symbol_side" if symbol and side else "symbol" if symbol else "global"
        )
        existing = None
        if self.mongodb_client and getattr(self.mongodb_client, "connected", False):
            if config_type == "global":
                existing = await self.mongodb_client.get_global_config()
                success = await self.mongodb_client.delete_global_config()
            elif config_type == "symbol":
                existing = await self.mongodb_client.get_symbol_config(symbol)
                success = await self.mongodb_client.delete_symbol_config(symbol)
            else:
                existing = await self.mongodb_client.get_symbol_side_config(
                    symbol, side
                )
                success = await self.mongodb_client.delete_symbol_side_config(
                    symbol, side
                )
        else:
            success = False

        if not success:
            return False, ["Failed to delete configuration"]

        if (
            existing
            and self.mongodb_client
            and getattr(self.mongodb_client, "connected", False)
        ):
            audit = TradingConfigAudit(
                config_type=config_type,  # type: ignore[arg-type]
                symbol=symbol,
                side=side,  # type: ignore[arg-type]
                action="delete",
                parameters_before=existing.parameters,
                parameters_after=None,
                version_before=existing.version,
                version_after=None,
                changed_by=changed_by,
                reason=reason,
                timestamp=datetime.utcnow(),
            )
            await self.mongodb_client.add_audit_record(audit)

        self.invalidate_cache(symbol=symbol, side=side)
        return True, []

    def invalidate_cache(
        self,
        symbol: str | None = None,
        side: str | None = None,
        strategy_id: str | None = None,
    ) -> None:
        keys_to_delete: list[str] = []
        for cache_key in list(self._cache.keys()):
            symbol_part, side_part, strategy_part = cache_key.split(":", 2)
            if symbol is not None and symbol_part != symbol:
                continue
            if side is not None and side_part != side:
                continue
            if strategy_id is not None and strategy_part != strategy_id:
                continue
            keys_to_delete.append(cache_key)
        for key in keys_to_delete:
            del self._cache[key]
