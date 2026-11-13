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
    opens = MagicMock()
    monkeypatch.setattr(arbiter_module, "ARBITER_STATE_UNAVAILABLE", counter)
    monkeypatch.setattr(arbiter_module, "CIO_ARBITER_BREAKER_OPEN_TOTAL", opens)
    cache = _unreachable_cache()
    arbiter = SignalArbiter(cache)

    allowed, reason = await arbiter.check("BCHUSDT", "buy", 0.9, "strat_a", "cid1")

    assert allowed is False
    assert reason.startswith("ARBITER_STATE_UNAVAILABLE")
    assert "refused" in reason
    counter.add.assert_called_once_with(1, {"outcome": "refused"})
    opens.add.assert_called_once_with(1)
    cache.set.assert_not_awaited()  # no arbitration state is written for a refused signal


@pytest.mark.asyncio
async def test_breaker_suppresses_redis_reads_and_recovers_after_window(monkeypatch):
    now = [100.0]
    counter = MagicMock()
    opens = MagicMock()
    cache = MagicMock()
    cache.get_strict = AsyncMock(
        side_effect=[CacheUnavailable("connection lost"), None, None, None, None]
    )
    cache.set = AsyncMock()
    monkeypatch.setattr(arbiter_module, "ARBITER_STATE_UNAVAILABLE", counter)
    monkeypatch.setattr(arbiter_module, "CIO_ARBITER_BREAKER_OPEN_TOTAL", opens)
    arbiter = SignalArbiter(cache)
    arbiter._clock = lambda: now[0]
    arbiter._breaker_seconds = 5.0

    allowed, reason = await arbiter.check("BCHUSDT", "buy", 0.9, "s", "cid1")
    assert allowed is False
    assert "refused" in reason
    assert arbiter._breaker_open_until == 105.0
    assert cache.get_strict.await_count == 1

    for cid, closing in (("cid2", False), ("cid3", True)):
        allowed, reason = await arbiter.check(
            "BCHUSDT",
            "close_long" if closing else "buy",
            0.9,
            "s",
            cid,
            closing=closing,
        )
        assert allowed is closing
        assert "circuit breaker open" in reason
        assert arbiter._breaker_open_until == 105.0
        assert cache.get_strict.await_count == 1

    now[0] = 104.999
    allowed, reason = await arbiter.check("BCHUSDT", "sell", 0.9, "s", "cid4")
    assert allowed is False
    assert "circuit breaker open" in reason
    assert cache.get_strict.await_count == 1

    now[0] = 105.0
    allowed, reason = await arbiter.check("BCHUSDT", "sell", 0.9, "s", "cid5")
    assert (allowed, reason) == (True, "allowed")
    assert arbiter._breaker_open_until is None
    assert cache.get_strict.await_count == 3
    assert opens.add.call_count == 1

    # After a successful half-open read, ordinary arbitration still reads Redis per signal.
    allowed, reason = await arbiter.check("ETHUSDT", "buy", 0.9, "s", "cid6")
    assert (allowed, reason) == (True, "allowed")
    assert arbiter._breaker_open_until is None
    assert cache.get_strict.await_count == 5
    opens.add.assert_called_once_with(1)


@pytest.mark.asyncio
async def test_breaker_reopens_when_half_open_probe_fails(monkeypatch):
    now = [10.0]
    opens = MagicMock()
    cache = MagicMock()
    cache.get_strict = AsyncMock(
        side_effect=[CacheUnavailable("down"), CacheUnavailable("still down")]
    )
    cache.set = AsyncMock()
    monkeypatch.setattr(arbiter_module, "ARBITER_STATE_UNAVAILABLE", MagicMock())
    monkeypatch.setattr(arbiter_module, "CIO_ARBITER_BREAKER_OPEN_TOTAL", opens)
    arbiter = SignalArbiter(cache)
    arbiter._clock = lambda: now[0]
    arbiter._breaker_seconds = 5.0

    assert (await arbiter.check("BTCUSDT", "buy", 0.9, "s", "cid1"))[0] is False
    assert arbiter._breaker_open_until == 15.0
    now[0] = 15.0
    allowed, reason = await arbiter.check("BTCUSDT", "buy", 0.9, "s", "cid2")
    assert allowed is False
    assert "still down" in reason
    assert arbiter._breaker_open_until == 20.0
    assert cache.get_strict.await_count == 2
    opens.add.assert_called_with(1)
    assert opens.add.call_count == 2


@pytest.mark.asyncio
async def test_breaker_allows_only_one_half_open_redis_probe(monkeypatch):
    now = [100.0]
    opened = asyncio.Event()
    release = asyncio.Event()
    reads = [0]
    cache = MagicMock()

    async def read(key: str):
        reads[0] += 1
        if reads[0] == 1:
            raise CacheUnavailable("connection lost")
        if reads[0] == 2:
            opened.set()
            await release.wait()
            return None
        return None

    cache.get_strict = AsyncMock(side_effect=read)
    cache.set = AsyncMock()
    monkeypatch.setattr(arbiter_module, "ARBITER_STATE_UNAVAILABLE", MagicMock())
    monkeypatch.setattr(arbiter_module, "CIO_ARBITER_BREAKER_OPEN_TOTAL", MagicMock())
    arbiter = SignalArbiter(cache)
    arbiter._clock = lambda: now[0]
    arbiter._breaker_seconds = 5.0

    assert (await arbiter.check("BCHUSDT", "buy", 0.9, "s", "cid1"))[0] is False
    assert arbiter._breaker_open_until == 105.0
    now[0] = 105.0
    first = asyncio.create_task(arbiter.check("BCHUSDT", "buy", 0.9, "s", "cid2"))
    await opened.wait()
    assert arbiter._breaker_open_until is None
    second = asyncio.create_task(arbiter.check("BCHUSDT", "sell", 0.9, "s", "cid3"))
    await asyncio.sleep(0)
    assert cache.get_strict.await_count == 2
    release.set()
    assert (await first)[0] is True
    assert (await second)[0] is True
    assert arbiter._breaker_open_until is None
    assert cache.get_strict.await_count == 5


@pytest.mark.asyncio
async def test_healthy_redis_never_opens_breaker(monkeypatch):
    opens = MagicMock()
    monkeypatch.setattr(arbiter_module, "CIO_ARBITER_BREAKER_OPEN_TOTAL", opens)
    cache = MagicMock()
    cache.get_strict = AsyncMock(return_value=None)
    cache.set = AsyncMock()
    arbiter = SignalArbiter(cache)

    assert await arbiter.check("BCHUSDT", "buy", 0.9, "s", "cid1") == (
        True,
        "allowed",
    )
    opens.add.assert_not_called()


@pytest.mark.parametrize(
    ("env_value", "expected"),
    [
        (None, 5.0),
        ("2.5", 2.5),
        ("0", 0.0),
        ("invalid", 5.0),
        ("-1", 5.0),
        ("nan", 5.0),
        ("inf", 5.0),
        ("-inf", 5.0),
    ],
)
def test_breaker_window_has_labelled_env_fallback(monkeypatch, env_value, expected):
    if env_value is None:
        monkeypatch.delenv("CIO_ARBITER_BREAKER_SECONDS", raising=False)
    else:
        monkeypatch.setenv("CIO_ARBITER_BREAKER_SECONDS", env_value)
    assert arbiter_module._breaker_seconds_from_env() == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_write", [1, 2])
@pytest.mark.parametrize("closing", [False, True])
async def test_unavailable_redis_write_reopens_breaker_and_preserves_intent_safety(
    monkeypatch, failed_write, closing
):
    now = [20.0]
    unavailable = MagicMock()
    opens = MagicMock()
    cache = MagicMock()
    cache.get_strict = AsyncMock(return_value=None)
    writes = [0]

    async def write(key: str, value: str, ttl: int = 900):
        writes[0] += 1
        if writes[0] == failed_write:
            raise CacheUnavailable("write connection lost")

    cache.set = AsyncMock(side_effect=write)
    monkeypatch.setattr(arbiter_module, "ARBITER_STATE_UNAVAILABLE", unavailable)
    monkeypatch.setattr(arbiter_module, "CIO_ARBITER_BREAKER_OPEN_TOTAL", opens)
    arbiter = SignalArbiter(cache)
    arbiter._clock = lambda: now[0]
    arbiter._breaker_seconds = 5.0
    action = "close_long" if closing else "buy"

    allowed, reason = await arbiter.check(
        "BTCUSDT", action, 0.9, "s", "write-failure", closing=closing
    )

    assert allowed is closing
    assert "Redis unreachable (write connection lost)" in reason
    outcome = "passed_closing" if closing else "refused"
    unavailable.add.assert_called_once_with(1, {"outcome": outcome})
    opens.add.assert_called_once_with(1)
    assert arbiter._breaker_open_until == 25.0
    assert writes[0] == failed_write

    now[0] = 24.999
    allowed, reason = await arbiter.check(
        "BTCUSDT", action, 0.9, "s", "breaker-open", closing=closing
    )
    assert allowed is closing
    assert "circuit breaker open" in reason
    assert cache.get_strict.await_count == 2
    assert writes[0] == failed_write
    unavailable.add.assert_called_with(1, {"outcome": outcome})
    assert unavailable.add.call_count == 2

    now[0] = 25.0
    allowed, reason = await arbiter.check("ETHUSDT", "sell", 0.9, "s", "recovered")
    assert (allowed, reason) == (True, "allowed")
    assert arbiter._breaker_open_until is None
    assert cache.get_strict.await_count == 4
    assert writes[0] == failed_write + 2
    assert opens.add.call_count == 1


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
