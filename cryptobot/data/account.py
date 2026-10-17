"""Test an exchange API key: does it work, can it trade, and (critically) can it withdraw?"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import ccxt

log = logging.getLogger(__name__)

QUOTES = ("USD", "USDT", "USDC")


@dataclass
class ConnectionReport:
    ok: bool
    message: str
    can_read: bool = False
    can_trade: bool | None = None  # None = could not verify
    can_withdraw: bool | None = None
    ip_restricted: bool | None = None
    balances: dict[str, float] = field(default_factory=dict)  # quote currencies only

    @property
    def safe_to_save(self) -> bool:
        return self.ok and self.can_withdraw is not True

    def summary(self) -> str:
        def yn(v: bool | None, good_when: bool) -> str:
            if v is None:
                return "unknown"
            return ("✔" if v == good_when else "✘") + (" yes" if v else " no")

        parts = [
            f"Read {yn(self.can_read, True)}",
            f"Trade {yn(self.can_trade, True)}",
            f"Withdraw {yn(self.can_withdraw, False)}" + (" (good)" if self.can_withdraw is False else " (DANGEROUS)" if self.can_withdraw else ""),
            f"IP restricted {yn(self.ip_restricted, True)}",
        ]
        return " · ".join(parts)


def make_exchange(exchange_id: str, key: str, secret: str, passphrase: str | None = None):
    cls = getattr(ccxt, exchange_id)
    cfg = {"apiKey": key, "secret": secret, "enableRateLimit": True}
    if passphrase:
        cfg["password"] = passphrase
    return cls(cfg)


def check_permissions(ex, exchange_id: str) -> tuple[bool | None, bool | None, bool | None]:
    """Return (can_trade, can_withdraw, ip_restricted); None where the exchange gives no answer."""
    try:
        if exchange_id in ("binance", "binanceus") and hasattr(ex, "sapiGetAccountApiRestrictions"):
            r = ex.sapiGetAccountApiRestrictions()
            return (
                bool(r.get("enableSpotAndMarginTrading")),
                bool(r.get("enableWithdrawals")),
                bool(r.get("ipRestrict")),
            )
        if exchange_id == "coinbase" and hasattr(ex, "v3PrivateGetBrokerageKeyPermissions"):
            r = ex.v3PrivateGetBrokerageKeyPermissions()
            return bool(r.get("can_trade")), bool(r.get("can_transfer")), None
    except Exception as e:  # noqa: BLE001
        log.warning("permission check failed on %s: %s", exchange_id, type(e).__name__)
    return None, None, None


def check_connection(exchange_id: str, key: str, secret: str, passphrase: str | None = None, exchange=None) -> ConnectionReport:
    try:
        ex = exchange or make_exchange(exchange_id, key, secret, passphrase)
        bal = ex.fetch_balance()
    except ccxt.AuthenticationError as e:
        return ConnectionReport(False, f"The exchange rejected the key: {str(e)[:160]}")
    except ccxt.PermissionDenied as e:
        return ConnectionReport(False, f"Permission denied (is your IP whitelisted?): {str(e)[:160]}")
    except Exception as e:  # noqa: BLE001
        return ConnectionReport(False, f"Could not connect: {type(e).__name__}: {str(e)[:160]}")
    total = bal.get("total") or {}
    balances = {q: float(total.get(q) or 0.0) for q in QUOTES if total.get(q)}
    can_trade, can_withdraw, ip_restricted = check_permissions(ex, exchange_id)
    rep = ConnectionReport(True, "Connected.", True, can_trade, can_withdraw, ip_restricted, balances)
    if can_withdraw is True:
        rep.message = "This key can WITHDRAW funds. Delete it on the exchange and create one with withdrawals disabled. It will not be saved."
    elif can_withdraw is None:
        rep.message = "Connected, but this exchange does not report key permissions. Confirm yourself that withdrawals are disabled."
    elif can_trade is False:
        rep.message = "Connected, but the key cannot trade. TEST mode works; BUY mode will need a key with spot trading enabled."
    return rep
