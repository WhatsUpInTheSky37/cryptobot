"""Outbound alerts: Discord webhook and Telegram bot. Desktop pop-ups live in the GUI tray."""
from __future__ import annotations

import json
import logging
import threading
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)


def _post(url: str, data: bytes, headers: dict[str, str]) -> None:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 - user-configured webhook
        r.read()


def send_discord(webhook_url: str, text: str) -> None:
    _post(webhook_url, json.dumps({"content": text[:1900]}).encode(), {"Content-Type": "application/json", "User-Agent": "CryptoBot"})


def send_telegram(bot_token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text[:4000]}).encode()
    _post(url, data, {"Content-Type": "application/x-www-form-urlencoded"})


def send_async(fn, *args) -> threading.Thread:
    def run() -> None:
        try:
            fn(*args)
        except Exception as e:  # noqa: BLE001
            log.warning("notification failed: %s: %s", type(e).__name__, e)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def format_fill(mode: str, symbol: str, side: str, amount: float, price: float, reason: str, pnl: float | None) -> str:
    s = f"[{mode}] {side.upper()} {symbol} {amount:.6f} @ {price:,.2f} ({reason})"
    if pnl is not None:
        s += f" P&L {pnl:+.2f}"
    return s
