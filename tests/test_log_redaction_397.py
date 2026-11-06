"""No logged URL carries credentials (petrosa-cio#397)."""

from __future__ import annotations

import logging

import pytest

from cio.core import log_redaction as lr

DUMMY_PW = "dummy-pw"  # not a credential


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            f"redis://:{DUMMY_PW}@redis-service.petrosa-system.svc.cluster.local:6379/0",
            "redis://***@redis-service.petrosa-system.svc.cluster.local:6379/0",
        ),
        (f"nats://user:{DUMMY_PW}@nats:4222", "nats://***@nats:4222"),
        (
            f"mysql+pymysql://app:{DUMMY_PW}@db:3306/x?a=b",
            "mysql+pymysql://***@db:3306/x?a=b",
        ),
        (
            f"two: redis://:{DUMMY_PW}@a:1 and https://u:{DUMMY_PW}@b/p",
            "two: redis://***@a:1 and https://***@b/p",
        ),
    ],
)
def test_redact_url_removes_userinfo(raw: str, expected: str) -> None:
    assert lr.redact_url(raw) == expected


@pytest.mark.parametrize(
    "unchanged",
    [
        "nats://nats-server.nats.svc.cluster.local:4222",
        "http://petrosa-data-manager:80/api/v1/x?email=a@b.c",
        "contact ops@example.com",
        "no url at all",
    ],
)
def test_redact_url_leaves_urls_without_userinfo_alone(unchanged: str) -> None:
    assert lr.redact_url(unchanged) == unchanged


def test_redact_url_passes_non_strings_through() -> None:
    obj = object()
    assert lr.redact_url(obj) is obj
    assert lr.redact_url(None) is None


@pytest.fixture
def redacting(caplog):
    original = logging.getLogRecordFactory()
    lr.install_log_redaction()
    caplog.set_level(logging.INFO)
    try:
        yield caplog
    finally:
        logging.setLogRecordFactory(original)


def test_every_logged_form_is_redacted(redacting) -> None:
    log = logging.getLogger("cio.test.redaction")
    url = f"redis://:{DUMMY_PW}@redis:6379/0"
    log.info(f"Connected to Redis at {url}")  # f-string
    log.info("Connected to Redis at %s", url)  # %-args
    log.info("Connected to %(target)s", {"target": url})  # mapping args
    log.warning("Failed to connect to Redis at %s", url, exc_info=False)
    text = redacting.text
    assert DUMMY_PW not in text
    assert text.count("redis://***@redis:6379/0") == 4


def test_install_is_idempotent_and_chains_the_existing_factory(caplog) -> None:
    original = logging.getLogRecordFactory()

    def tagging(*args, **kwargs):
        record = original(*args, **kwargs)
        record.tagged = True
        return record

    logging.setLogRecordFactory(tagging)
    try:
        lr.install_log_redaction()
        once = logging.getLogRecordFactory()
        lr.install_log_redaction()
        assert logging.getLogRecordFactory() is once  # not wrapped twice
        caplog.set_level(logging.INFO)
        logging.getLogger("cio.test.chain").info("dsn %s", f"redis://:{DUMMY_PW}@r:1")
        record = caplog.records[-1]
        assert record.tagged is True  # the earlier factory still runs
        assert DUMMY_PW not in record.getMessage()
    finally:
        logging.setLogRecordFactory(original)


def test_cio_main_installs_the_guard_and_redacts_its_connection_logs() -> None:
    source = open("cio/main.py", encoding="utf-8").read()
    assert "install_log_redaction()" in source
    assert 'logger.info(f"Connected to Redis at {redis_url}")' not in source
    assert 'logger.info(f"Connected to NATS at {nats_url}")' not in source
    assert "redact_url(redis_url)" in source and "redact_url(nats_url)" in source
