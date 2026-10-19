"""One owner for trader state; synchronous exchange I/O never blocks the feed."""
from __future__ import annotations

import asyncio
import itertools
import logging
import queue
import threading
from concurrent.futures import Future
from typing import Any, Callable

log = logging.getLogger(__name__)


class TraderWorker:
    def __init__(self, factory: Callable[[], Any]) -> None:
        # Cap market data below, while retaining room for emergency commands.
        self._queue: queue.PriorityQueue = queue.PriorityQueue()
        self._sequence = itertools.count()
        self.ready: Future = Future()
        self.trader = None
        self._overloaded = False
        self._thread = threading.Thread(target=self._run, args=(factory,), name="trader-orders", daemon=True)
        self._thread.start()

    async def wait_ready(self):
        await asyncio.wrap_future(self.ready)
        return self

    @property
    def symbols(self):
        return list(self.trader.symbols)

    @property
    def has_exposure(self):
        return any(p.open for p in list(self.trader.ledger.positions.values())) or bool(self.trader._pending_orders(include_protection=True))

    def submit(self, method: str, *args, priority: int = 0) -> Future:
        result: Future = Future()
        if method in ("emergency_stop", "stop") and self.trader:
            interrupt = getattr(getattr(self.trader, "executor", None), "interrupt", None)
            if interrupt:
                interrupt.set()
        self._queue.put_nowait((priority, next(self._sequence), method, args, result))
        return result

    def on_event(self, event) -> None:
        try:
            if self._queue.qsize() >= 20_000:
                raise queue.Full
            self.submit("on_event", event, priority=10)
        except queue.Full:
            # Do not continue trading on an incomplete event stream.
            interrupt = getattr(getattr(self.trader, "executor", None), "interrupt", None)
            if interrupt:
                interrupt.set()
            log.critical("trader input overflow: entries must remain paused")
            self._overloaded = True

    def stop(self, reason="stopped by user"):
        return self.submit("stop", reason) if hasattr(self.trader, "ledger") else self.submit("stop")

    def emergency_stop(self):
        return self.submit("emergency_stop", priority=-10)

    def resume(self):
        return self.submit("resume")

    def set_limits(self, limits):
        return self.submit("set_limits", limits)

    async def close(self):
        result = self.submit("shutdown", priority=-20)
        await asyncio.wrap_future(result)

    def _run(self, factory):
        try:
            self.trader = factory()
            self.trader.start()
            self.ready.set_result(True)
        except BaseException as error:
            self.ready.set_exception(error)
            return
        while True:
            _, _, method, args, result = self._queue.get()
            try:
                if method == "shutdown":
                    self.trader.stop("engine shutting down; exchange protection must remain in force") if hasattr(self.trader, "ledger") else self.trader.stop()
                    result.set_result(None)
                    return
                if self._overloaded and hasattr(self.trader, "ledger"):
                    self.trader.status = "reconcile_required"
                    self.trader.connected = False
                elif self._overloaded:
                    self.trader.connected = False
                    self.trader.running = False
                value = getattr(self.trader, method)(*args)
                result.set_result(value)
            except Exception as error:
                log.exception("trader command failed: %s", method)
                if hasattr(self.trader, "ledger"):
                    self.trader.status = "reconcile_required"
                    self.trader._persist()
                from cryptobot.core.events import AlertEvent
                import time
                self.trader.emit(AlertEvent("bad", "Engine command failed", f"{method}: {error}", int(time.time() * 1000)))
                result.set_exception(error)
