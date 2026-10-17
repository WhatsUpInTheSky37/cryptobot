"""The Pulse screen renders every state without a live engine (offscreen Qt)."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# Qt needs libEGL/libGL on the host; without them the import fails with ImportError (not ModuleNotFoundError), which
# pytest >= 8.2 no longer turns into a skip unless asked. Skip rather than error on such hosts.
pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

from PySide6.QtCore import QObject, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from cryptobot.core.events import PulseEventRecord, PulseModeStatus, PulseSnapshot, PulseStatusEvent  # noqa: E402
from cryptobot.db.store import Store  # noqa: E402
from cryptobot.gui.pages.pulse import PulsePage  # noqa: E402
from cryptobot.pulse.config import PulseConfig  # noqa: E402
from cryptobot.pulse.health import INSUFFICIENT  # noqa: E402


class FakeCtrl(QObject):
    pulse_status = Signal(object)
    pulse_event = Signal(object)

    def __init__(self):
        super().__init__()
        self.cmds = []

    def pulse_start(self, cfg, slip, exchange=None, symbols=None):
        self.cmds.append(("start", cfg, slip, exchange, symbols))

    def pulse_stop(self):
        self.cmds.append(("stop",))

    def pulse_train(self):
        self.cmds.append(("train",))


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def mode(key, label, **kw):
    base = dict(key=key, label=label, budget=100.0, equity=101.0, cash=50.0, open_positions=1, realized_today=0.5, unrealized=0.5, open_risk_pct=0.25, trades=3, wins=2, losses=1,
                net_pnl=1.0, gross_profit=2.0, gross_loss=1.0, consecutive_losses=0, paused_until_ms=0, halted=False, message="", positions=[{"symbol": "ETH/USDT", "units": 0.01, "entry_px": 3000.0, "stop": 2990.0, "target": 3020.0}],
                accepted=1, rejected=4)
    base.update(kw)
    return PulseModeStatus(**base)


def test_pulse_page_upgrades_old_health_defaults(app):
    store = Store(":memory:")
    old = PulseConfig().model_dump()
    old["health"].update({"stale_trade_s": 90, "min_trades_per_min": 2.0})
    store.set_setting("pulse.config", old)
    page = PulsePage(FakeCtrl(), store)
    assert page.stale.value() == PulseConfig().health.stale_trade_s and page.min_tpm.value() == PulseConfig().health.min_trades_per_min
    custom = PulseConfig().model_dump()
    custom["health"].update({"stale_trade_s": 120, "min_trades_per_min": 1.0})
    store.set_setting("pulse.config", custom)
    assert PulsePage(FakeCtrl(), store).stale.value() == 120  # a user's own choice is kept


def test_pulse_page_renders_all_states(app):
    store = Store(":memory:")
    ctrl = FakeCtrl()
    page = PulsePage(ctrl, store)
    page.start()
    assert ctrl.cmds[0][0] == "start" and ctrl.cmds[0][1]["budget"] == page.cfg.budget and len(ctrl.cmds[0][1]["modes"]) == 3
    assert ctrl.cmds[0][3:] == (None, None)  # default: the Market page's feed
    page.exchange.setCurrentIndex(page.exchange.findData("coinbase"))
    assert "BTC/USD" in page.symbols.text()
    page.symbols.setText("btc/usd, eth/usd")
    page.start()
    assert ctrl.cmds[-1][3:] == ("coinbase", ["BTC/USD", "ETH/USD"])
    assert store.get_setting("pulse.exchange") == "coinbase" and store.get_setting("pulse.symbols") == ["BTC/USD", "ETH/USD"]
    snaps = [
        PulseSnapshot("ETH/USDT", 1, "excluded", 0, 0, 0, 0, 0, 0, 0, 0.0, 3000.0, 0.0, 0.1, "", False, "stale_trades", "last trade 120s ago", "range"),
        PulseSnapshot("BTC/USDT", 1, "prepared", 88.0, 20.0, 90, 90, 90, 90, 90, 0.5, 80_000.0, 0.01, 0.2, "prepared", True, "ok", "", "trend_up",
                      {k: 90.0 for k in ("f", "a", "w", "r", "c", "acc", "vpr", "sx", "vx")}, {k: 10.0 for k in ("f", "a", "w", "r", "c", "acc", "vpr", "sx", "vx")},
                      {"ev_pct": 0.05, "target_pct": 0.5, "stop_pct": 0.2, "cost_pct": 0.3, "fee_pct": 0.2, "spread_pct": 0.02, "slippage_pct": 0.08, "evidence": "p=80% from 40", "break_even_p": 0.6, "rr": 1.2, "verdict": "ok"},
                      {"ev_pct": None, "reason": "INSUFFICIENT HISTORICAL EVIDENCE"}, 79_999.0, 80_001.0, 120.0, 42),
    ]
    st = PulseStatusEvent(True, 300.0, 303.0, 1, 10, 4, 0.5, 0.7, 0.01, 3, 1.0, 0, 0, "Running.", snaps, 0.2, 0.5, 0.3, True,
                          [mode("A", "Conservative"), mode("B", "Balanced", halted=True), mode("C", "Experimental", paused_until_ms=9_999_999_999_999, consecutive_losses=4)],
                          {"events": 10, "pending": 6, "resolved": 4, "effective_long": 2, "hit_rate_long": 0.5, "min_similar": 30}, {"backend": "logit", "usable": False, "note": "", "min_events": 300}, "1/2 coins with valid data; ETH/USDT: INSUFFICIENT DATA (last trade 120s ago)")
    ctrl.pulse_status.emit(st)
    eth = page.cards["ETH/USDT"]
    assert eth.health.text() == INSUFFICIENT and "last trade" in eth.ev.text() and eth.prs.text() == "PRS —"
    btc = page.cards["BTC/USDT"]
    assert btc.health.text() == "DATA OK" and "Bullish 88" in btc.prs.text() and "Expected net profit after costs: +0.050%" in btc.ev.text()
    assert "daily loss limit" in page.mode_cards["B"].line3.text() and "paused" in page.mode_cards["C"].line3.text()
    assert "INSUFFICIENT" in page.health_line.text() and page.stop_btn.isEnabled()
    # a live signal arrives, then resolves
    ctrl.pulse_event.emit(PulseEventRecord(7, 1_760_000_000_000, "BTC/USDT", "long", 88.0, "signal", "EV +0.05%", "pending", None, None, 40, 0.8, 0.05, "trend_up", None, None, {"A": (True, "ok"), "B": (False, "weak")}))
    assert page.events.rowCount() == 1 and page.events.item(0, 10).text() == "A:✓ B:✗"
    ctrl.pulse_event.emit(PulseEventRecord(7, 1_760_000_000_000, "BTC/USDT", "long", 0, "", "", "target", 0.6, -0.1, 0, 0, 0, "", 0.3, "target"))
    assert page.events.item(0, 6).text() == "target" and page.events.item(0, 7).text() == "+0.30%"
    # analysis tabs render from an empty and a populated store
    page.refresh_analysis()
    assert page.perf_table.rowCount() == 3
    eid = store.insert_pulse_event(ts_ms=1_760_000_000_000, exchange="binanceus", symbol="BTC/USDT", direction="long", prs=88, f=90, a=90, w=90, r=90, c=90, imbalance=0.5, vol_pct=0.2, spread_pct=0.01, mid=80_000,
                                   target_pct=0.5, stop_pct=0.2, cost_pct=0.3, status="signal", outcome="target", first_hit="target", net_pct=0.2, mfe_pct=0.6, mae_pct=-0.1, regime="trend_up", evidence="sufficient",
                                   similar_p=0.8, similar_eff=40, ev_pct=0.05, features_json='{"raw_long": {"f": 0.9}}')
    store.insert_decision(event_id=eid, ts_ms=1, mode="A", symbol="BTC/USDT", direction="long", accepted=False, code="ev_below_margin", reason="EV too low")
    store.insert_trade(mode="PULSE-B", exchange="binanceus", symbol="BTC/USDT", side="sell", ts_ms=2, price=1, amount=1, fee=0.1, pnl=1.5, reason="pulse B target")
    store.add_equity("PULSE-B", 2, 101.5, 101.5, 0)
    page.tabs.setCurrentIndex(1)
    assert page.perf_table.item(1, 3).text() == "1" and page.coin_table.rowCount() == 1 and page.regime_table.rowCount() == 1
    page.tabs.setCurrentIndex(2)
    assert page.rej_table.rowCount() == 1 and page.rej_table.item(0, 1).text() == "Expected value below margin"
    page.tabs.setCurrentIndex(3)
    assert page.events.rowCount() == 1
    page.events.selectRow(0)
    text = page.detail.toPlainText()
    assert "Pressure Release Score 88" in text and "Mode A: rejected" in text and "hypothetical net return after costs: +0.200%" in text and "fees 0.000%" in text
    page.stop()
    assert ctrl.cmds[-1] == ("stop",)
