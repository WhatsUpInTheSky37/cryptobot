"""Entry point of the trading-engine process.

This module must never import Qt. It receives commands on one queue and sends
events on another. In Milestone 1 the engine only runs the market data feed;
strategy, risk, and executors are added in later milestones on top of it.
"""
from __future__ import annotations

import asyncio
import logging
import queue
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from cryptobot.core.events import AlertEvent, ConnectionEvent, Event, LogEvent
from cryptobot.core.models import Mode
from cryptobot.data.feed import MarketFeed
from cryptobot.db.store import Store
from cryptobot.engine.risk import RiskLimits
from cryptobot.engine.trader import Trader
from cryptobot.earlywarning.config import EWConfig
from cryptobot.earlywarning.data import BinanceUSData, make_client
from cryptobot.earlywarning.engine import EarlyWarningEngine
from cryptobot.earlywarning.store import EWStore
from cryptobot.pulse.config import PulseConfig
from cryptobot.pulse.engine import PulseEngine
from cryptobot.strategy.config import StrategyConfig

log = logging.getLogger("engine")


class _QueueLogHandler(logging.Handler):
    def __init__(self, evt_q) -> None:
        super().__init__(level=logging.INFO)
        self._q = evt_q

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._q.put(LogEvent(record.levelname, record.name, self.format(record), int(record.created * 1000)))
        except Exception:  # noqa: BLE001
            pass


def _setup_logging(evt_q, log_path: str | None) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    if log_path:
        fh = RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=3)
        fh.setFormatter(fmt)
        root.addHandler(fh)
    qh = _QueueLogHandler(evt_q)
    qh.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(qh)
    logging.getLogger("ccxt").setLevel(logging.WARNING)


def engine_main(cmd_q, evt_q, config: dict[str, Any]) -> None:
    """Process target. ``config`` keys: exchange, symbols, db_path, log_path."""
    _setup_logging(evt_q, config.get("log_path"))
    try:
        asyncio.run(_run(cmd_q, evt_q, config))
    except Exception:  # noqa: BLE001
        log.exception("engine crashed")
        raise


async def _run(cmd_q, evt_q, config: dict[str, Any]) -> None:
    store = Store(Path(config["db_path"]))
    holder: dict[str, Any] = {"trader": None, "pulse": None, "ew": None, "ew_task": None, "ew_stop": None, "conn": None, "pulse_feed_task": None, "pulse_feed_stop": None}
    base_tfs = tuple(config.get("timeframes_s", (1, 60, 300)))

    def emit_all(ev: Event) -> None:
        evt_q.put(ev)
        if isinstance(ev, ConnectionEvent):
            holder["conn"] = ev  # replayed to engines that start after the feed connected
        t: Trader | None = holder["trader"]
        if t is not None:
            try:
                t.on_event(ev)
            except Exception:  # noqa: BLE001 - a trader bug must not kill the data feed
                log.exception("trader failed on %s", type(ev).__name__)
        pu: PulseEngine | None = holder["pulse"]
        if pu is not None and not getattr(pu, "own_feed", False):
            try:
                pu.on_event(ev)
            except Exception:  # noqa: BLE001
                log.exception("pulse failed on %s", type(ev).__name__)

    def make_feed(exchange: str, symbols: list[str], extra_tf: int | None = None) -> MarketFeed:
        tfs = base_tfs + ((extra_tf,) if extra_tf and extra_tf not in base_tfs else ())
        return MarketFeed(
            exchange,
            symbols,
            emit_all,
            timeframes_s=tfs,
            history_bars=int(config.get("history_bars", 300)),
            on_closed_bars=lambda bars: store.upsert_bars(exchange, bars),
        )

    stop = asyncio.Event()
    market = {"exchange": config["exchange"], "symbols": list(config["symbols"]), "tf": None}
    feed = make_feed(market["exchange"], market["symbols"])
    feed_task = asyncio.create_task(feed.run(stop))
    log.info("engine started: %s %s", config["exchange"], ", ".join(config["symbols"]))
    store.audit("engine", "started", exchange=config["exchange"], symbols=config["symbols"])

    async def restart_feed() -> None:
        nonlocal stop, feed, feed_task
        stop.set()
        try:
            await asyncio.wait_for(feed_task, timeout=10)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            feed_task.cancel()
        stop = asyncio.Event()
        feed = make_feed(market["exchange"], market["symbols"], market["tf"])
        feed_task = asyncio.create_task(feed.run(stop))

    async def stop_pulse_feed() -> None:
        if holder["pulse_feed_stop"] is not None:
            holder["pulse_feed_stop"].set()
        if holder["pulse_feed_task"] is not None:
            try:
                await asyncio.wait_for(holder["pulse_feed_task"], timeout=10)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                holder["pulse_feed_task"].cancel()
        holder["pulse_feed_task"], holder["pulse_feed_stop"] = None, None

    loop = asyncio.get_running_loop()
    try:
        while True:
            try:
                cmd = await loop.run_in_executor(None, _get_cmd, cmd_q)
            except queue.Empty:
                if feed_task.done() and feed_task.exception() is not None:
                    log.error("feed task died: %r", feed_task.exception())
                    feed_task = asyncio.create_task(feed.run(stop))
                continue
            kind = cmd.get("type")
            trader: Trader | None = holder["trader"]
            if kind == "stop":
                break
            if kind == "set_market":
                market["exchange"], market["symbols"] = cmd["exchange"], list(cmd["symbols"])
                await restart_feed()
                log.info("market changed: %s %s", market["exchange"], ", ".join(market["symbols"]))
            elif kind == "start_trading":
                if trader is not None:
                    trader.stop("restarting")
                    holder["trader"] = None
                strategy = StrategyConfig.model_validate(cmd["strategy"])
                limits = RiskLimits.model_validate(cmd["limits"])
                mode = Mode(cmd.get("mode", "TEST"))
                market["exchange"], market["symbols"], market["tf"] = cmd["exchange"], list(cmd["symbols"]), strategy.timeframe_s
                exchange_obj = None
                creds = cmd.pop("credentials", None)  # never logged, never persisted
                if mode == Mode.BUY and creds:
                    from cryptobot.data.account import make_exchange

                    exchange_obj = make_exchange(market["exchange"], creds["key"], creds["secret"], creds.get("passphrase"))
                    exchange_obj.load_markets()
                try:
                    t = Trader(store, evt_q.put, market["exchange"], market["symbols"], strategy, limits, mode=mode,
                               slippage_pct=float(cmd.get("slippage_pct", 0.05)), reset_peak=bool(cmd.get("reset_peak", False)),
                               exchange=exchange_obj)
                except (NotImplementedError, ValueError) as e:
                    log.error("cannot start: %s", e)
                    evt_q.put(LogEvent("ERROR", "trader", f"Cannot start {mode.value} mode: {e}", int(time.time() * 1000)))
                    evt_q.put(AlertEvent("bad", f"Cannot start {mode.value} mode", str(e), int(time.time() * 1000)))
                    continue
                t.start()
                holder["trader"] = t
                await restart_feed()  # re-sends history so the trader can decide immediately
            elif kind == "stop_trading":
                if trader is not None:
                    trader.stop()
                    holder["trader"] = None
            elif kind == "emergency_stop":
                if trader is not None:
                    trader.emergency_stop()
                    holder["trader"] = None
            elif kind == "resume":
                if trader is not None and not trader.resume():
                    evt_q.put(LogEvent("WARNING", "trader", "Cannot resume yet: the daily limit resets at local midnight.", int(time.time() * 1000)))
            elif kind == "pulse_start":
                if holder["pulse"] is not None:
                    holder["pulse"].stop()
                    holder["pulse"] = None
                await stop_pulse_feed()
                pcfg = PulseConfig.model_validate(cmd.get("config") or {})
                p_ex = cmd.get("exchange") or market["exchange"]
                p_syms = list(cmd.get("symbols") or market["symbols"])
                own_feed = (p_ex, p_syms) != (market["exchange"], list(market["symbols"]))
                pu = PulseEngine(store, evt_q.put, p_ex, p_syms, pcfg, slippage_pct=float(cmd.get("slippage_pct", 0.05)))
                if own_feed:
                    # Pulse is paper-only and needs no key: it may watch a liquid exchange while the trader stays on another.
                    # Its feed's events go to the Pulse engine only, never to the Market page or the trader.
                    def pulse_emit(ev: Event, _pu=pu) -> None:
                        try:
                            _pu.on_event(ev)
                        except Exception:  # noqa: BLE001
                            log.exception("pulse failed on %s", type(ev).__name__)

                    pfeed = MarketFeed(p_ex, p_syms, pulse_emit, timeframes_s=(1, 60), history_bars=120)
                    holder["pulse_feed_stop"] = asyncio.Event()
                    holder["pulse_feed_task"] = asyncio.create_task(pfeed.run(holder["pulse_feed_stop"]), name="pulse-feed")
                    pu.own_feed = True
                elif holder["conn"] is not None:
                    pu.on_event(holder["conn"])  # the main feed may already be connected; Pulse must not wait for an event that never comes
                pu.start()
                holder["pulse"] = pu
                log.info("pulse started on %s %s%s", p_ex, ", ".join(p_syms), " (own feed)" if own_feed else "")
            elif kind == "pulse_stop":
                if holder["pulse"] is not None:
                    holder["pulse"].stop()
                    holder["pulse"] = None
                await stop_pulse_feed()
            elif kind == "ew_start":
                if holder["ew"] is not None:
                    holder["ew_stop"].set()
                    try:
                        await asyncio.wait_for(holder["ew_task"], timeout=15)
                    except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                        holder["ew_task"].cancel()
                ew_store = EWStore(Path(config["db_path"]).with_name("earlywarning.db"))
                ew_cfg = EWConfig.model_validate(cmd.get("config") or {})
                ew_store.set("config", ew_cfg.model_dump())
                ex_id = cmd.get("exchange", "binanceus")
                ew = EarlyWarningEngine(ew_store, evt_q.put, ex_id, ew_cfg, data=BinanceUSData(make_client(ex_id), ew_store))
                holder["ew"], holder["ew_stop"] = ew, asyncio.Event()
                holder["ew_task"] = asyncio.create_task(ew.run(holder["ew_stop"]), name="earlywarning")
                log.info("early warning started on %s", ex_id)
            elif kind == "ew_stop":
                if holder["ew"] is not None:
                    holder["ew_stop"].set()
                    try:
                        await asyncio.wait_for(holder["ew_task"], timeout=15)
                    except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                        holder["ew_task"].cancel()
                    holder["ew"] = None
            elif kind == "pulse_train":
                if holder["pulse"] is not None:
                    note = holder["pulse"].train_now()
                    evt_q.put(LogEvent("INFO", "pulse", f"ML: {note}", int(time.time() * 1000)))
                else:
                    evt_q.put(LogEvent("WARNING", "pulse", "Start Pulse before training the model.", int(time.time() * 1000)))
            elif kind == "set_limits":
                if trader is not None:
                    trader.set_limits(RiskLimits.model_validate(cmd["limits"]))
            elif kind == "ping":
                evt_q.put(LogEvent("DEBUG", "engine", "pong", int(time.time() * 1000)))
    finally:
        trader = holder["trader"]
        if trader is not None:
            trader.stop("engine shutting down")
        if holder["pulse"] is not None:
            holder["pulse"].stop()
        await stop_pulse_feed()
        if holder["ew"] is not None:
            holder["ew_stop"].set()
            try:
                await asyncio.wait_for(holder["ew_task"], timeout=10)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                holder["ew_task"].cancel()
        stop.set()
        try:
            await asyncio.wait_for(feed_task, timeout=5)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            feed_task.cancel()
        store.audit("engine", "stopped")
        log.info("engine stopped")


def _get_cmd(cmd_q) -> dict[str, Any]:
    return cmd_q.get(timeout=0.5)
