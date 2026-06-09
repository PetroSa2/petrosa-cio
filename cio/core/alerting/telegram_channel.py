"""Telegram notification channel for operator alerts (petrosa_k8s#810)."""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramChannel:
    """Best-effort Telegram notifier using the Bot HTTP API.

    Reads ``TELEGRAM_BOT_TOKEN`` and ``TELEGRAM_CHAT_ID`` from the
    environment. When either is absent the channel degrades gracefully:
    :meth:`send` returns ``False`` and logs a single INFO line so the
    caller can distinguish "not configured" from a real failure.
    """

    def __init__(
        self,
        bot_token: str | None = None,
        chat_id: str | None = None,
    ) -> None:
        self._bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self._chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "")

    @property
    def is_configured(self) -> bool:
        return bool(self._bot_token and self._chat_id)

    async def send(self, text: str, extra: dict[str, Any] | None = None) -> bool:
        """Send *text* to the configured chat. Returns True on HTTP 200.

        Never raises — all exceptions are caught, logged, and returned as
        ``False`` so the caller's subscribe loop stays alive.
        """
        if not self.is_configured:
            logger.info(
                "telegram_channel.skipped: "
                "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set"
            )
            return False

        url = _TELEGRAM_API.format(token=self._bot_token)
        payload: dict[str, Any] = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=payload)
            if resp.status_code == 200:
                return True
            logger.warning(
                "telegram_send_failed status=%s body=%s",
                resp.status_code,
                resp.text[:200],
            )
            return False
        except Exception as exc:  # noqa: BLE001
            logger.warning("telegram_send_error exc=%s", exc)
            return False
