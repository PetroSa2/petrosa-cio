"""petrosa-cio#406: a Redis error must never read as "no prior signal" in the arbiter.

- A stale pooled connection after a redis restart is replaced by the client's bounded retry: normal path.
- Redis still unreachable: new entries are refused (ARBITER_STATE_UNAVAILABLE, counted), closes still pass.
- An absent key is still a normal miss.
"""

import asyncio
import json
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
import redis.asyncio as redis_asyncio
from nats.aio.msg import Msg
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff

from cio.core import arbiter as arbiter_module
from cio.core.arbiter import SignalArbiter
from cio.core.cache import AsyncRedisCache, CacheUnavailable
from cio.core.listener import NATSListener
from cio.main import _redis_client_kwargs


async def _read_command(reader: asyncio.StreamReader) -> list[bytes] | None:
    line = await reader.readline()
    if not line:
        return None
    count = int(line[1:].strip())
    parts = []
    for _ in range(count):
        size = int((await reader.readline())[1:].strip())
        parts.append((await reader.readexactly(size + 2))[:-2])
    return parts


class _FakeRedis:
    """A minimal RESP server: HELLO, PING, GET, SET, anything else +OK. ``restart()`` kills every open connection."""

    def __init__(self) -> None:
        self.store: dict[bytes, bytes] = {}
        self.writers: set[asyncio.StreamWriter] = set()
        self.connections = 0
        self.server: asyncio.base_events.Server | None = None

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        self.server.close()
        for writer in list(self.writers):
            writer.transport.abort()
        await self.server.wait_closed()

    async def restart(self) -> None:
        for writer in list(self.writers):
            writer.transport.abort()
        await asyncio.sleep(0.05)

    async def _handle(self, reader, writer) -> None:
        self.connections += 1
        self.writers.add(writer)
        try:
            while (command := await _read_command(reader)) is not None:
                name = command[0].upper()
                if name == b"HELLO":  # redis-py 8 negotiates RESP3 on connect
                    response = b"%1\r\n$5\r\nproto\r\n:3\r\n"
                elif name == b"PING":
                    response = b"+PONG\r\n"
                elif name == b"GET":
                    value = self.store.get(command[1])
                    response = (
                        b"_\r\n"  # RESP3 null
                        if value is None
                        else b"$%d\r\n%s\r\n" % (len(value), value)
                    )
                elif name == b"SET":
                    self.store[command[1]] = command[2]
                    response = b"+OK\r\n"
                else:
                    response = b"+OK\r\n"
                writer.write(response)
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self.writers.discard(writer)
            writer.close()


@pytest_asyncio.fixture
async def fake_redis():
    server = _FakeRedis()
    port = await server.start()
    yield server, f"redis://127.0.0.1:{port}"
    await server.stop()


@pytest.mark.asyncio
async def test_stale_connection_after_restart_is_retried_and_dedup_holds(
    fake_redis, caplog
):
    server, url = fake_redis
    client = redis_asyncio.from_url(url, **_redis_client_kwargs())
    cache = AsyncRedisCache(client)
    arbiter = SignalArbiter(cache)

    allowed, _ = await arbiter.check("BCHUSDT", "buy", 0.8, "strat_a", "cid1")
    assert allowed is True
    assert server.connections == 1

    await (
        server.restart()
    )  # the pooled connection is now dead, as after the 2026-11-07 redis roll

    with caplog.at_level(logging.WARNING):
        allowed, reason = await arbiter.check("BCHUSDT", "buy", 0.9, "strat_b", "cid2")

    assert allowed is False
    assert reason.startswith("SIGNAL_DEDUPLICATED")
    assert server.connections == 2  # one fresh connection replaced the stale one
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    await client.aclose()


@pytest.mark.asyncio
async def test_without_the_retry_the_stale_connection_is_an_error(fake_redis):
    """Control: the same restart with redis-py's from_url default (no retry) fails the read, the #406 symptom."""
    server, url = fake_redis
    kwargs = _redis_client_kwargs()
    kwargs.update(retry=Retry(NoBackoff(), 0), retry_on_error=[])
    client = redis_asyncio.from_url(url, **kwargs)
    cache = AsyncRedisCache(client)
    await cache.set("arbiter:dedup:BCHUSDT:buy", "1", ttl=60)

    await server.restart()

    with pytest.raises(CacheUnavailable) as err:
        await cache.get_strict("arbiter:dedup:BCHUSDT:buy")
    assert "Connection" in str(err.value)
    await client.aclose()


def _unreachable_cache() -> MagicMock:
    cache = MagicMock()
    cache.get_strict = AsyncMock(
        side_effect=CacheUnavailable("ConnectionError: Connection refused")
    )
    cache.get = AsyncMock(return_value=None)
    cache.set = AsyncMock()
    return cache


@pytest.mark.asyncio
async def test_unreachable_redis_refuses_a_new_entry_and_counts_it(monkeypatch):
    counter = MagicMock()
    monkeypatch.setattr(arbiter_module, "ARBITER_STATE_UNAVAILABLE", counter)
    cache = _unreachable_cache()
    arbiter = SignalArbiter(cache)

    allowed, reason = await arbiter.check("BCHUSDT", "buy", 0.9, "strat_a", "cid1")

    assert allowed is False
    assert reason.startswith("ARBITER_STATE_UNAVAILABLE")
    assert "refused" in reason
    counter.add.assert_called_once_with(1, {"outcome": "refused"})
    cache.set.assert_not_awaited()  # no arbitration state is written for a refused signal


@pytest.mark.asyncio
async def test_unreachable_redis_lets_a_closing_intent_through(monkeypatch):
    counter = MagicMock()
    monkeypatch.setattr(arbiter_module, "ARBITER_STATE_UNAVAILABLE", counter)
    arbiter = SignalArbiter(_unreachable_cache())

    allowed, reason = await arbiter.check(
        "BCHUSDT", "close_long", 0.9, "strat_a", "cid1", closing=True
    )

    assert allowed is True
    assert reason.startswith("ARBITER_STATE_UNAVAILABLE")
    counter.add.assert_called_once_with(1, {"outcome": "passed_closing"})


@pytest.mark.asyncio
async def test_bias_read_failure_also_fails_closed(monkeypatch):
    """The dedup read succeeds (miss), the conflict read fails: still refused, never a silent pass."""
    monkeypatch.setattr(arbiter_module, "ARBITER_STATE_UNAVAILABLE", MagicMock())
    cache = MagicMock()
    cache.get_strict = AsyncMock(
        side_effect=[None, CacheUnavailable("ConnectionError: lost")]
    )
    cache.set = AsyncMock()
    arbiter = SignalArbiter(cache)

    allowed, reason = await arbiter.check("BCHUSDT", "sell", 0.9, "strat_a", "cid1")

    assert allowed is False
    assert reason.startswith("ARBITER_STATE_UNAVAILABLE")
    cache.set.assert_not_awaited()


@pytest.mark.asyncio
async def test_absent_keys_are_still_a_normal_miss():
    redis = AsyncMock()
    redis.get.return_value = None
    arbiter = SignalArbiter(AsyncRedisCache(redis))

    allowed, reason = await arbiter.check("BCHUSDT", "buy", 0.9, "strat_a", "cid1")

    assert (allowed, reason) == (True, "allowed")
    assert redis.set.await_count == 2  # bias + dedup recorded


@pytest.mark.asyncio
async def test_get_strict_raises_on_error_and_decodes_values():
    redis = AsyncMock()
    redis.get.side_effect = [b"1", None, RuntimeError("down")]
    cache = AsyncRedisCache(redis)

    assert await cache.get_strict("k") == "1"
    assert await cache.get_strict("k") is None
    with pytest.raises(CacheUnavailable) as err:
        await cache.get_strict("k")
    assert "RuntimeError: down" in str(err.value)


def _listener(arbiter: SignalArbiter) -> tuple[NATSListener, MagicMock]:
    enforcer = MagicMock()
    enforcer.audit = AsyncMock()
    context_builder = MagicMock()
    context_builder.build = AsyncMock(side_effect=RuntimeError("stop after arbiter"))
    listener = NATSListener(
        nats_client=MagicMock(),
        enforcer=enforcer,
        context_builder=context_builder,
        router=MagicMock(),
        arbiter=arbiter,
    )
    return listener, context_builder


def _msg(payload: dict) -> MagicMock:
    msg = MagicMock(spec=Msg)
    msg.headers = None
    msg.subject = "cio.intent.trading.strat_a"
    msg.data = json.dumps(payload).encode()
    return msg


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "reaches_context"),
    [
        ({"symbol": "BCHUSDT", "action": "buy", "strategy_id": "s"}, False),
        ({"symbol": "BCHUSDT", "action": "close_long", "strategy_id": "s"}, True),
        (
            {
                "symbol": "BCHUSDT",
                "side": "sell",
                "reduce_only": True,
                "strategy_id": "s",
            },
            True,
        ),
    ],
)
async def test_listener_passes_closing_intents_when_redis_is_unreachable(
    monkeypatch, payload, reaches_context
):
    monkeypatch.setattr(arbiter_module, "ARBITER_STATE_UNAVAILABLE", MagicMock())
    listener, context_builder = _listener(SignalArbiter(_unreachable_cache()))

    await listener._handle_message(_msg(payload))

    assert context_builder.build.await_count == (1 if reaches_context else 0)
