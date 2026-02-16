"""
Optional Telegram notification support.

If ``TELEGRAM_BOT_TOKEN`` and ``TELEGRAM_CHAT_ID`` are configured in
the environment, messages are sent via the Bot API.  Otherwise, calls
silently no-op so the rest of the bot never needs to care whether
Telegram is enabled.
"""

from __future__ import annotations

import os
from typing import Optional

import aiohttp

from utils.logger import get_logger

log = get_logger(__name__)

_BOT_TOKEN: Optional[str] = os.getenv("TELEGRAM_BOT_TOKEN")
_CHAT_ID: Optional[str] = os.getenv("TELEGRAM_CHAT_ID")
_ENABLED: bool = bool(_BOT_TOKEN and _CHAT_ID)


async def send_telegram(message: str) -> None:
    """Send a message via the Telegram Bot API.

    Fails silently (logs a warning) if the request errors out, so
    Telegram downtime never crashes the bot.
    """
    if not _ENABLED:
        return

    url = f"https://api.telegram.org/bot{_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": _CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    log.warning("Telegram API returned %d: %s", resp.status, body[:200])
                else:
                    log.debug("Telegram message sent successfully")
    except Exception as exc:
        log.warning("Telegram send failed (non-fatal): %s", exc)


def is_enabled() -> bool:
    """Check whether Telegram alerts are configured."""
    return _ENABLED
