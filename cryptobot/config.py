"""Application paths and static defaults.

Nothing in here is secret. API keys live in the OS keychain (Milestone 4).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_data_dir, user_log_dir

APP_NAME = "CryptoBot"


@dataclass(frozen=True)
class AppPaths:
    data_dir: Path
    log_dir: Path

    @property
    def db_path(self) -> Path:
        return self.data_dir / "cryptobot.db"

    @property
    def engine_log(self) -> Path:
        return self.log_dir / "engine.log"

    @property
    def gui_log(self) -> Path:
        return self.log_dir / "gui.log"

    @classmethod
    def default(cls) -> "AppPaths":
        p = cls(Path(user_data_dir(APP_NAME, APP_NAME)), Path(user_log_dir(APP_NAME, APP_NAME)))
        p.data_dir.mkdir(parents=True, exist_ok=True)
        p.log_dir.mkdir(parents=True, exist_ok=True)
        return p


# Exchanges the app knows how to talk to. The id is the ccxt id.
# "available_us" is informational and drives the label shown in the GUI; the
# user confirms availability for their own country.
EXCHANGES: dict[str, dict] = {
    "kraken": {
        "name": "Kraken",
        "symbols": ["BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD"],
        "available_us": True,
    },
    "coinbase": {
        "name": "Coinbase Advanced",
        "symbols": ["BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD"],
        "available_us": True,
    },
    "binanceus": {
        "name": "Binance.US",
        "symbols": ["BTC/USDT", "ETH/USDT", "SOL/USDT"],
        "available_us": True,
    },
    "binance": {
        "name": "Binance.com (not available in the US)",
        "symbols": ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT"],
        "available_us": False,
    },
}

DEFAULT_EXCHANGE = "kraken"

# Bar timeframes the engine maintains live for every selected coin.
# 1s is built from the trade stream; the others are also backfilled from REST.
LIVE_TIMEFRAMES_S: tuple[int, ...] = (1, 60, 300)
HISTORY_BARS = 300

