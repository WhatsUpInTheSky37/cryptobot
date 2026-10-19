"""Owns the engine process and turns its events into Qt signals."""
from __future__ import annotations

import logging
import multiprocessing as mp
import queue

from PySide6.QtCore import QObject, QThread, Signal, QTimer

from cryptobot.config import AppPaths
from cryptobot.core.events import (
    AlertEvent,
    BarsEvent,
    BudgetEvent,
    ConnectionEvent,
    EWStatusEvent,
    EquityEvent,
    FillEvent,
    HeartbeatEvent,
    LogEvent,
    MarketInfoEvent,
    PositionsEvent,
    PulseEventRecord,
    PulseStatusEvent,
    SignalEvent,
    TickEvent,
    TraderStateEvent,
)
from cryptobot.engine.risk import RiskLimits
from cryptobot.engine.runner import engine_main
from cryptobot.strategy.config import StrategyConfig

log = logging.getLogger(__name__)


class _Bridge(QThread):
    event = Signal(object)

    def __init__(self, q) -> None:
        super().__init__()
        self._q = q
        self._running = True

    def run(self) -> None:
        while self._running:
            try:
                ev = self._q.get(timeout=0.2)
            except queue.Empty:
                continue
            except (EOFError, OSError):
                break
            self.event.emit(ev)

    def stop(self) -> None:
        self._running = False


class EngineController(QObject):
    connection = Signal(object)  # ConnectionEvent
    tick = Signal(object)  # TickEvent
    bars = Signal(object)  # BarsEvent
    heartbeat = Signal(object)  # HeartbeatEvent
    log_line = Signal(object)  # LogEvent
    engine_state = Signal(str)  # "running" | "stopped" | "crashed"
    trader_state = Signal(object)  # TraderStateEvent
    budget = Signal(object)  # BudgetEvent
    positions = Signal(object)  # PositionsEvent
    fill = Signal(object)  # FillEvent
    signal = Signal(object)  # SignalEvent
    alert = Signal(object)  # AlertEvent
    equity = Signal(object)  # EquityEvent
    market_info = Signal(object)  # MarketInfoEvent
    pulse_status = Signal(object)  # PulseStatusEvent
    pulse_event = Signal(object)  # PulseEventRecord
    ew_status = Signal(object)  # EWStatusEvent

    def __init__(self, paths: AppPaths) -> None:
        super().__init__()
        self._paths = paths
        self._ctx = mp.get_context("spawn")
        self._proc: mp.process.BaseProcess | None = None
        self._cmd_q = None
        self._evt_q = None
        self._bridge: _Bridge | None = None
        self.exchange: str = ""
        self.symbols: list[str] = []
        self._watchdog = QTimer(self)
        self._watchdog.setInterval(1000)
        self._watchdog.timeout.connect(self._check_process)
        self._watchdog.start()

    def _check_process(self):
        if self._proc is not None and not self._proc.is_alive() and self._proc.exitcode is not None:
            self.engine_state.emit("crashed")
            self._proc = None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.is_alive()

    def start(self, exchange: str, symbols: list[str]) -> None:
        if self.running:
            self.set_market(exchange, symbols)
            return
        self.exchange, self.symbols = exchange, list(symbols)
        self._cmd_q = self._ctx.Queue()
        self._evt_q = self._ctx.Queue()
        config = {
            "exchange": exchange,
            "symbols": list(symbols),
            "db_path": str(self._paths.db_path),
            "log_path": str(self._paths.engine_log),
        }
        self._proc = self._ctx.Process(target=engine_main, args=(self._cmd_q, self._evt_q, config), daemon=True, name="cryptobot-engine")
        self._proc.start()
        self._bridge = _Bridge(self._evt_q)
        self._bridge.event.connect(self._dispatch)
        self._bridge.start()
        self.engine_state.emit("running")
        log.info("engine process started pid=%s", self._proc.pid)

    def set_market(self, exchange: str, symbols: list[str]) -> None:
        self.exchange, self.symbols = exchange, list(symbols)
        if self._cmd_q is not None and self.running:
            self._cmd_q.put({"type": "set_market", "exchange": exchange, "symbols": list(symbols)})
        else:
            self.start(exchange, symbols)

    # -- trading commands ---------------------------------------------------

    def _cmd(self, **cmd) -> None:
        if self._cmd_q is not None and self.running:
            self._cmd_q.put(cmd)
        else:
            log.warning("engine not running; dropped command %s", cmd.get("type"))

    def start_trading(self, strategy: StrategyConfig, limits: RiskLimits, slippage_pct: float, reset_peak: bool = False, mode: str = "TEST", credentials: dict | None = None) -> None:
        cmd = dict(type="start_trading", mode=mode, exchange=self.exchange, symbols=list(self.symbols),
                   strategy=strategy.to_json(), limits=limits.model_dump(), slippage_pct=slippage_pct, reset_peak=reset_peak)
        if credentials:
            cmd["credentials"] = credentials  # goes over the local pipe to the engine; never logged or stored
        self._cmd(**cmd)

    def stop_trading(self) -> None:
        self._cmd(type="stop_trading")

    def emergency_stop(self) -> None:
        self._cmd(type="emergency_stop")

    def resume(self) -> None:
        self._cmd(type="resume")

    def set_limits(self, limits: RiskLimits) -> None:
        self._cmd(type="set_limits", limits=limits.model_dump())

    def pulse_start(self, config: dict, slippage_pct: float, exchange: str | None = None, symbols: list[str] | None = None) -> None:
        """Start Pulse on its own exchange/coins (paper, no key needed) or, with neither given, on the Market page's feed."""
        self._cmd(type="pulse_start", config=config, slippage_pct=slippage_pct, exchange=exchange, symbols=list(symbols) if symbols else None)

    def pulse_stop(self) -> None:
        self._cmd(type="pulse_stop")

    def pulse_train(self) -> None:
        self._cmd(type="pulse_train")

    def ew_start(self, config: dict, exchange: str = "binanceus") -> None:
        self._cmd(type="ew_start", config=config, exchange=exchange)

    def ew_stop(self) -> None:
        self._cmd(type="ew_stop")

    def shutdown(self) -> None:
        if self._bridge is not None:
            self._bridge.stop()
        if self._proc is not None and self._proc.is_alive():
            try:
                self._cmd_q.put({"type": "stop"})
            except Exception:  # noqa: BLE001
                pass
            self._proc.join(timeout=6)
            if self._proc.is_alive():
                log.warning("engine did not stop in time; terminating")
                self._proc.terminate()
                self._proc.join(timeout=2)
        if self._bridge is not None:
            self._bridge.wait(1000)
        self._proc = None
        self.engine_state.emit("stopped")

    def _dispatch(self, ev: object) -> None:
        if isinstance(ev, BarsEvent):
            self.bars.emit(ev)
        elif isinstance(ev, TickEvent):
            self.tick.emit(ev)
        elif isinstance(ev, HeartbeatEvent):
            self.heartbeat.emit(ev)
            if self._proc is not None and not self._proc.is_alive():
                self.engine_state.emit("crashed")
        elif isinstance(ev, ConnectionEvent):
            self.connection.emit(ev)
        elif isinstance(ev, LogEvent):
            self.log_line.emit(ev)
        elif isinstance(ev, TraderStateEvent):
            self.trader_state.emit(ev)
        elif isinstance(ev, BudgetEvent):
            self.budget.emit(ev)
        elif isinstance(ev, PositionsEvent):
            self.positions.emit(ev)
        elif isinstance(ev, FillEvent):
            self.fill.emit(ev)
        elif isinstance(ev, SignalEvent):
            self.signal.emit(ev)
        elif isinstance(ev, AlertEvent):
            self.alert.emit(ev)
        elif isinstance(ev, EquityEvent):
            self.equity.emit(ev)
        elif isinstance(ev, MarketInfoEvent):
            self.market_info.emit(ev)
        elif isinstance(ev, PulseStatusEvent):
            self.pulse_status.emit(ev)
        elif isinstance(ev, PulseEventRecord):
            self.pulse_event.emit(ev)
        elif isinstance(ev, EWStatusEvent):
            self.ew_status.emit(ev)

