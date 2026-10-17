"""Enter, test, and save an exchange API key. Keys go to the OS keychain and are never shown again."""
from __future__ import annotations

import urllib.request

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import QCheckBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from cryptobot import secrets
from cryptobot.config import EXCHANGES
from cryptobot.data.account import ConnectionReport, check_connection
from cryptobot.gui.theme import AMBER, GREEN, MUTED, RED

INSTRUCTIONS = {
    "binanceus": (
        "Binance.US → profile → API Management → Create API.\n"
        "• Enable: Read and Spot Trading.\n"
        "• Leave Withdrawals DISABLED.\n"
        "• Restrict access to trusted IPs only, and add your IP below.\n"
        "Paste the API key and the secret key here."
    ),
    "kraken": (
        "Kraken → Security → API → Add key.\n"
        "• Enable: Query Funds, Query Open/Closed Orders & Trades, Create & Modify Orders, Cancel/Close Orders.\n"
        "• Leave Withdraw Funds and Deposit Funds UNCHECKED.\n"
        "Paste the API key and the private key here."
    ),
    "coinbase": (
        "Coinbase → Settings → API → Create API key (Coinbase Developer Platform).\n"
        "• Permissions: View and Trade only. Do NOT grant Transfer.\n"
        "• Add an IP allowlist with your IP below.\n"
        "Paste the key name (organizations/…/apiKeys/…) as the key and the EC private key as the secret."
    ),
    "binance": "Binance.com does not serve US residents. Use Binance.US instead.",
}


class _TestWorker(QThread):
    done = Signal(object)

    def __init__(self, exchange_id, key, secret, passphrase):
        super().__init__()
        self.args = (exchange_id, key, secret, passphrase)

    def run(self) -> None:
        self.done.emit(check_connection(*self.args))


class _IpWorker(QThread):
    done = Signal(str)

    def run(self) -> None:
        try:
            with urllib.request.urlopen("https://api.ipify.org", timeout=6) as r:  # noqa: S310
                self.done.emit(r.read().decode().strip())
        except Exception:  # noqa: BLE001
            self.done.emit("")


class ApiKeyPanel(QWidget):
    saved = Signal(str)  # exchange id

    def __init__(self, exchange_id: str, store=None) -> None:
        super().__init__()
        self.exchange_id = exchange_id
        self.store = store
        self.report: ConnectionReport | None = None
        self._worker: _TestWorker | None = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self.title = QLabel("")
        self.title.setStyleSheet("font-weight: 600;")
        lay.addWidget(self.title)
        self.instr = QLabel("")
        self.instr.setWordWrap(True)
        self.instr.setObjectName("muted")
        lay.addWidget(self.instr)
        self.ip = QLabel("Your public IP: looking up…")
        self.ip.setObjectName("muted")
        lay.addWidget(self.ip)
        form = QFormLayout()
        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText("API key")
        self.secret = QLineEdit()
        self.secret.setEchoMode(QLineEdit.EchoMode.Password)
        self.secret.setPlaceholderText("Secret / private key")
        self.passphrase = QLineEdit()
        self.passphrase.setEchoMode(QLineEdit.EchoMode.Password)
        self.passphrase.setPlaceholderText("(only if your exchange gave you one)")
        form.addRow("API key", self.key)
        form.addRow("Secret", self.secret)
        form.addRow("Passphrase", self.passphrase)
        lay.addLayout(form)
        row = QHBoxLayout()
        self.test_btn = QPushButton("Test connection")
        self.test_btn.clicked.connect(self.test)
        row.addWidget(self.test_btn)
        self.save_btn = QPushButton("Save to keychain")
        self.save_btn.setEnabled(False)
        self.save_btn.clicked.connect(self.save)
        row.addWidget(self.save_btn)
        row.addStretch(1)
        lay.addLayout(row)
        self.result = QLabel("")
        self.result.setWordWrap(True)
        lay.addWidget(self.result)
        self.confirm = QCheckBox("I confirm withdrawals are disabled on this key")
        self.confirm.setVisible(False)
        self.confirm.toggled.connect(self._update_save)
        lay.addWidget(self.confirm)
        note = QLabel("Keys are stored in your operating system's keychain and never shown again. We never ask for a wallet private key or seed phrase.")
        note.setWordWrap(True)
        note.setObjectName("muted")
        lay.addWidget(note)
        self.set_exchange(exchange_id)
        self._ipw = _IpWorker()
        self._ipw.done.connect(lambda ip: self.ip.setText(f"Your public IP (for the exchange's whitelist): {ip}" if ip else "Your public IP: unavailable"))
        self._ipw.start()

    def set_exchange(self, exchange_id: str) -> None:
        self.exchange_id = exchange_id
        name = EXCHANGES[exchange_id]["name"]
        self.title.setText(f"API key for {name}" + ("  ·  a key is already saved" if secrets.has_api_key(exchange_id) else ""))
        self.instr.setText(INSTRUCTIONS.get(exchange_id, ""))
        self.passphrase.setVisible(exchange_id == "coinbase")
        self.report = None
        self.result.setText("")
        self.save_btn.setEnabled(False)
        self.confirm.setVisible(False)

    def test(self) -> None:
        k, s = self.key.text().strip(), self.secret.text().strip()
        if not k or not s:
            self.result.setStyleSheet(f"color: {AMBER};")
            self.result.setText("Enter both the key and the secret first.")
            return
        self.test_btn.setEnabled(False)
        self.result.setStyleSheet(f"color: {MUTED};")
        self.result.setText("Testing…")
        self._worker = _TestWorker(self.exchange_id, k, s, self.passphrase.text().strip() or None)
        self._worker.done.connect(self._on_report)
        self._worker.finished.connect(lambda: self.test_btn.setEnabled(True))
        self._worker.start()

    def _on_report(self, rep: ConnectionReport) -> None:
        self.report = rep
        color = GREEN if rep.safe_to_save and rep.can_withdraw is False else AMBER if rep.ok else RED
        self.result.setStyleSheet(f"color: {color};")
        bal = ", ".join(f"{q} {v:,.2f}" for q, v in rep.balances.items()) or "no USD/USDT/USDC balance"
        self.result.setText(f"{rep.message}\n{rep.summary()}\nBalance: {bal}" if rep.ok else rep.message)
        self.confirm.setVisible(rep.ok and rep.can_withdraw is None)
        self.confirm.setChecked(False)
        self._update_save()

    def _update_save(self) -> None:
        rep = self.report
        ok = bool(rep and rep.safe_to_save and (rep.can_withdraw is False or self.confirm.isChecked()))
        self.save_btn.setEnabled(ok)

    def save(self) -> None:
        secrets.save_api_key(self.exchange_id, self.key.text().strip(), self.secret.text().strip(), self.passphrase.text().strip() or None)
        if self.store is not None and self.report is not None:
            self.store.set_setting(f"apikey.can_trade.{self.exchange_id}", self.report.can_trade)
            self.store.audit("settings", "API key saved to keychain", exchange=self.exchange_id, can_trade=self.report.can_trade, can_withdraw=self.report.can_withdraw)
        self.key.clear()
        self.secret.clear()
        self.passphrase.clear()
        self.save_btn.setEnabled(False)
        self.result.setStyleSheet(f"color: {GREEN};")
        self.result.setText("Saved to the keychain. The key will not be shown again.")
        self.title.setText(f"API key for {EXCHANGES[self.exchange_id]['name']}  ·  a key is already saved")
        self.saved.emit(self.exchange_id)
