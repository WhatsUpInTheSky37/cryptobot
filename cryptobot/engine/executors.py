"""Order executors. TEST and BUY share everything upstream; only this differs."""
from __future__ import annotations

import logging
import math
import time
import threading
import uuid
from dataclasses import asdict, dataclass
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
    quote_fee: float | None = None
    base_fee: float = 0.0
    fees: tuple[dict[str, Any], ...] = ()
    execution_key: str | None = None
    client_id: str | None = None
    fee_estimated: bool = False

    @property
    def cash_fee(self) -> float:
        return self.fee if self.quote_fee is None else self.quote_fee

    @property
    def external_fee_value(self) -> float:
        return max(0.0, self.fee - self.cash_fee - self.base_fee * self.price)

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


class OrderUncertain(ExecutionError):
    """No new order may replace this request until its exchange state is resolved."""


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
        a = self.risk.consume(approval.token, now_ms)
        px = market_price * (1 + self.slip) if a.intent.side == "buy" else market_price * (1 - self.slip)
        fee = px * a.amount * a.fee_rate
        return Fill(now_ms, a.intent.symbol, a.intent.side, px, a.amount, fee, a.intent.reason, Mode.TEST.value, expected_price=market_price)


class LiveExecutor(Executor):
    """Real orders through ccxt.

    Places a *marketable limit* order (limit price a little through the market so
    it fills at once but can never fill worse than the slippage cap), waits for
    the fill, cancels any remainder after a timeout, and reports the actual
    average price, fee and order id. Ambiguous responses are recovered by the
    journaled client id; an unresolved request is never blindly submitted again.
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
        store: Any = None,
        scope: str = "",
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
        self.store, self.scope = store, scope
        self.interrupt = threading.Event()

    # -- public ----------------------------------------------------------------

    def execute(self, approval: Approval, market_price: float, now_ms: int, urgent: bool = False) -> Fill | None:
        a = self.risk.consume(approval.token, now_ms)
        sym, side = a.intent.symbol, a.intent.side
        amount = float(self._amount_prec(sym, a.amount))
        if not math.isfinite(amount) or amount <= 0 or not math.isfinite(market_price) or market_price <= 0:
            raise ExecutionError("invalid rounded amount or market price")
        if amount > a.amount + max(1e-12, a.amount * 1e-12):
            raise ExecutionError("venue precision increased the approved quantity")
        cid = f"{self.CLIENT_PREFIX}{a.token[:16]}"  # Kraken free-text IDs allow 18 characters
        if urgent:
            otype, price = "market", None
        else:
            otype = "limit"
            raw = market_price * (1 + self.max_slip) if side == "buy" else market_price * (1 - self.max_slip)
            price = float(self._price_prec(sym, raw))
            # Venue precision must never round a buy beyond the approved spend.
            if side == "buy" and price > raw + abs(raw) * 1e-12:
                raise ExecutionError("rounded limit exceeds the approved slippage cap")
        if self.store:
            self.store.journal_order(cid, self.scope, sym, {"approval": asdict(a), "market_price": market_price, "now_ms": now_ms, "urgent": urgent})
        self.interrupt.clear()
        try:
            order = self._place_with_retry(sym, otype, side, amount, price, cid)
            if self.store:
                self.store.update_order(cid, "acknowledged", order)
            order = self._wait_for_fill(order, sym)
        except OrderUncertain:
            if self.store:
                self.store.update_order(cid, "unknown")
            raise
        except ExecutionError:
            if self.store:
                self.store.update_order(cid, "rejected")
            raise
        try:
            fill = self.fill_from_order(order, a, market_price, now_ms, cid)
        except OrderUncertain:
            # Known purchased inventory still needs protection when commission
            # valuation or execution-detail retrieval is temporarily unavailable.
            if side == "buy" and float(order.get("filled") or 0) > 0 and a.intent.stop_price is not None:
                fees = order.get("fees") or ([order["fee"]] if order.get("fee") else [])
                base = sym.partition("/")[0]
                units = float(order["filled"]) - sum(float(f.get("cost") or 0) for f in fees if f.get("currency") == base)
                protection = Approval(type(a.intent)(sym, "sell", units, market_price, "exchange stop-loss"), units, a.fee_rate, uuid.uuid4().hex, now_ms)
                try:
                    self.protect(protection, a.intent.stop_price, now_ms)
                except ExecutionError:
                    log.critical("known inventory on %s could not receive native protection; operator reconciliation required", sym)
            if self.store:
                self.store.update_order(cid, "unknown", order)
            raise
        if self.store:
            self.store.update_order(cid, "filled" if fill else "canceled_empty", order, asdict(fill) if fill else None, applied=False if fill else True)
        return fill

    def fill_from_order(self, order: dict[str, Any], a: Approval, market_price: float, now_ms: int, cid: str) -> Fill | None:
        sym, side = a.intent.symbol, a.intent.side
        filled = float(order.get("filled") or 0.0)
        if filled <= 0:
            log.warning("%s %s %s: order %s not filled (%s)", side, sym, a.amount, order.get("id"), order.get("status"))
            return None
        cost = float(order.get("cost") or 0.0)
        if not order.get("average") and cost <= 0:
            try:
                trades = self.exchange.fetch_my_trades(sym, a.issued_ms - 60_000)
                trades = [t for t in trades if str(t.get("order")) == str(order["id"])]
                traded = sum(float(t.get("amount") or 0) for t in trades)
                if not trades or abs(traded - filled) > max(1e-12, filled * 1e-8):
                    raise ValueError("incomplete execution history")
                cost = sum(float(t.get("cost") or float(t["price"]) * float(t["amount"])) for t in trades)
                order["trades"], order["cost"] = trades, cost
            except Exception as error:
                raise OrderUncertain("filled quantity has no confirmed execution price") from error
        avg = float(order.get("average") or cost / filled)
        fee, quote_fee, base_fee, fees, estimated = self._settle_fees(order, sym, avg, a.fee_rate)
        remaining = float(order.get("remaining") or 0.0)
        status = "filled" if remaining <= 1e-12 else "partial"
        ts = int(order.get("lastTradeTimestamp") or order.get("timestamp") or now_ms)
        return Fill(ts, sym, side, avg, filled, fee, a.intent.reason, Mode.BUY.value, str(order.get("id")), status, market_price,
                    quote_fee, base_fee, tuple(fees), f"{self.scope}:{order['id']}", cid, estimated)

    def cancel_ours(self, symbol: str) -> int:
        """Cancel open orders this app placed (by client id prefix). Returns the count."""
        n = 0
        try:
            for o in self.exchange.fetch_open_orders(symbol):
                cid = str(o.get("clientOrderId") or "")
                if cid.startswith(self.CLIENT_PREFIX):
                    final = self._wait_for_fill(o, symbol, cancel_immediately=True)
                    if float(final.get("filled") or 0) > 0:
                        raise OrderUncertain("leftover order has fills that require journal reconciliation")
                    n += 1
        except Exception as e:
            raise OrderUncertain(f"cannot confirm cancellation on {symbol}: {e}") from e
        return n

    def protect(self, a: Approval, stop: float, now_ms: int) -> tuple[str, dict[str, Any]]:
        """Native stop remains at the venue when this process or its feed stops."""
        capability = getattr(self.exchange, "feature_value", lambda *args: False)
        if not capability(a.intent.symbol, "createOrder", "stopLossPrice"):
            raise ExecutionError("this market has no verified native stop capability; entries paused")
        cid = f"{self.CLIENT_PREFIX}{a.token[:16]}"
        amount = self._amount_prec(a.intent.symbol, a.amount)
        trigger = self._price_prec(a.intent.symbol, stop)
        otype, price, params = "limit", self._price_prec(a.intent.symbol, trigger * (1 - self.max_slip)), {"stopLossPrice": trigger}
        if self.exchange_id == "kraken":
            otype, price, params = "stop-loss", trigger, {}
        elif self.exchange_id in ("binance", "binanceus"):
            market = (getattr(self.exchange, "markets", {}) or {}).get(a.intent.symbol, {})
            order_types = market.get("info", {}).get("orderTypes", [])
            if "STOP_LOSS" in order_types:
                otype, price = "market", None
        if self.store:
            self.store.journal_order(cid, self.scope, a.intent.symbol, {"approval": asdict(a), "market_price": stop, "now_ms": now_ms, "role": "protection"})
        try:
            order = self._place_with_retry(a.intent.symbol, otype, "sell", amount, price, cid, params)
        except OrderUncertain:
            if self.store:
                self.store.update_order(cid, "unknown")
            raise
        except ExecutionError:
            if self.store:
                self.store.update_order(cid, "rejected")
            raise
        if self.store:
            self.store.update_order(cid, "protecting", order)
        return cid, order

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
        except Exception as e:
            raise ExecutionError(f"cannot validate amount precision for {sym}") from e

    def _price_prec(self, sym: str, price: float) -> float:
        try:
            return float(self.exchange.price_to_precision(sym, price))
        except Exception as e:
            raise ExecutionError(f"cannot validate price precision for {sym}") from e

    def _client_params(self, cid: str) -> dict[str, Any]:
        if self.exchange_id == "kraken":
            return {"cl_ord_id": cid}
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
                    if str(o.get("clientOrderId") or o.get("info", {}).get("cl_ord_id") or "") == cid:
                        return o
        except Exception as e:  # noqa: BLE001
            log.warning("lookup by client id failed: %s", e)
        return None

    def _place_with_retry(self, sym: str, otype: str, side: str, amount: float, price: float | None, cid: str, extra_params: dict[str, Any] | None = None) -> dict[str, Any]:
        for attempt in range(1):
            try:
                o = self.exchange.create_order(sym, otype, side, amount, price, {**self._client_params(cid), **(extra_params or {})})
                self.orders_placed += 1
                return o
            except ccxt.InsufficientFunds as e:
                raise ExecutionError(f"insufficient funds on the exchange: {e}") from e
            except ccxt.InvalidOrder as e:
                raise ExecutionError(f"the exchange rejected the order: {e}") from e
            except (ccxt.AuthenticationError, ccxt.PermissionDenied) as e:
                raise ExecutionError(f"API key problem: {e}") from e
            except (ccxt.RateLimitExceeded, ccxt.NetworkError, ccxt.ExchangeNotAvailable) as e:
                log.warning("create_order attempt %d failed: %s", attempt + 1, type(e).__name__)
                existing = self._find_by_client_id(sym, cid)  # it may have gone through
                if existing:
                    return existing
                raise OrderUncertain(f"submission status unknown for {cid}; entries paused until reconciled") from e
            except ccxt.ExchangeError as e:
                raise OrderUncertain(f"exchange response did not establish order status for {cid}: {type(e).__name__}") from e

    def _wait_for_fill(self, order: dict[str, Any], sym: str, cancel_immediately: bool = False) -> dict[str, Any]:
        deadline = self._clock() + (0 if cancel_immediately else self.fill_timeout_s)
        oid = order.get("id")
        cur = order
        while True:
            status = (cur.get("status") or "open").lower()
            remaining = float(cur.get("remaining") or 0.0)
            if status in ("closed", "filled") and cur.get("filled") is not None:
                return cur
            if status in ("canceled", "cancelled", "rejected", "expired") and cur.get("filled") is not None:
                return cur
            if self._clock() >= deadline or self.interrupt.is_set():
                break
            self._sleep(self.poll_s)
            try:
                cur = self.exchange.fetch_order(oid, sym)
            except Exception as e:  # noqa: BLE001
                log.warning("fetch_order failed: %s", type(e).__name__)
        # timed out with something left: cancel the remainder, then take whatever filled
        try:
            canceled = self.exchange.cancel_order(oid, sym)
            if canceled and canceled.get("status") in ("closed", "filled", "canceled", "cancelled", "expired", "rejected"):
                cur = canceled
        except Exception as e:  # noqa: BLE001
            log.warning("cancel after timeout failed: %s", type(e).__name__)
        try:
            cur = self.exchange.fetch_order(oid, sym)
        except Exception:  # noqa: BLE001
            pass
        if (cur.get("status") or "").lower() not in ("closed", "filled", "canceled", "cancelled", "expired", "rejected") or cur.get("filled") is None:
            raise OrderUncertain(f"order {oid} remains open or unknown after cancellation")
        return cur

    def _settle_fees(self, order: dict[str, Any], sym: str, avg: float, fallback_rate: float):
        base, _, quote = sym.partition("/")
        fees = order.get("fees") or ([order["fee"]] if order.get("fee") else [])
        if not fees and order.get("trades"):
            fees = [f for t in order["trades"] for f in (t.get("fees") or ([t["fee"]] if t.get("fee") else []))]
        estimated = not bool(fees)
        if estimated:
            fees = [{"cost": float(order.get("filled") or 0) * avg * fallback_rate, "currency": quote, "estimated": True}]
        total = quote_fee = base_fee = 0.0
        for f in fees:
            cost, cur = float(f.get("cost") or 0), f.get("currency") or quote
            if cur == quote:
                value = cost
                quote_fee += cost
            elif cur == base:
                value = cost * avg
                base_fee += cost
            else:
                try:
                    ticker = self.exchange.fetch_ticker(f"{cur}/{quote}")
                    rate = float(ticker.get("last") or ticker.get("close") or 0)
                    if not math.isfinite(rate) or rate <= 0:
                        raise ValueError("no commission conversion rate")
                    value = cost * rate
                except Exception as e:
                    raise OrderUncertain(f"commission valuation unresolved for {cur}; original fee retained in journal") from e
            f["quote_value"] = value
            total += value
        return total, quote_fee, base_fee, fees, estimated

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
                raise OrderUncertain(f"commission conversion required for {cur}")
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

