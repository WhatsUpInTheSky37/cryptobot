"""Events the engine process sends to the GUI process. All picklable."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from cryptobot.core.models import Bar, Tick

ConnectionStatus = Literal[
    "connecting", "connected", "reconnecting", "blocked", "error", "stopped"
]


@dataclass(slots=True)
class ConnectionEvent:
    exchange: str
    status: ConnectionStatus
    message: str = ""
    attempt: int = 0
    retry_in_s: float = 0.0


@dataclass(slots=True)
class TickEvent:
    exchange: str
    tick: Tick


@dataclass(slots=True)
class BarsEvent:
    """Bars to upsert by open time. ``replace`` means discard what you had first."""

    exchange: str
    symbol: str
    timeframe_s: int
    bars: list[Bar] = field(default_factory=list)
    replace: bool = False


@dataclass(slots=True)
class BookEvent:
    """Order-book snapshot summary, at most once per second per symbol."""

    exchange: str
    symbol: str
    ts_ms: int
    best_bid: float
    best_ask: float
    bid_depth_near: float  # quote value of bids within 0.1% of mid
    ask_depth_near: float
    bid_depth_wide: float  # within 0.5% of mid
    ask_depth_wide: float
    seq: int = 0  # exchange update id of the book this summary came from (0 = unknown)
    synced: bool = True  # False when the local book is known to be out of sync (never trade on it)

    @property
    def mid(self) -> float:
        return (self.best_bid + self.best_ask) / 2.0

    @property
    def spread_pct(self) -> float:
        m = self.mid
        return (self.best_ask - self.best_bid) / m * 100.0 if m else 0.0


@dataclass(slots=True)
class SymbolDroppedEvent:
    """The feed could not watch this symbol (not listed on the exchange) and continues without it."""

    exchange: str
    symbol: str
    reason: str


@dataclass(slots=True)
class BookSyncEvent:
    """Order-book synchronisation state for one symbol (snapshot, gap, resync, verified)."""

    exchange: str
    symbol: str
    ts_ms: int
    status: str  # snapshot | synced | gap | resync | unsynced | verified | mismatch | stopped
    detail: str = ""
    seq: int = 0


@dataclass(slots=True)
class HeartbeatEvent:
    exchange: str
    ts_ms: int


@dataclass(slots=True)
class LogEvent:
    level: str
    logger: str
    message: str
    ts_ms: int


Event = ConnectionEvent | TickEvent | BarsEvent | BookEvent | BookSyncEvent | HeartbeatEvent | LogEvent


# ---------------------------------------------------------------------------
# Trading events (Milestone 3+)


@dataclass(slots=True)
class MarketInfoEvent:
    exchange: str
    symbol: str
    taker_fee: float
    maker_fee: float
    min_amount: float
    min_cost: float
    amount_step: float
    price_step: float


@dataclass(slots=True)
class PositionSnapshot:
    symbol: str
    mode: str
    amount: float
    entry_price: float
    current_price: float
    pnl: float
    pnl_pct: float
    stop: float | None
    take_profit: float | None
    entry_ts_ms: int
    basis: float


@dataclass(slots=True)
class TraderStateEvent:
    mode: str  # STOPPED | TEST | BUY
    status: str  # stopped | starting | running | halted_daily | halted_drawdown
    message: str = ""
    exchange: str = ""
    symbols: list[str] = field(default_factory=list)
    strategy: str = ""
    started_ms: int = 0
    halted_until_ms: int = 0
    next_decision_ms: int = 0


@dataclass(slots=True)
class BudgetEvent:
    mode: str
    allocated: float
    in_use: float
    available: float
    cash: float
    equity: float
    today_pnl: float
    peak: float
    drawdown_pct: float
    ts_ms: int


@dataclass(slots=True)
class PositionsEvent:
    mode: str
    positions: list[PositionSnapshot] = field(default_factory=list)


@dataclass(slots=True)
class FillEvent:
    mode: str
    ts_ms: int
    symbol: str
    side: str
    price: float
    amount: float
    fee: float
    reason: str
    pnl: float | None = None
    order_id: str | None = None
    status: str = "filled"


@dataclass(slots=True)
class SignalEvent:
    mode: str
    symbol: str
    ts_ms: int
    exposure: float
    text: str


@dataclass(slots=True)
class AlertEvent:
    level: str  # info | warn | bad
    title: str
    message: str
    ts_ms: int


@dataclass(slots=True)
class EquityEvent:
    mode: str
    ts_ms: int
    equity: float


# ---------------------------------------------------------------------------
# Crypto Pulse (order-flow research engine)


@dataclass(slots=True)
class PulseSnapshot:
    """One coin's live state. When ``valid`` is False the scores are meaningless
    and the GUI shows INSUFFICIENT DATA with ``health_detail`` instead."""

    symbol: str
    ts_ms: int
    state: str  # off | warming | excluded | idle | monitor | prepared | entered
    prs_long: float
    prs_short: float
    f: float
    a: float
    w: float
    r: float
    c: float
    imbalance_60: float
    mid: float
    spread_pct: float
    vol_pct: float
    note: str = ""
    valid: bool = True
    health: str = "ok"
    health_detail: str = ""
    regime: str = "unknown"
    scores_long: dict[str, float] = field(default_factory=dict)
    scores_short: dict[str, float] = field(default_factory=dict)
    assessment_long: dict[str, object] = field(default_factory=dict)  # costs, target/stop, EV, evidence
    assessment_short: dict[str, object] = field(default_factory=dict)
    bid: float = 0.0
    ask: float = 0.0
    trades_per_min: float = 0.0
    book_seq: int = 0


@dataclass(slots=True)
class PulseModeStatus:
    """One paper-trading configuration (Conservative / Balanced / Experimental)."""

    key: str
    label: str
    budget: float
    equity: float
    cash: float
    open_positions: int
    realized_today: float
    unrealized: float
    open_risk_pct: float
    trades: int
    wins: int
    losses: int
    net_pnl: float
    gross_profit: float
    gross_loss: float
    consecutive_losses: int
    paused_until_ms: int
    halted: bool
    message: str = ""
    positions: list[dict] = field(default_factory=list)
    accepted: int = 0
    rejected: int = 0


@dataclass(slots=True)
class PulseStatusEvent:
    running: bool
    paper_budget: float
    paper_equity: float
    open_positions: int
    events_recorded: int
    events_resolved: int
    hit_rate: float
    required_win_rate: float
    ev_pct: float
    trades: int
    trade_pnl: float
    consecutive_losses: int
    paused_until_ms: int
    message: str
    snapshots: list[PulseSnapshot] = field(default_factory=list)
    stop_pct: float = 0.0
    target_pct: float = 0.0
    cost_pct: float = 0.0
    connected: bool = False
    modes: list[PulseModeStatus] = field(default_factory=list)
    memory: dict[str, object] = field(default_factory=dict)  # resolved counts, effective samples, evidence text
    ml: dict[str, object] = field(default_factory=dict)
    health_summary: str = ""


@dataclass(slots=True)
class PulseEventRecord:
    id: int
    ts_ms: int
    symbol: str
    direction: str
    prs: float
    status: str  # signal | research | "" (outcome update)
    reason: str
    outcome: str  # pending | target | stop | expired | abandoned
    mfe_pct: float | None
    mae_pct: float | None
    similar_n: int
    similar_p: float
    ev_pct: float
    regime: str = ""
    net_pct: float | None = None
    first_hit: str | None = None
    decisions: dict[str, tuple[bool, str]] = field(default_factory=dict)  # mode -> (accepted, reason)


# ---------------------------------------------------------------------------
# Early Warning (accumulation & surge research, paper only)


@dataclass(slots=True)
class EWCandidateView:
    symbol: str
    state: str  # one of earlywarning.config.STATES
    eas: float | None
    components: dict[str, object]  # ranks per component, or {'status': 'INSUFFICIENT DATA', 'detail': ...}
    data_status: str  # "ok" or the reason data is not usable
    price: float
    changes: dict[int, float | None]  # % over 5/15/30/60 minutes
    rel_volume: float | None
    buy_sell_ratio: float | None
    freq_accel: float | None
    similarity: dict[str, object]  # {'n': ..., 'p': ... or None, 'note': ...}
    surge_probability: float | None  # only when Model A is validated
    target_label: str
    target_net_pct: float | None  # only when Model B is validated and positive after costs
    est_net_pct: float | None
    warnings: list[str]
    spread_pct: float | None
    quote_volume_1m: float | None
    scanner_reasons: list[str]
    updated_ms: int = 0
    note: str = ""


@dataclass(slots=True)
class EWStatusEvent:
    running: bool
    connected: bool
    message: str
    scanner: dict[str, object] = field(default_factory=dict)
    candidates: list[EWCandidateView] = field(default_factory=list)
    paper: dict[str, object] = field(default_factory=dict)
    stats: dict[str, object] = field(default_factory=dict)  # true/false positives, missed surges, precision, recall
    research: dict[str, object] = field(default_factory=dict)
    transitions: list[dict] = field(default_factory=list)
