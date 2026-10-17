"""The Early Warning screen renders live, explorer, paper and research states offscreen."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import QObject, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from cryptobot.config import AppPaths  # noqa: E402
from cryptobot.core.events import EWCandidateView, EWStatusEvent  # noqa: E402
from cryptobot.db.store import Store  # noqa: E402
from cryptobot.earlywarning.cli import research  # noqa: E402
from cryptobot.earlywarning.config import COMPONENTS, EWConfig  # noqa: E402
from cryptobot.earlywarning.store import EWStore  # noqa: E402
from cryptobot.gui.pages.earlywarning import EarlyWarningPage  # noqa: E402


class FakeCtrl(QObject):
    ew_status = Signal(object)

    def __init__(self):
        super().__init__()
        self.cmds = []

    def ew_start(self, cfg, exchange):
        self.cmds.append(("start", cfg, exchange))

    def ew_stop(self):
        self.cmds.append(("stop",))


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_early_warning_page_renders_everything(app, tmp_path):
    paths = AppPaths(tmp_path / "data", tmp_path / "logs")
    paths.data_dir.mkdir(parents=True)
    ew = EWStore(paths.data_dir / "earlywarning.db")
    cfg = EWConfig(days=15)
    cfg.seed_symbols = ["POND/USD"]
    research(ew, cfg, demo=True, progress=lambda m: None)
    ew.set("config", cfg.model_dump())
    ctrl = FakeCtrl()
    page = EarlyWarningPage(ctrl, Store(":memory:"), paths)
    page.start()
    assert ctrl.cmds[0][0] == "start" and ctrl.cmds[0][2] == "binanceus"
    ok = {c: 80.0 for c in COMPONENTS} | {"status": "ok"}
    cands = [
        EWCandidateView("POND/USD", "ACCUMULATION CONFIRMED", 82.0, ok, "ok", 1.23, {5: 0.1, 15: 0.3, 30: 0.5, 60: 0.9}, 2.4, 1.8, 2.1, {"n": 120, "p": None, "note": "not validated"}, None,
                        "TARGET NOT YET RELIABLE", None, None, [], 0.08, 2500.0, ["volume 2.4x its 24h rate"], 1_750_000_000_000),
        EWCandidateView("XYZ/USD", "NO DATA", None, {"status": "INSUFFICIENT DATA", "detail": "12/120 observations"}, "ok", 0.5, {5: None, 15: None, 30: None, 60: None}, None, None, None, {"n": 0, "p": None, "note": "not validated"}, None,
                        "TARGET NOT YET RELIABLE", None, None, ["thin: cannot size a position"], 0.9, 3.0, [], 0),
    ]
    ev = EWStatusEvent(True, True, "Streaming 2 of 40 eligible markets", {"eligible": 40, "excluded": 30, "selected": ["POND/USD", "XYZ/USD"], "last_scan_ms": 1_750_000_000_000, "next_scan_ms": 1_750_000_300_000, "top": [("POND/USD", 3.1, ["volume 2.4x"], ""), ("ABC/USD", 0.0, [], "spread 1.2% above 0.6%")]},
                       cands, {"budget": 100.0, "equity": 101.0, "cash": 60.0, "unrealized": 1.0, "realized_today": 0.0, "trades": 2, "wins": 1, "losses": 1, "net_pnl": 1.0, "fees": 0.2, "consecutive_losses": 1, "paused_until_ms": 0, "halted": False,
                               "positions": [{"symbol": "POND/USD", "units": 30.0, "entry_px": 1.2, "stop": 1.17, "t1": 1.25}], "rejections": {"target_not_reliable": 9}, "entry_method": "A", "exit_method": "partial"},
                       {"signals": 5, "pending": 2, "resolved": 3, "true_positives": 1, "false_positives": 2, "precision": 1 / 3, "missed_surges": 1, "recall": 0.5}, {"has_report": True, "demo": True},
                       [{"ts_ms": 1_750_000_000_000, "symbol": "POND/USD", "from_state": "QUIET", "to_state": "ACCUMULATION CONFIRMED", "eas": 82.0, "note": "ok"}])
    ctrl.ew_status.emit(ev)
    assert page.cand.rowCount() == 2 and page.cand.item(1, 2).text() == "INSUFFICIENT DATA" and page.cand.item(0, 13).text() == "TARGET NOT YET RELIABLE"
    assert "SYNTHETIC" in page.status_line.text() and "precision 33%" in page.scan_line.text() and page.trans.rowCount() == 1
    page.cand.selectRow(0)
    assert "Early Accumulation Score 82.0" in page.cand_detail.toPlainText()
    page.cand.selectRow(1)
    assert "INSUFFICIENT DATA" in page.cand_detail.toPlainText()
    assert "target_not_reliable 9" in page.p_rej.text() and "A / partial" in page.p_method.text()
    # explorer: events from the demo research, with timeline and hypothetical outcome
    page.tabs.setCurrentIndex(1)
    assert page.ex_symbol.count() == 1 and page.ev_table.rowCount() > 0
    page.ev_table.selectRow(0)
    text = page.ev_detail.toPlainText()
    assert "surge starting" in text and "Would the strategy have entered" in text and "Minutes before start" in text
    assert page.ev_eas.xData is not None and len(page.ev_eas.xData) > 0
    page.tabs.setCurrentIndex(2)
    page.tabs.setCurrentIndex(3)
    assert "Early Warning — research report" in page.r_report.toPlainText()
    page.stop()
    assert ctrl.cmds[-1] == ("stop",)
