"""Engine death must not strand a Qt thread or deliver events from an old book."""
import queue
import threading

import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication

from cryptobot.config import AppPaths
from cryptobot.core.events import LogEvent
from cryptobot.gui.controller import EngineController, _Bridge


class Channel(queue.Queue):
    closed = False

    def cancel_join_thread(self):
        pass

    def close(self):
        self.closed = True


class Process:
    pid = 123
    exitcode = None
    alive = False

    def start(self):
        self.alive = True

    def is_alive(self):
        return self.alive

    def join(self, timeout=None):
        pass

    def terminate(self):
        self.alive, self.exitcode = False, -15


class Context:
    Queue = Channel

    def Process(self, **kwargs):
        return Process()


def test_crash_restart_releases_channels_and_ignores_old_engine_events(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr("cryptobot.gui.controller.mp.get_context", lambda _: Context())
    controller = EngineController(AppPaths(tmp_path, tmp_path))
    states, logs, threads = [], [], []
    controller.engine_state.connect(states.append)
    controller.log_line.connect(lambda ev: (logs.append(ev.message), threads.append(QThread.currentThread())))

    def emit_in_worker(bridge, message):
        thread = threading.Thread(target=lambda: bridge.event.emit(bridge, LogEvent("INFO", "test", message, 1)))
        thread.start()
        thread.join(1)
        assert not thread.is_alive()

    try:
        controller.start("kraken", ["BTC/USD"])
        old_bridge, old_cmd, old_events = controller._bridge, controller._cmd_q, controller._evt_q
        emit_in_worker(old_bridge, "old")
        controller._proc.alive, controller._proc.exitcode = False, 1
        controller._check_process()
        assert old_bridge.wait(1000) and old_cmd.closed and old_events.closed
        assert controller._bridge is None and not controller.running
        controller._check_process()
        assert states.count("crashed") == 1
        controller.start("coinbase", ["ETH/USD"])
        emit_in_worker(controller._bridge, "current")
        app.processEvents()
        assert logs == ["current"]
        assert threads == [app.thread()]
    finally:
        controller.shutdown()
        controller._watchdog.stop()


def test_stopped_reader_can_detach_from_a_partial_ipc_frame():
    app = QApplication.instance() or QApplication([])
    entered, release = threading.Event(), threading.Event()

    class PartialFrame:
        def get(self, timeout):
            entered.set()
            release.wait(2)
            return LogEvent("INFO", "test", "stale", 1)

    bridge = _Bridge(PartialFrame())
    seen = []
    bridge.event.connect(lambda source, event: seen.append(event))
    bridge.start()
    try:
        assert entered.wait(1)
        bridge.stop()
        assert not bridge.wait(10)
        assert bridge._thread.daemon
    finally:
        release.set()
        assert bridge.wait(1000)
    app.processEvents()
    assert not seen
