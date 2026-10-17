import sys

import ccxt
import keyring
import pytest
from keyring.backend import KeyringBackend

from cryptobot import autostart, secrets
from cryptobot.data.account import ConnectionReport, check_connection
from cryptobot.data.markets import MarketRow, is_suggested, rank_tickers
from cryptobot.notify import format_fill


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        self.store = {}

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        if (service, username) not in self.store:
            raise keyring.errors.PasswordDeleteError()
        del self.store[(service, username)]


@pytest.fixture
def mem_keyring(monkeypatch):
    kr = MemoryKeyring()
    monkeypatch.setattr(keyring, "get_password", kr.get_password)
    monkeypatch.setattr(keyring, "set_password", kr.set_password)
    monkeypatch.setattr(keyring, "delete_password", kr.delete_password)
    return kr


def test_keychain_roundtrip_and_delete(mem_keyring):
    assert not secrets.has_api_key("binanceus")
    secrets.save_api_key("binanceus", "KEY123456789", "SECRETxyz")
    assert secrets.load_api_key("binanceus") == ("KEY123456789", "SECRETxyz", None)
    assert secrets.has_api_key("binanceus")
    secrets.delete_api_key("binanceus")
    assert secrets.load_api_key("binanceus") is None
    assert secrets.mask("KEY123456789") == "KEY1••••••••89"
    # keys never touch the settings database or disk: only the keyring backend saw them
    assert all(k[0] == "CryptoBot" for k in mem_keyring.store)


class FakeEx:
    def __init__(self, balance=None, restrictions=None, fail=None):
        self._bal = balance or {"total": {"USDT": 123.45, "BTC": 0.01}}
        self._r = restrictions
        self._fail = fail

    def fetch_balance(self):
        if self._fail:
            raise self._fail
        return self._bal

    def sapiGetAccountApiRestrictions(self):  # noqa: N802
        if self._r is None:
            raise ccxt.ExchangeError("not supported")
        return self._r


def test_connection_refuses_withdrawal_enabled_key():
    rep = check_connection("binanceus", "k", "s", exchange=FakeEx(restrictions={"enableSpotAndMarginTrading": True, "enableWithdrawals": True, "ipRestrict": False}))
    assert rep.ok and rep.can_withdraw is True and not rep.safe_to_save
    assert "WITHDRAW" in rep.message


def test_connection_good_key_reports_permissions_and_balances():
    rep = check_connection("binanceus", "k", "s", exchange=FakeEx(restrictions={"enableSpotAndMarginTrading": True, "enableWithdrawals": False, "ipRestrict": True}))
    assert rep.ok and rep.safe_to_save and rep.can_trade and rep.can_withdraw is False and rep.ip_restricted
    assert rep.balances == {"USDT": 123.45}
    assert "Withdraw ✔ no (good)" in rep.summary()


def test_connection_unknown_permissions_and_bad_key():
    rep = check_connection("kraken", "k", "s", exchange=FakeEx(restrictions=None))
    assert rep.ok and rep.can_withdraw is None and "Confirm yourself" in rep.message
    bad = check_connection("kraken", "k", "s", exchange=FakeEx(fail=ccxt.AuthenticationError("Invalid key")))
    assert not bad.ok and "rejected" in bad.message


def test_rank_tickers_filters_and_suggests():
    tickers = {
        "BTC/USDT": {"last": 80000, "bid": 79990, "ask": 80010, "quoteVolume": 50_000_000, "percentage": 1.2, "high": 81000, "low": 79000},
        "BTC/USD": {"last": 80000, "bid": 79900, "ask": 80100, "quoteVolume": 2_000_000, "percentage": 1.2, "high": 81000, "low": 79000},
        "DOGE/USDT": {"last": 0.1, "bid": 0.099, "ask": 0.101, "baseVolume": 1_000_000, "percentage": -3, "high": 0.11, "low": 0.09},
        "USDC/USDT": {"last": 1.0, "bid": 1.0, "ask": 1.0, "quoteVolume": 90_000_000},
        "ETH/EUR": {"last": 3000, "bid": 2999, "ask": 3001, "quoteVolume": 5_000_000},
        "BTC/USDT:USDT": {"last": 80000, "quoteVolume": 999_999_999},
    }
    rows = rank_tickers(tickers, ("USDT", "USD"))
    assert [r.symbol for r in rows] == ["BTC/USDT", "DOGE/USDT"]  # dedup by base, stables and futures and EUR dropped
    btc, doge = rows
    assert btc.suggested and btc.spread_pct == pytest.approx(0.025)
    assert not doge.suggested  # $100k volume, 2% spread
    assert doge.volume_quote_24h == pytest.approx(100_000)
    assert not is_suggested(MarketRow("X/USD", 1, 10_000_000, 0, 0.5, 0))


def test_autostart_writes_and_removes_launch_agent(tmp_path, monkeypatch):
    if sys.platform != "darwin":
        pytest.skip("macOS only")
    assert not autostart.is_enabled(tmp_path)
    msg = autostart.set_enabled(True, tmp_path)
    p = tmp_path / "Library" / "LaunchAgents" / f"{autostart.LABEL}.plist"
    assert p.exists() and str(p) in msg and autostart.is_enabled(tmp_path)
    import plistlib

    data = plistlib.loads(p.read_bytes())
    assert data["RunAtLoad"] and data["ProgramArguments"][-1] == "cryptobot"
    autostart.set_enabled(False, tmp_path)
    assert not p.exists()


def test_format_fill():
    assert format_fill("TEST", "BTC/USDT", "buy", 0.001, 80000, "signal entry", None) == "[TEST] BUY BTC/USDT 0.001000 @ 80,000.00 (signal entry)"
    assert format_fill("BUY", "BTC/USDT", "sell", 0.001, 81000, "stop-loss", -1.5).endswith("P&L -1.50")
