"""Credentials never reach a log line (petrosa-cio#397).

A connection URL such as ``redis://:<password>@host:6379/0`` carries its credentials in the userinfo part.
:func:`redact_url` replaces that part with ``***``. :func:`install_log_redaction` wraps the process's
log-record factory so every record, from CIO or from a library, has the userinfo of any URL in its message
and string arguments redacted before any handler (stream, OTLP) sees it. It chains onto the factory already
installed (e.g. OpenTelemetry's logging instrumentation) and is idempotent.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

# scheme://userinfo@ : userinfo is anything up to the first "@" that contains no "/", whitespace or quote.
_URL_USERINFO = re.compile(r"(?P<scheme>\b[A-Za-z][A-Za-z0-9+.\-]*://)[^/@\s\"'<>]*@")
REDACTED = "***"


def redact_url(text: Any) -> Any:
    """``text`` with the userinfo of every URL in it replaced by ``***``; non-strings pass through."""
    if not isinstance(text, str) or "@" not in text:
        return text
    return _URL_USERINFO.sub(lambda m: f"{m.group('scheme')}{REDACTED}@", text)


def _redact_args(args: Any) -> Any:
    if isinstance(args, tuple):
        return tuple(redact_url(a) for a in args)
    if isinstance(args, dict):
        return {k: redact_url(v) for k, v in args.items()}
    return redact_url(args)


def _redact_record(record: logging.LogRecord) -> logging.LogRecord:
    record.msg = redact_url(record.msg)
    if record.args:
        record.args = _redact_args(record.args)
    return record


def install_log_redaction() -> None:
    """Wrap the current log-record factory so every new record is redacted (once per process)."""
    current: Callable[..., logging.LogRecord] = logging.getLogRecordFactory()
    if getattr(current, "_cio_redacts_credentials", False):
        return

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        return _redact_record(current(*args, **kwargs))

    factory._cio_redacts_credentials = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(factory)
