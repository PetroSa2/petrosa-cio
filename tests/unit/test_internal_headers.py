import logging

import httpx
import pytest

from cio.core import internal_headers as headers_module
from cio.core.characterization_stale_gate import is_characterization_stale
from cio.core.context_builder import ContextBuilder
from cio.core.durable_state import DataManagerStateStore
from cio.core.envelope_fetcher import EnvelopeFetcher


def test_internal_headers_includes_token_without_logging_value(monkeypatch, caplog):
    secret = "test-only-internal-token"
    dm_secret = "test-only-dm-token"
    monkeypatch.setenv("PETROSA_INTERNAL_TOKEN", secret)
    monkeypatch.setenv("DM_SERVICE_NAME", "test-cio")
    monkeypatch.setenv("DM_SERVICE_TOKEN", dm_secret)
    caplog.set_level(logging.WARNING)

    headers = headers_module.internal_headers(namespace="cio_auto_resume")

    assert headers == {
        "X-Petrosa-Issuer": "CIO",
        "X-Petrosa-Service": "test-cio",
        "X-Petrosa-Namespace": "cio_auto_resume",
        "X-Petrosa-Internal-Token": secret,
        "Authorization": f"Bearer {dm_secret}",
    }
    assert secret not in caplog.text
    assert dm_secret not in caplog.text


def test_internal_headers_omits_missing_token_and_warns_once(monkeypatch, caplog):
    monkeypatch.delenv("PETROSA_INTERNAL_TOKEN", raising=False)
    monkeypatch.delenv("DM_SERVICE_TOKEN", raising=False)
    headers_module._warned_missing_token = False
    headers_module._warned_missing_dm_token = False
    caplog.set_level(logging.WARNING)

    headers = headers_module.internal_headers()
    headers_module.internal_headers()

    assert headers == {
        "X-Petrosa-Issuer": "CIO",
        "X-Petrosa-Service": "petrosa-cio",
    }
    assert caplog.text.count("PETROSA_INTERNAL_TOKEN is not set") == 1
    assert caplog.text.count("DM_SERVICE_TOKEN is not set") == 1


@pytest.mark.asyncio
async def test_data_manager_clients_attach_internal_token(monkeypatch):
    token = "client-token"
    dm_token = "dm-client-token"
    monkeypatch.setenv("DM_SERVICE_NAME", "cio-test")
    monkeypatch.setenv("DM_SERVICE_TOKEN", dm_token)
    monkeypatch.setenv("PETROSA_INTERNAL_TOKEN", token)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"entries": []})

    store = DataManagerStateStore("http://dm", "cio_auto_resume")
    await store._client.aclose()
    store._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await store.all()
    await store.close()

    fetcher_requests: list[httpx.Request] = []
    fetcher = EnvelopeFetcher(
        "http://dm",
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: (
                    fetcher_requests.append(request)
                    or httpx.Response(200, json={"value": {}})
                )
            )
        ),
    )
    await fetcher.get_active("strategy:test")
    await fetcher.aclose()

    characterization_requests: list[httpx.Request] = []
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: (
                characterization_requests.append(request) or httpx.Response(200)
            )
        )
    )
    await is_characterization_stale(
        strategy_id="s1", strategy_revision_id="r1", client=client
    )
    await client.aclose()

    builder = ContextBuilder("http://dm", "http://tradeengine")
    assert builder.client.headers["X-Petrosa-Internal-Token"] == token
    assert builder.client.headers["X-Petrosa-Service"] == "cio-test"
    assert builder.client.headers["Authorization"] == f"Bearer {dm_token}"
    await builder.client.aclose()
    assert requests[0].headers["X-Petrosa-Internal-Token"] == token
    assert requests[0].headers["X-Petrosa-Service"] == "cio-test"
    assert requests[0].headers["Authorization"] == f"Bearer {dm_token}"
    assert fetcher_requests[0].headers["X-Petrosa-Service"] == "cio-test"
    assert fetcher_requests[0].headers["Authorization"] == f"Bearer {dm_token}"
    assert characterization_requests[0].headers["X-Petrosa-Service"] == "cio-test"
    assert characterization_requests[0].headers["Authorization"] == f"Bearer {dm_token}"
