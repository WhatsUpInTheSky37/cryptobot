"""Order executors. TEST and BUY share everything upstream; only this differs."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

import ccxt

from cryptobot.core.models import Mode
from cryptobot.engine.risk import Approval, RiskManager

log = logging.getLogger("executor")


@dataclass(frozen=True)
class Fill:
    ts_ms: int
    symbol: str
    side: str
    price: float  # actual average fill price
    amount: float
    fee: float  # in quote currency
    reason: str
    mode: str
    order_id: str | None = None
    status: str = "filled"  # filled | partial
    expected_price: float = 0.0  # market price when the decision was made

    @property
    def notional(self) -> float:
        return self.price * self.amount

    @property
    def slippage_pct(self) -> float:
        """Positive = worse than expected."""
        if not self.expected_price:
            return 0.0
        raw = (self.price / self.expected_price - 1.0) * 100.0
        return raw if self.side == "buy" else -raw


class ExecutionError(Exception):
    """The exchange refused or could not process the order (not retryable)."""


class Executor:
    mode: Mode

    def execute(self, approval: Approval, market_price: float, now_ms: int, urgent: bool = False) -> Fill | None:  # pragma: no cover
        raise NotImplementedError


class SimulatedExecutor(Executor):
    """Fills instantly at the live price with slippage and the exchange's taker fee. Sends nothing anywhere."""

    mode = Mode.TEST

    def __init__(self, risk: RiskManager, slippage_pct: float = 0.05) -> None:
        self.risk = risk
        self.slip = slippage_pct / 100.0

    def execute(self, approval: Approval, market_price: float, now_ms: int, urgent: bool = False) -> Fill | None:
        a = self.risk.consume(approval.token)  # raises if the approval is missing or reused
        px = market_price * (1 + self.slip) if a.intent.side == "buy" else market_price * (1 - self.slip)
        fee = px * a.amount * a.fee_rate
        return Fill(now_ms, a.intent.symbol, a.intent.side, px, a.amount, fee, a.intent.reason, Mode.TEST.value, expected_price=market_price)


class LiveExecutor(Executor):
    """Real orders through ccxt.

    Places a *marketable limit* order (limit price a little through the market so
    it fills at once but can never fill worse than the slippage cap), waits for
    the fill, cancels any remainder after a timeout, and reports the actual
    average price, fee and order id. Retries network errors without duplicating
    orders by tagging every order with the approval token as its client id.
    """

    mode = Mode.BUY
    CLIENT_PREFIX = "cb"

    def __init__(
        self,
        risk: RiskManager,
        exchange: Any,
        *,
        exchange_id: str = "",
        max_slippage_pct: float = 0.25,
        fill_timeout_s: float = 15.0,
        poll_s: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if exchange is None:
            raise ValueError("LiveExecutor needs an authenticated exchange")
        self.risk = risk
        self.exchange = exchange
        self.exchange_id = exchange_id or getattr(exchange, "id", "")
        self.max_slip = max_slippage_pct / 100.0
        self.fill_timeout_s = fill_timeout_s
        self.poll_s = poll_s
        self._sleep = sleep
        self._clock = clock
        self.orders_placed = 0

    # -- public ----------------------------------------------------------------

    def execute(self, approval: Approval, market_price: float, now_ms: int, urgent: bool = False) -> Fill | None:
        a = self.risk.consume(approval.token)
        sym, side = a.intent.symbol, a.intent.side
        amount = float(self._amount_prec(sym, a.amount))
        if amount <= 0:
            return None
        cid = f"{self.CLIENT_PREFIX}{a.token[:24]}"
        if urgent:
            otype, price = "market", None
        else:
            otype = "limit"
            raw = market_price * (1 + self.max_slip) if side == "buy" else market_price * (1 - self.max_slip)
            price = float(self._price_prec(sym, raw))
        order = self._place_with_retry(sym, otype, side, amount, price, cid)
        order = self._wait_for_fill(order, sym)
        filled = float(order.get("filled") or 0.0)
        if filled <= 0:
            log.warning("%s %s %s: order %s not filled (%s)", side, sym, amount, order.get("id"), order.get("status"))
            return None
        avg = float(order.get("average") or order.get("price") or price or market_price)
        fee = self._fee_in_quote(order, sym, avg, a.fee_rate)
        remaining = float(order.get("remaining") or 0.0)
        status = "filled" if remaining <= 1e-12 else "partial"
        return Fill(now_ms, sym, side, avg, filled, fee, a.intent.reason, Mode.BUY.value, str(order.get("id")), status, market_price)

    def cancel_ours(self, symbol: str) -> int:
        """Cancel open orders this app placed (by client id prefix). Returns the count."""
        n = 0
        try:
            for o in self.exchange.fetch_open_orders(symbol):
                cid = str(o.get("clientOrderId") or "")
                if cid.startswith(self.CLIENT_PREFIX):
                    self.exchange.cancel_order(o["id"], symbol)
                    n += 1
        except Exception as e:  # noqa: BLE001
            log.warning("cancel_ours(%s) failed: %s", symbol, e)
        return n

    def balance(self, currency: str) -> float:
        bal = self.exchange.fetch_balance()
        total = bal.get("total") or {}
        return float(total.get(currency) or 0.0)

    def taker_fee(self, symbol: str) -> float | None:
        try:
            if getattr(self.exchange, "has", {}).get("fetchTradingFee"):
                f = self.exchange.fetch_trading_fee(symbol)
                if f and f.get("taker") is not None:
                    return float(f["taker"])
        except Exception as e:  # noqa: BLE001
            log.info("fetch_trading_fee unavailable: %s", type(e).__name__)
        return None

    # -- internals -------------------------------------------------------------

    def _amount_prec(self, sym: str, amount: float) -> float:
        try:
            return float(self.exchange.amount_to_precision(sym, amount))
        except Exception:  # noqa: BLE001
            return amount

    def _price_prec(self, sym: str, price: float) -> float:
        try:
            return float(self.exchange.price_to_precision(sym, price))
        except Exception:  # noqa: BLE001
            return price

    def _client_params(self, cid: str) -> dict[str, Any]:
        if self.exchange_id == "kraken":
            return {"userref": int(cid[2:10], 16) % 2_000_000_000}
        return {"clientOrderId": cid}

    def _find_by_client_id(self, sym: str, cid: str) -> dict[str, Any] | None:
        params = self._client_params(cid)
        try:
            if "clientOrderId" in params and getattr(self.exchange, "has", {}).get("fetchOrder"):
                try:
                    o = self.exchange.fetch_order(None, sym, params)
                    if o:
                        return o
                except Exception:  # noqa: BLE001
                    pass
            for fetch in ("fetch_open_orders", "fetch_closed_orders"):
                fn = getattr(self.exchange, fetch, None)
                if fn is None:
                    continue
                for o in fn(sym, None, 50):
                    if str(o.get("clientOrderId") or "") == cid or (params.get("userref") is not None and o.get("info", {}).get("userref") == params["userref"]):
                        return o
        except Exception as e:  # noqa: BLE001
            log.warning("lookup by client id failed: %s", e)
        return None

    def _place_with_retry(self, sym: str, otype: str, side: str, amount: float, price: float | None, cid: str) -> dict[str, Any]:
        delay = 1.0
        last: Exception | None = None
        for attempt in range(4):
            try:
                o = self.exchange.create_order(sym, otype, side, amount, price, self._client_params(cid))
                self.orders_placed += 1
                return o
            except ccxt.InsufficientFunds as e:
                raise ExecutionError(f"insufficient funds on the exchange: {e}") from e
            except ccxt.InvalidOrder as e:
                raise ExecutionError(f"the exchange rejected the order: {e}") from e
            except (ccxt.AuthenticationError, ccxt.PermissionDenied) as e:
                raise ExecutionError(f"API key problem: {e}") from e
            except (ccxt.RateLimitExceeded, ccxt.NetworkError, ccxt.ExchangeNotAvailable) as e:
                last = e
                log.warning("create_order attempt %d failed: %s", attempt + 1, type(e).__name__)
                existing = self._find_by_client_id(sym, cid)  # it may have gone through
                if existing:
                    return existing
                self._sleep(delay)
                delay = min(8.0, delay * 2)
        raise ExecutionError(f"could not place order after retries: {last}")

    def _wait_for_fill(self, order: dict[str, Any], sym: str) -> dict[str, Any]:
        deadline = self._clock() + self.fill_timeout_s
        oid = order.get("id")
        cur = order
        while True:
            status = (cur.get("status") or "open").lower()
            remaining = float(cur.get("remaining") or 0.0)
            if status in ("closed", "filled") or (status == "open" and remaining <= 1e-12 and cur.get("filled")):
                return cur
            if status in ("canceled", "cancelled", "rejected", "expired"):
                return cur
            if self._clock() >= deadline:
                break
            self._sleep(self.poll_s)
            try:
                cur = self.exchange.fetch_order(oid, sym)
            except Exception as e:  # noqa: BLE001
                log.warning("fetch_order failed: %s", type(e).__name__)
        # timed out with something left: cancel the remainder, then take whatever filled
        try:
            self.exchange.cancel_order(oid, sym)
        except Exception as e:  # noqa: BLE001
            log.warning("cancel after timeout failed: %s", type(e).__name__)
        try:
            cur = self.exchange.fetch_order(oid, sym)
        except Exception:  # noqa: BLE001
            pass
        return cur

    @staticmethod
    def _fee_in_quote(order: dict[str, Any], sym: str, avg: float, fallback_rate: float) -> float:
        base, _, quote = sym.partition("/")
        fees = order.get("fees") or ([order["fee"]] if order.get("fee") else [])
        total = 0.0
        found = False
        for f in fees:
            if not f or f.get("cost") is None:
                continue
            found = True
            cost, cur = float(f["cost"]), f.get("currency")
            if cur == base:
                cost *= avg
            elif cur not in (None, quote):
                continue  # fee paid in a third asset (e.g. BNB): not convertible here, leave it out
            total += cost
        if found:
            return total
        return float(order.get("filled") or 0.0) * avg * fallback_rate


class ReadOnlyExchange:
    """Wrapper that lets code read market data but never place, cancel, or withdraw."""

    _BLOCKED = (
        "create_order", "create_limit_order", "create_market_order", "create_limit_buy_order",
        "create_limit_sell_order", "create_market_buy_order", "create_market_sell_order",
        "create_orders", "edit_order", "cancel_order", "cancel_orders", "cancel_all_orders",
        "withdraw", "transfer", "create_deposit_address", "borrow_margin", "repay_margin",
    )

    def __init__(self, exchange) -> None:
        object.__setattr__(self, "_ex", exchange)

    def __getattr__(self, name: str):
        if name in self._BLOCKED or name.startswith("create_") or name.startswith("cancel_"):
            raise PermissionError(f"{name} is not allowed in TEST mode")
        return getattr(self._ex, name)

    def __setattr__(self, name: str, value) -> None:
        setattr(self._ex, name, value)
