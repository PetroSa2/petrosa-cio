import os
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture(autouse=True)
def setup_test_env():
    """Ensure test environment variables are set."""
    os.environ["LLM_PROVIDER"] = "mock"
    os.environ["VECTOR_PROVIDER"] = "mock"
    os.environ["NATS_URL"] = "nats://localhost:4222"
    os.environ["REDIS_URL"] = "redis://localhost:6379"
    os.environ["DATA_MANAGER_URL"] = "http://data-manager"
    os.environ["TRADEENGINE_URL"] = "http://tradeengine"
    os.environ["STRATEGY_API_URL"] = "http://strategy-api"
    yield


@pytest.fixture
def mock_nats_client():
    """Mock for nats.aio.client.Client."""
    mock = AsyncMock()
    mock.publish = AsyncMock()
    mock.subscribe = AsyncMock()
    return mock


@pytest.fixture
def mock_redis_cache():
    """Mock for AsyncRedisCache."""
    mock = MagicMock()
    mock.get = AsyncMock(return_value=None)
    mock.set = AsyncMock()
    return mock


@pytest.fixture
def mock_httpx_client():
    """Mock for httpx.AsyncClient."""
    mock = AsyncMock()
    mock.get = AsyncMock()
    mock.post = AsyncMock()
    return mock
