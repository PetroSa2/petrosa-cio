"""Ported config manager tests for CIO TradingConfigManager migration."""

import time
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apps.nurse.enforcer import TradingConfigManager
from contracts.trading_config import TradingConfig


@pytest.fixture
def mock_mongodb_client():
    client = MagicMock()
    client.connected = True
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.get_global_config = AsyncMock(return_value=None)
    client.get_symbol_config = AsyncMock(return_value=None)
    client.get_strategy_config = AsyncMock(return_value=None)
    client.get_symbol_side_config = AsyncMock(return_value=None)
    client.set_global_config = AsyncMock(return_value=True)
    client.set_symbol_config = AsyncMock(return_value=True)
    client.set_symbol_side_config = AsyncMock(return_value=True)
    client.delete_global_config = AsyncMock(return_value=True)
    client.delete_symbol_config = AsyncMock(return_value=True)
    client.delete_symbol_side_config = AsyncMock(return_value=True)
    client.add_audit_record = AsyncMock(return_value=True)
    client.get_config_history = AsyncMock(return_value=[])
    client.get_audit_record_by_version = AsyncMock(return_value=None)
    client.get_audit_record_by_id = AsyncMock(return_value=None)
    return client


@pytest.fixture
def config_manager(mock_mongodb_client):
    return TradingConfigManager(
        mongodb_client=mock_mongodb_client, cache_ttl_seconds=60
    )


@pytest.mark.asyncio
async def test_get_cache_key_and_cache_hit(config_manager):
    cache_key = config_manager._get_cache_key("BTCUSDT", "LONG", "momentum")
    assert cache_key == "BTCUSDT:LONG:momentum"

    config_manager._cache[cache_key] = ({"leverage": 77}, time.time())
    resolved = await config_manager.get_config(
        symbol="BTCUSDT", side="LONG", strategy_id="momentum"
    )
    assert resolved["leverage"] == 77


@pytest.mark.asyncio
async def test_get_config_layer_hierarchy(config_manager, mock_mongodb_client):
    mock_mongodb_client.get_global_config = AsyncMock(
        return_value=TradingConfig(
            parameters={"leverage": 10, "stop_loss_pct": 2.0},
            created_by="test",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
    )
    mock_mongodb_client.get_symbol_config = AsyncMock(
        return_value=TradingConfig(
            symbol="BTCUSDT",
            parameters={"leverage": 20},
            created_by="test",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
    )
    mock_mongodb_client.get_symbol_side_config = AsyncMock(
        return_value=TradingConfig(
            symbol="BTCUSDT",
            side="LONG",
            parameters={"stop_loss_pct": 1.5},
            created_by="test",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
    )

    cfg = await config_manager.get_config(symbol="BTCUSDT", side="LONG")
    assert cfg["leverage"] == 20
    assert cfg["stop_loss_pct"] == 1.5


@pytest.mark.asyncio
async def test_set_config_validate_only(config_manager, mock_mongodb_client):
    success, config, errors = await config_manager.set_config(
        parameters={"leverage": 15},
        changed_by="test",
        validate_only=True,
    )
    assert success is True
    assert config is None
    assert errors == []
    mock_mongodb_client.set_global_config.assert_not_called()


@pytest.mark.asyncio
async def test_set_config_rejects_invalid_parameters(config_manager):
    success, config, errors = await config_manager.set_config(
        parameters={"leverage": 999},
        changed_by="test",
    )
    assert success is False
    assert config is None
    assert errors


@pytest.mark.asyncio
async def test_set_config_injects_potential_pnl_metadata(config_manager):
    success, config, errors = await config_manager.set_config(
        parameters={
            "leverage": 15,
            "potential_pnl": 123.45,
            "blocked_reason": "semantic_veto",
        },
        changed_by="test",
    )

    assert success is True
    assert errors == []
    assert config is not None
    assert config.metadata["potential_pnl"] == pytest.approx(123.45)
    assert config.metadata["blocked_reason"] == "semantic_veto"


@pytest.mark.asyncio
async def test_rollback_by_version_and_id_paths(config_manager, mock_mongodb_client):
    mock_mongodb_client.get_audit_record_by_version = AsyncMock(
        return_value={"parameters_after": {"leverage": 22}}
    )
    with patch.object(
        config_manager,
        "set_config",
        new_callable=AsyncMock,
    ) as mock_set:
        mock_set.return_value = (True, TradingConfig(parameters={}, created_by="t"), [])
        success, _, _ = await config_manager.rollback_config(
            changed_by="admin", target_version=2
        )
        assert success is True
        assert mock_set.call_args[1]["parameters"]["leverage"] == 22

    mock_mongodb_client.get_audit_record_by_id = AsyncMock(
        return_value={
            "symbol": "BTCUSDT",
            "side": None,
            "parameters_after": {"leverage": 33},
        }
    )
    with patch.object(
        config_manager,
        "set_config",
        new_callable=AsyncMock,
    ) as mock_set:
        mock_set.return_value = (True, TradingConfig(parameters={}, created_by="t"), [])
        success, _, _ = await config_manager.rollback_config(
            changed_by="admin", symbol="BTCUSDT", rollback_id="audit-1"
        )
        assert success is True
        assert mock_set.call_args[1]["parameters"]["leverage"] == 33


@pytest.mark.asyncio
async def test_delete_config_and_cache_invalidation(config_manager):
    key = config_manager._get_cache_key("BTCUSDT", "LONG")
    config_manager._cache[key] = ({"leverage": 10}, time.time())

    success, errors = await config_manager.delete_config(
        changed_by="admin", symbol="BTCUSDT", side="LONG"
    )

    assert success is True
    assert errors == []
    assert key not in config_manager._cache


@pytest.mark.asyncio
async def test_previous_config_resolution(config_manager, mock_mongodb_client):
    mock_mongodb_client.get_config_history = AsyncMock(
        return_value=[
            {
                "action": "update",
                "parameters_before": {"leverage": 9},
                "parameters_after": {"leverage": 10},
            },
            {
                "action": "update",
                "parameters_after": {"leverage": 8},
            },
        ]
    )

    prev = await config_manager.get_previous_config(symbol="BTCUSDT")
    assert prev == {"leverage": 9}
