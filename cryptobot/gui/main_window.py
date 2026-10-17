"""Main window: navigation rail, top bar with the mode badge, and the page stack."""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from cryptobot import __version__
from cryptobot.config import EXCHANGES
from cryptobot import notify, secrets
from cryptobot.core.events import AlertEvent, ConnectionEvent, FillEvent, LogEvent, TraderStateEvent
from cryptobot.core.models import Mode
from cryptobot.db.store import Store
from cryptobot.gui.controller import EngineController
from cryptobot.gui.pages.market import MarketPage
from cryptobot.gui.pages.coins import CoinsPage
from cryptobot.gui.pages.dashboard import DashboardPage
from cryptobot.gui.pages.earlywarning import EarlyWarningPage
from cryptobot.gui.pages.help import HelpPage
from cryptobot.gui.pages.history import HistoryPage
from cryptobot.gui.pages.placeholder import PlaceholderPage
from cryptobot.gui.pages.pulse import PulsePage
from cryptobot.gui.pages.risk import RiskPage
from cryptobot.gui.pages.settings import SettingsPage
from cryptobot.gui.tray import Tray
from cryptobot.gui.pages.strategy import StrategyPage
from cryptobot.gui.theme import AMBER, GREEN, GREY, RED
from cryptobot.gui.widgets.mode_badge import ModeBadge

PAGES = [
    ("Dashboard", "dashboard"),
    ("Market", "market"),
    ("Coins", "coins"),
    ("Strategy", "strategy"),
    ("Pulse", "pulse"),
    ("Early\nWarning", "earlywarning"),
    ("Risk", "risk"),
    ("History", "history"),
    ("Settings", "settings"),
    ("Help", "help"),
]


class MainWindow(QMainWindow):
    def __init__(self, ctrl: EngineController, store: Store, run_wizard=None) -> None:
        super().__init__()
        self.ctrl = ctrl
        self.store = store
        self._run_wizard = run_wizard or (lambda: None)
        self._really_quit = False
        self._tray_hint_shown = False
        self.setWindowTitle(f"CryptoBot {__version__}")
        self.resize(1280, 820)
        self.setMinimumSize(960, 640)

        central = QWidget()
        outer = QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.setCentralWidget(central)

        # -- nav rail -------------------------------------------------------
        rail = QFrame()
        rail.setObjectName("navRail")
        rail.setFixedWidth(96)
        rl = QVBoxLayout(rail)
        rl.setContentsMargins(8, 12, 8, 12)
        rl.setSpacing(4)
        logo = QLabel("◆")
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo.setStyleSheet("font-size: 22px; color: #3b82f6; padding-bottom: 8px;")
        rl.addWidget(logo)
        self.nav = QButtonGroup(self)
        for i, (label, _key) in enumerate(PAGES):
            b = QPushButton(label)
            b.setObjectName("nav")
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            self.nav.addButton(b, i)
            rl.addWidget(b)
        rl.addStretch(1)
        outer.addWidget(rail)

        # -- right side -----------------------------------------------------
        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)

        top = QFrame()
        top.setObjectName("topBar")
        top.setFixedHeight(56)
        tl = QHBoxLayout(top)
        tl.setContentsMargins(16, 8, 16, 8)
        tl.setSpacing(16)
        self.mode_badge = ModeBadge()
        tl.addWidget(self.mode_badge)
        self.exchange_label = QLabel("")
        self.exchange_label.setObjectName("muted")
        tl.addWidget(self.exchange_label)
        self.conn_label = QLabel("● Starting…")
        self.conn_label.setStyleSheet(f"color: {AMBER};")
        tl.addWidget(self.conn_label)
        tl.addStretch(1)
        self.clock = QLabel("")
        self.clock.setObjectName("muted")
        tl.addWidget(self.clock)
        right.addWidget(top)

        self.stack = QStackedWidget()
        self.pages: dict[str, QWidget] = {}
        self.pages["dashboard"] = DashboardPage(ctrl, store)
        self.pages["market"] = MarketPage(ctrl, store)
        self.pages["coins"] = CoinsPage(ctrl, store)
        self.pages["strategy"] = StrategyPage(store)
        self.pages["pulse"] = PulsePage(ctrl, store)
        self.pages["earlywarning"] = EarlyWarningPage(ctrl, store)
        self.pages["risk"] = RiskPage(ctrl, store)
        self.pages["history"] = HistoryPage(ctrl, store)
        self.pages["settings"] = SettingsPage(ctrl, store, self._run_wizard)
        self.log_view = self.pages["settings"].log_view
        self.pages["help"] = HelpPage()
        for _label, key in PAGES:
            self.stack.addWidget(self.pages[key])
        right.addWidget(self.stack, 1)
        outer.addLayout(right, 1)

        self.nav.idClicked.connect(self.stack.setCurrentIndex)
        self.nav.button(0).setChecked(True)
        self.stack.setCurrentIndex(0)

        ctrl.connection.connect(self._on_connection)
        ctrl.trader_state.connect(self._on_trader_state)
        ctrl.fill.connect(self._on_fill)
        ctrl.alert.connect(self._on_alert)
        ctrl.log_line.connect(self._on_log)

        self._build_menu()
        self.tray = Tray(self.show_window, self.quit_app, self)
        self.tray.enabled_popups = bool(store.get_setting("notify.desktop", True))
        self.tray.show()
        ctrl.engine_state.connect(self._on_engine_state)

        self._clock_timer = QTimer(self)
        self._clock_timer.timeout.connect(self._tick_clock)
        self._clock_timer.start(1000)
        self._tick_clock()
        self.set_mode(Mode.STOPPED)

    def _build_menu(self) -> None:
        from PySide6.QtGui import QAction, QKeySequence

        bar = self.menuBar()
        bar.setNativeMenuBar(True)
        file_menu = bar.addMenu("File")
        quit_act = QAction("Quit CryptoBot (stops the bot)", self)
        quit_act.setShortcut(QKeySequence.StandardKey.Quit)
        quit_act.triggered.connect(self.quit_app)
        file_menu.addAction(quit_act)
        help_menu = bar.addMenu("Help")
        guide = QAction("User guide", self)
        guide.setShortcut(QKeySequence.StandardKey.HelpContents)
        guide.triggered.connect(lambda: self._goto("help"))
        help_menu.addAction(guide)
        for title in ("Reading a backtest", "Setting up an API key", "What to do if…"):
            a = QAction(title, self)
            a.triggered.connect(lambda _c=False, t=title: (self._goto("help"), self.pages["help"].show_section(t)))
            help_menu.addAction(a)
        help_menu.addSeparator()
        about = QAction("About CryptoBot", self)
        about.triggered.connect(self._about)
        help_menu.addAction(about)

    def _goto(self, key: str) -> None:
        idx = [k for _l, k in PAGES].index(key)
        self.nav.button(idx).setChecked(True)
        self.stack.setCurrentIndex(idx)
        self.show_window()

    def _about(self) -> None:
        from PySide6.QtWidgets import QMessageBox

        QMessageBox.about(
            self,
            "About CryptoBot",
            f"<b>CryptoBot {__version__}</b><br>Budget-capped automated crypto trading with TEST and BUY modes.<br><br>"
            "This software can lose money. It never promises profit. Run TEST mode first and read the numbers.",
        )

    # -- slots ---------------------------------------------------------------

    def set_mode(self, mode: Mode) -> None:
        self.mode_badge.set_mode(mode)

    def _on_connection(self, ev: ConnectionEvent) -> None:
        name = EXCHANGES.get(ev.exchange, {}).get("name", ev.exchange)
        self.exchange_label.setText(name)
        color, text = {
            "connecting": (AMBER, "Connecting"),
            "reconnecting": (AMBER, "Reconnecting"),
            "connected": (GREEN, "Connected"),
            "blocked": (RED, "Blocked"),
            "error": (RED, "Error"),
            "stopped": (GREY, "Disconnected"),
        }[ev.status]
        self.conn_label.setStyleSheet(f"color: {color};")
        self.conn_label.setText(f"● {text}")
        self.conn_label.setToolTip(ev.message)

    def _on_log(self, ev: LogEvent) -> None:
        t = datetime.fromtimestamp(ev.ts_ms / 1000).strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"{t} {ev.level:<7} {ev.logger}: {ev.message}")

    def _on_engine_state(self, state: str) -> None:
        if state == "crashed":
            self.conn_label.setStyleSheet(f"color: {RED};")
            self.conn_label.setText("● Engine stopped unexpectedly")

    def _tick_clock(self) -> None:
        self.clock.setText(datetime.now().strftime("%a %b %d  %H:%M:%S  %Z").strip())

    def _on_trader_state(self, ev: TraderStateEvent) -> None:
        mode = Mode(ev.mode)
        self.set_mode(mode)
        self.tray.set_mode(mode, ev.status.replace("_", " ") if ev.status != "stopped" else "")

    def _outbound(self, text: str) -> None:
        wh = secrets.load_secret("discord_webhook")
        if wh:
            notify.send_async(notify.send_discord, wh, text)
        tok, chat = secrets.load_secret("telegram_token"), self.store.get_setting("notify.telegram_chat", "")
        if tok and chat:
            notify.send_async(notify.send_telegram, tok, chat, text)

    def _on_fill(self, ev: FillEvent) -> None:
        text = notify.format_fill(ev.mode, ev.symbol, ev.side, ev.amount, ev.price, ev.reason, ev.pnl)
        self.tray.enabled_popups = bool(self.store.get_setting("notify.desktop", True))
        self.tray.notify(f"{ev.mode} {ev.side.upper()} {ev.symbol}", text)
        self._outbound(text)

    def _on_alert(self, ev: AlertEvent) -> None:
        self.tray.enabled_popups = bool(self.store.get_setting("notify.desktop", True))
        self.tray.notify(ev.title, ev.message, bad=ev.level == "bad")
        self._outbound(f"⚠ {ev.title}: {ev.message}")

    def show_window(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def quit_app(self) -> None:
        self._really_quit = True
        self.close()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        to_tray = bool(self.store.get_setting("app.minimize_to_tray", True)) and self.tray.isSystemTrayAvailable()
        if to_tray and not self._really_quit:
            event.ignore()
            self.hide()
            if not self._tray_hint_shown:
                self._tray_hint_shown = True
                self.tray.showMessage("CryptoBot is still running", "The bot keeps trading in the background. Use the tray icon to show the window or quit.", QSystemTrayIcon.MessageIcon.Information, 5000)
            return
        self.tray.hide()
        self.ctrl.shutdown()
        from PySide6.QtWidgets import QApplication

        QApplication.instance().quit()
        super().closeEvent(event)
