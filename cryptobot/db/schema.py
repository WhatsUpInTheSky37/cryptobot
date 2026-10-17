"""SQLite schema. Trades and positions are keyed by mode so TEST and BUY never mix."""
from __future__ import annotations

from sqlalchemy import Float, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)  # JSON


class BarRow(Base):
    __tablename__ = "bars"
    exchange: Mapped[str] = mapped_column(String(32), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    timeframe_s: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts_ms: Mapped[int] = mapped_column(Integer, primary_key=True)
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float, default=0.0)


class TradeRow(Base):
    __tablename__ = "trades"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mode: Mapped[str] = mapped_column(String(8), index=True)  # TEST or BUY
    exchange: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(4))
    ts_ms: Mapped[int] = mapped_column(Integer, index=True)
    price: Mapped[float] = mapped_column(Float)  # expected / signal price
    amount: Mapped[float] = mapped_column(Float)
    fee: Mapped[float] = mapped_column(Float, default=0.0)
    fee_currency: Mapped[str] = mapped_column(String(16), default="")
    slippage: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str] = mapped_column(Text, default="")
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    position_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # BUY mode only
    order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="filled")


class PositionRow(Base):
    __tablename__ = "positions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mode: Mapped[str] = mapped_column(String(8), index=True)
    exchange: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(5), default="long")
    entry_ts_ms: Mapped[int] = mapped_column(Integer)
    entry_price: Mapped[float] = mapped_column(Float)
    amount: Mapped[float] = mapped_column(Float)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    trailing_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(8), default="open", index=True)
    exit_ts_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)


class EquityPoint(Base):
    __tablename__ = "equity"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mode: Mapped[str] = mapped_column(String(8), index=True)
    ts_ms: Mapped[int] = mapped_column(Integer, index=True)
    equity: Mapped[float] = mapped_column(Float)
    cash: Mapped[float] = mapped_column(Float)
    in_use: Mapped[float] = mapped_column(Float)


class ModeSwitch(Base):
    __tablename__ = "mode_switches"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts_ms: Mapped[int] = mapped_column(Integer)
    from_mode: Mapped[str] = mapped_column(String(8))
    to_mode: Mapped[str] = mapped_column(String(8))
    stats_json: Mapped[str] = mapped_column(Text, default="{}")


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts_ms: Mapped[int] = mapped_column(Integer, index=True)
    level: Mapped[str] = mapped_column(String(8))
    category: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text)
    data_json: Mapped[str] = mapped_column(Text, default="{}")


class PulseEventRow(Base):
    """Market Memory: every pressure-release setup, taken or not, and what price did next."""

    __tablename__ = "pulse_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts_ms: Mapped[int] = mapped_column(Integer, index=True)
    exchange: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    direction: Mapped[str] = mapped_column(String(5))  # long | short
    prs: Mapped[float] = mapped_column(Float)
    f: Mapped[float] = mapped_column(Float)
    a: Mapped[float] = mapped_column(Float)
    w: Mapped[float] = mapped_column(Float)
    r: Mapped[float] = mapped_column(Float)
    c: Mapped[float] = mapped_column(Float)
    imbalance: Mapped[float] = mapped_column(Float)
    vol_pct: Mapped[float] = mapped_column(Float)
    spread_pct: Mapped[float] = mapped_column(Float)
    mid: Mapped[float] = mapped_column(Float)
    target_pct: Mapped[float] = mapped_column(Float)
    stop_pct: Mapped[float] = mapped_column(Float)
    cost_pct: Mapped[float] = mapped_column(Float)
    similar_n: Mapped[int] = mapped_column(Integer, default=0)
    similar_p: Mapped[float] = mapped_column(Float, default=0.0)
    ev_pct: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(10))  # entered | rejected | research
    reason: Mapped[str] = mapped_column(Text, default="")
    outcome: Mapped[str] = mapped_column(String(8), default="pending", index=True)  # pending | target | stop | expired
    mfe_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    mae_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    resolved_ts_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trade_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)


Index("ix_trades_mode_ts", TradeRow.mode, TradeRow.ts_ms)
