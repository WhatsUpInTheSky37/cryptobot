"""GUI entry point."""
from __future__ import annotations

import logging
import multiprocessing as mp
import os
import sys
from logging.handlers import RotatingFileHandler

from cryptobot.config import DEFAULT_EXCHANGE, EXCHANGES, AppPaths
from cryptobot.db.store import Store


def main() -> None:
    mp.freeze_support()
    paths = AppPaths.default()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[RotatingFileHandler(paths.gui_log, maxBytes=2_000_000, backupCount=3), logging.StreamHandler()],
    )

    from PySide6.QtWidgets import QApplication

    from cryptobot.gui.controller import EngineController
    from cryptobot.gui.main_window import MainWindow
    from cryptobot.gui.theme import apply_theme

    app = QApplication(sys.argv)
    app.setApplicationName("CryptoBot")
    app.setOrganizationName("CryptoBot")
    apply_theme(app)

    from PySide6.QtCore import QTimer

    from cryptobot.gui.pages.coins import selected_symbols
    from cryptobot.gui.wizard import SetupWizard

    app.setQuitOnLastWindowClosed(False)
    store = Store(paths.db_path)
    ctrl = EngineController(paths)

    def run_wizard() -> None:
        wiz = SetupWizard(store, win)
        if wiz.exec():
            ctrl.set_market(wiz.result_exchange, wiz.result_symbols)
            QTimer.singleShot(1500, win.pages["dashboard"].start_bot)

    win = MainWindow(ctrl, store, run_wizard)
    win.show()

    exchange = store.get_setting("market.exchange", DEFAULT_EXCHANGE)
    if exchange not in EXCHANGES:
        exchange = DEFAULT_EXCHANGE
    ctrl.start(exchange, selected_symbols(store, exchange))
    first_run = not store.get_setting("setup.completed", False)
    if os.environ.get("CRYPTOBOT_SCREENSHOT_BUYDIALOG"):
        from cryptobot import secrets
        from cryptobot.engine.modes import buy_gate
        from cryptobot.gui.widgets.mode_dialog import ModeSwitchDialog

        ok, reasons, stats = buy_gate(store, int(__import__("time").time() * 1000), 100.0, secrets.has_api_key(exchange), None)
        dlg = ModeSwitchDialog(EXCHANGES[exchange]["name"], stats, reasons, 100.0, win)
        dlg.show()
        win = dlg
    elif os.environ.get("CRYPTOBOT_SCREENSHOT_WIZARD"):
        wiz = SetupWizard(store, win)
        wiz.show()
        win = wiz  # the screenshot hook below grabs the wizard
    elif first_run:
        QTimer.singleShot(300, run_wizard)
    elif (store.get_setting("trader.running", False) and not os.environ.get("CRYPTOBOT_NO_AUTOSTART")) or os.environ.get("CRYPTOBOT_SCREENSHOT_START"):
        QTimer.singleShot(1500, win.pages["dashboard"].start_bot)
    if (store.get_setting("pulse.running", False) and not os.environ.get("CRYPTOBOT_NO_AUTOSTART")) or os.environ.get("CRYPTOBOT_SCREENSHOT_PULSE"):
        QTimer.singleShot(2500, win.pages["pulse"].start)

    # Developer aid: CRYPTOBOT_SCREENSHOT=/path/out.png [CRYPTOBOT_SCREENSHOT_AFTER=seconds]
    # grabs the window after a delay and exits. Used to verify the GUI without a human.
    shot = os.environ.get("CRYPTOBOT_SCREENSHOT")
    if shot:
        from PySide6.QtCore import QTimer

        def _grab() -> None:
            win.grab().save(shot)
            logging.getLogger("gui").info("screenshot saved to %s", shot)
            ctrl.shutdown()
            app.quit()

        page = os.environ.get("CRYPTOBOT_SCREENSHOT_PAGE")
        if page and hasattr(win, "nav"):
            keys = [k for _l, k in __import__("cryptobot.gui.main_window", fromlist=["PAGES"]).PAGES]
            win.nav.button(keys.index(page)).click()
            if page == "strategy" and os.environ.get("CRYPTOBOT_SCREENSHOT_RUN"):
                win.pages["strategy"].run()
        tf = os.environ.get("CRYPTOBOT_SCREENSHOT_TF")
        if tf and hasattr(win, "pages"):
            win.pages["market"].tf_group.button(int(tf)).click()
        QTimer.singleShot(int(float(os.environ.get("CRYPTOBOT_SCREENSHOT_AFTER", "20")) * 1000), _grab)

    rc = app.exec()
    ctrl.shutdown()
    sys.exit(rc)


if __name__ == "__main__":
    main()
