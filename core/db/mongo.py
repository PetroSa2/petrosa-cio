"""Async MongoDB adapter for trading config persistence."""

from __future__ import annotations

from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient

from contracts.trading_config import TradingConfig, TradingConfigAudit


class MongoConfigAdapter:
    """MongoDB adapter with methods used by TradingConfigManager."""

    def __init__(self, uri: str, db_name: str = "petrosa_cio"):
        self.uri = uri
        self.db_name = db_name
        self.client: AsyncIOMotorClient | None = None
        self.connected = False

    @property
    def db(self):
        if self.client is None:
            raise RuntimeError("Mongo adapter not connected")
        return self.client[self.db_name]

    async def connect(self) -> None:
        self.client = AsyncIOMotorClient(self.uri)
        await self.client.admin.command("ping")
        self.connected = True

    async def disconnect(self) -> None:
        if self.client is not None:
            self.client.close()
        self.connected = False

    async def get_global_config(self) -> TradingConfig | None:
        doc = await self.db.trading_configs.find_one({"symbol": None, "side": None})
        return TradingConfig(**doc) if doc else None

    async def get_symbol_config(self, symbol: str) -> TradingConfig | None:
        doc = await self.db.trading_configs.find_one({"symbol": symbol, "side": None})
        return TradingConfig(**doc) if doc else None

    async def get_strategy_config(self, strategy_id: str) -> TradingConfig | None:
        doc = await self.db.trading_configs.find_one(
            {"strategy_id": strategy_id, "symbol": None, "side": None}
        )
        return TradingConfig(**doc) if doc else None

    async def get_symbol_side_config(
        self, symbol: str, side: str
    ) -> TradingConfig | None:
        doc = await self.db.trading_configs.find_one({"symbol": symbol, "side": side})
        return TradingConfig(**doc) if doc else None

    async def _upsert(self, config: TradingConfig) -> bool:
        query = {
            "strategy_id": config.strategy_id,
            "symbol": config.symbol,
            "side": config.side,
        }
        result = await self.db.trading_configs.replace_one(
            query,
            config.model_dump(),
            upsert=True,
        )
        return result.acknowledged

    async def set_global_config(self, config: TradingConfig) -> bool:
        return await self._upsert(config)

    async def set_symbol_config(self, config: TradingConfig) -> bool:
        return await self._upsert(config)

    async def set_symbol_side_config(self, config: TradingConfig) -> bool:
        return await self._upsert(config)

    async def delete_global_config(self) -> bool:
        result = await self.db.trading_configs.delete_one(
            {"symbol": None, "side": None}
        )
        return result.deleted_count > 0

    async def delete_symbol_config(self, symbol: str) -> bool:
        result = await self.db.trading_configs.delete_one(
            {"symbol": symbol, "side": None}
        )
        return result.deleted_count > 0

    async def delete_symbol_side_config(self, symbol: str, side: str) -> bool:
        result = await self.db.trading_configs.delete_one(
            {"symbol": symbol, "side": side}
        )
        return result.deleted_count > 0

    async def add_audit_record(self, audit: TradingConfigAudit) -> bool:
        result = await self.db.config_history.insert_one(audit.model_dump())
        return result.acknowledged

    async def get_config_history(
        self,
        symbol: str | None = None,
        side: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {}
        if symbol is not None:
            query["symbol"] = symbol
        if side is not None:
            query["side"] = side

        cursor = self.db.config_history.find(query).sort("timestamp", -1).limit(limit)
        return [doc async for doc in cursor]

    async def get_audit_record_by_version(
        self,
        *,
        version: int,
        config_type: str,
        symbol: str | None,
        side: str | None,
    ) -> dict[str, Any] | None:
        query: dict[str, Any] = {
            "config_type": config_type,
            "version_after": version,
            "symbol": symbol,
            "side": side,
        }
        return await self.db.config_history.find_one(query)

    async def get_audit_record_by_id(self, audit_id: str) -> dict[str, Any] | None:
        return await self.db.config_history.find_one({"id": audit_id})
