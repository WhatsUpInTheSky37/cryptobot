"""Settings: exchange key, notifications, app behaviour, log viewer."""
from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from cryptobot import autostart, notify, secrets
from cryptobot.config import EXCHANGES
from cryptobot.db.store import Store
from cryptobot.gui.controller import EngineController
from cryptobot.gui.theme import GREEN, MUTED, RED
from cryptobot.gui.widgets.api_key_panel import ApiKeyPanel


class ApiKeyDialog(QDialog):
    def __init__(self, exchange_id: str, store, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Exchange API key")
        self.resize(640, 520)
        lay = QVBoxLayout(self)
        self.panel = ApiKeyPanel(exchange_id, store)
        self.panel.saved.connect(lambda _e: self.accept())
        lay.addWidget(self.panel)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        lay.addWidget(close)


class SettingsPage(QWidget):
    def __init__(self, ctrl: EngineController, store: Store, run_wizard) -> None:
        super().__init__()
        self.ctrl = ctrl
        self.store = store
        self._run_wizard = run_wizard
        self._exchange = store.get_setting("market.exchange", "kraken")

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(scroll.Shape.NoFrame)
        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(12)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)
        scroll.setWidget(inner)

        h = QLabel("Settings")
        h.setObjectName("h1")
        lay.addWidget(h)

        # -- exchange connection -------------------------------------------
        ex = QGroupBox("Exchange connection")
        ef = QFormLayout(ex)
        self.ex_label = QLabel("")
        ef.addRow("Exchange", self.ex_label)
        self.key_status = QLabel("")
        ef.addRow("API key", self.key_status)
        row = QHBoxLayout()
        self.add_key = QPushButton("Add / replace API key…")
        self.add_key.clicked.connect(self._add_key)
        self.rm_key = QPushButton("Remove key")
        self.rm_key.clicked.connect(self._remove_key)
        row.addWidget(self.add_key)
        row.addWidget(self.rm_key)
        row.addStretch(1)
        ef.addRow(row)
        ef.addRow(QLabel("Change the exchange itself on the Market screen or by running setup again."))
        lay.addWidget(ex)

        # -- notifications --------------------------------------------------
        nt = QGroupBox("Notifications")
        nf = QFormLayout(nt)
        self.desktop = QCheckBox("Desktop pop-ups for trades and alerts")
        self.desktop.setChecked(bool(store.get_setting("notify.desktop", True)))
        self.desktop.toggled.connect(lambda v: store.set_setting("notify.desktop", v))
        nf.addRow(self.desktop)
        self.discord = QLineEdit()
        self.discord.setEchoMode(QLineEdit.EchoMode.Password)
        self.discord.setPlaceholderText("https://discord.com/api/webhooks/…  (stored in keychain)")
        nf.addRow("Discord webhook", self.discord)
        self.tg_token = QLineEdit()
        self.tg_token.setEchoMode(QLineEdit.EchoMode.Password)
        self.tg_token.setPlaceholderText("Bot token from @BotFather (stored in keychain)")
        nf.addRow("Telegram bot token", self.tg_token)
        self.tg_chat = QLineEdit(str(store.get_setting("notify.telegram_chat", "") or ""))
        self.tg_chat.setPlaceholderText("Your chat id")
        nf.addRow("Telegram chat id", self.tg_chat)
        nrow = QHBoxLayout()
        save_n = QPushButton("Save")
        save_n.clicked.connect(self._save_notify)
        test_n = QPushButton("Send test message")
        test_n.clicked.connect(self._test_notify)
        nrow.addWidget(save_n)
        nrow.addWidget(test_n)
        nrow.addStretch(1)
        nf.addRow(nrow)
        self.notify_msg = QLabel("")
        self.notify_msg.setObjectName("muted")
        nf.addRow(self.notify_msg)
        lay.addWidget(nt)

        # -- app ------------------------------------------------------------
        ap = QGroupBox("Application")
        af = QFormLayout(ap)
        self.tray = QCheckBox("Keep running in the system tray when the window is closed")
        self.tray.setChecked(bool(store.get_setting("app.minimize_to_tray", True)))
        self.tray.toggled.connect(lambda v: store.set_setting("app.minimize_to_tray", v))
        af.addRow(self.tray)
        self.auto = QCheckBox("Start CryptoBot when the computer starts")
        self.auto.setChecked(autostart.is_enabled())
        self.auto.toggled.connect(self._toggle_autostart)
        af.addRow(self.auto)
        self.auto_msg = QLabel("")
        self.auto_msg.setObjectName("muted")
        af.addRow(self.auto_msg)
        wiz = QPushButton("Run setup wizard again…")
        wiz.clicked.connect(self._run_wizard)
        af.addRow(wiz)
        lay.addWidget(ap)

        # -- log ------------------------------------------------------------
        lg = QGroupBox("Engine log")
        ll = QVBoxLayout(lg)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        self.log_view.setMinimumHeight(220)
        ll.addWidget(self.log_view)
        lay.addWidget(lg)
        lay.addStretch(1)

        ctrl.connection.connect(self._on_connection)
        self._refresh_key_status()

    # ------------------------------------------------------------------

    def _on_connection(self, ev) -> None:
        if ev.exchange != self._exchange:
            self._exchange = ev.exchange
            self._refresh_key_status()

    def _refresh_key_status(self) -> None:
        self.ex_label.setText(EXCHANGES.get(self._exchange, {}).get("name", self._exchange))
        has = secrets.has_api_key(self._exchange)
        self.key_status.setText("Saved in keychain (never displayed)" if has else "None — TEST mode works without one; BUY mode will need it")
        self.key_status.setStyleSheet(f"color: {GREEN if has else MUTED};")
        self.rm_key.setEnabled(has)

    def _add_key(self) -> None:
        ApiKeyDialog(self._exchange, self.store, self).exec()
        self._refresh_key_status()

    def _remove_key(self) -> None:
        secrets.delete_api_key(self._exchange)
        self.store.audit("settings", "API key removed", exchange=self._exchange)
        self._refresh_key_status()

    def _save_notify(self) -> None:
        if self.discord.text().strip():
            secrets.save_secret("discord_webhook", self.discord.text().strip())
            self.discord.clear()
        if self.tg_token.text().strip():
            secrets.save_secret("telegram_token", self.tg_token.text().strip())
            self.tg_token.clear()
        self.store.set_setting("notify.telegram_chat", self.tg_chat.text().strip())
        self.notify_msg.setText("Saved. Secrets went to the keychain and are not shown again.")

    def _test_notify(self) -> None:
        sent = []
        wh = secrets.load_secret("discord_webhook")
        if wh:
            notify.send_async(notify.send_discord, wh, "CryptoBot test message")
            sent.append("Discord")
        tok, chat = secrets.load_secret("telegram_token"), self.store.get_setting("notify.telegram_chat", "")
        if tok and chat:
            notify.send_async(notify.send_telegram, tok, chat, "CryptoBot test message")
            sent.append("Telegram")
        self.notify_msg.setText(f"Sent to {', '.join(sent)}." if sent else "Nothing configured yet. Save a webhook or token first.")

    def _toggle_autostart(self, enabled: bool) -> None:
        try:
            self.auto_msg.setText(autostart.set_enabled(enabled))
        except Exception as e:  # noqa: BLE001
            self.auto_msg.setStyleSheet(f"color: {RED};")
            self.auto_msg.setText(f"Could not change start-up setting: {e}")
