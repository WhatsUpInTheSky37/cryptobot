"""Thin synchronous data-access layer over SQLite (WAL mode).

Both the GUI process and the engine process open their own ``Store`` on the
same file; WAL mode makes concurrent readers and a single writer safe.
"""
from __future__ import annotations

import json
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from cryptobot.core.models import Bar
from cryptobot.db.schema import AuditLog, BarRow, Base, EquityPoint, PositionRow, PulseEventRow, Setting, TradeRow


class Store:
    def __init__(self, path: Path | str) -> None:
        if str(path) == ":memory:":
            self.path = Path(":memory:")
            self.engine = create_engine(
                "sqlite:///:memory:", future=True, connect_args={"check_same_thread": False}, poolclass=StaticPool
            )
        else:
            self.path = Path(path)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.engine = create_engine(f"sqlite:///{self.path}", future=True)

            @event.listens_for(self.engine, "connect")
            def _pragmas(dbapi_conn, _record):  # noqa: ANN001
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA synchronous=NORMAL")
                cur.execute("PRAGMA busy_timeout=5000")
                cur.close()

        Base.metadata.create_all(self.engine)

    # -- settings -----------------------------------------------------------

    def get_setting(self, key: str, default: Any = None) -> Any:
        with Session(self.engine) as s:
            row = s.get(Setting, key)
            return json.loads(row.value) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        with Session(self.engine) as s:
            stmt = insert(Setting).values(key=key, value=json.dumps(value))
            stmt = stmt.on_conflict_do_update(index_elements=[Setting.key], set_={"value": stmt.excluded.value})
            s.execute(stmt)
            s.commit()

    # -- bars ---------------------------------------------------------------

    def upsert_bars(self, exchange: str, bars: Iterable[Bar]) -> int:
        rows = [
            {
                "exchange": exchange,
                "symbol": b.symbol,
                "timeframe_s": b.timeframe_s,
                "ts_ms": b.ts_ms,
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": b.volume,
            }
            for b in bars
        ]
        if not rows:
            return 0
        # SQLite limits the number of bound variables per statement, so write in chunks.
        chunk = 500
        with Session(self.engine) as s:
            for i in range(0, len(rows), chunk):
                stmt = insert(BarRow).values(rows[i : i + chunk])
                stmt = stmt.on_conflict_do_update(
                    index_elements=[BarRow.exchange, BarRow.symbol, BarRow.timeframe_s, BarRow.ts_ms],
                    set_={
                        "open": stmt.excluded.open,
                        "high": stmt.excluded.high,
                        "low": stmt.excluded.low,
                        "close": stmt.excluded.close,
                        "volume": stmt.excluded.volume,
                    },
                )
                s.execute(stmt)
            s.commit()
        return len(rows)

    def load_bars(self, exchange: str, symbol: str, timeframe_s: int, limit: int = 500) -> list[Bar]:
        with Session(self.engine) as s:
            q = (
                select(BarRow)
                .where(BarRow.exchange == exchange, BarRow.symbol == symbol, BarRow.timeframe_s == timeframe_s)
                .order_by(BarRow.ts_ms.desc())
                .limit(limit)
            )
            rows = list(s.scalars(q))
        rows.reverse()
        return [Bar(r.symbol, r.timeframe_s, r.ts_ms, r.open, r.high, r.low, r.close, r.volume, 0, True) for r in rows]

    def last_closed_ts(self, exchange: str, symbol: str, timeframe_s: int) -> int | None:
        with Session(self.engine) as s:
            q = (
                select(BarRow.ts_ms)
                .where(BarRow.exchange == exchange, BarRow.symbol == symbol, BarRow.timeframe_s == timeframe_s)
                .order_by(BarRow.ts_ms.desc())
                .limit(1)
            )
            return s.scalar(q)

    # -- trades / positions / equity ---------------------------------------

    def insert_trade(self, **fields: Any) -> int:
        with Session(self.engine) as s:
            row = TradeRow(**fields)
            s.add(row)
            s.commit()
            return int(row.id)

    def load_trades(self, mode: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        with Session(self.engine) as s:
            q = select(TradeRow).order_by(TradeRow.ts_ms.desc()).limit(limit)
            if mode:
                q = q.where(TradeRow.mode == mode)
            rows = list(s.scalars(q))
        return [{c.name: getattr(r, c.name) for c in TradeRow.__table__.columns} for r in rows]

    def count_trades(self, mode: str) -> int:
        with Session(self.engine) as s:
            return len(list(s.scalars(select(TradeRow.id).where(TradeRow.mode == mode))))

    def open_position(self, **fields: Any) -> int:
        with Session(self.engine) as s:
            row = PositionRow(**fields)
            s.add(row)
            s.commit()
            return int(row.id)

    def update_position(self, position_id: int, **fields: Any) -> None:
        with Session(self.engine) as s:
            s.execute(update(PositionRow).where(PositionRow.id == position_id).values(**fields))
            s.commit()

    def load_positions(self, mode: str, status: str = "open") -> list[dict[str, Any]]:
        with Session(self.engine) as s:
            rows = list(s.scalars(select(PositionRow).where(PositionRow.mode == mode, PositionRow.status == status)))
        return [{c.name: getattr(r, c.name) for c in PositionRow.__table__.columns} for r in rows]

    def add_equity(self, mode: str, ts_ms: int, equity: float, cash: float, in_use: float) -> None:
        with Session(self.engine) as s:
            s.add(EquityPoint(mode=mode, ts_ms=ts_ms, equity=equity, cash=cash, in_use=in_use))
            s.commit()

    def load_equity(self, mode: str, limit: int = 5000) -> list[tuple[int, float]]:
        with Session(self.engine) as s:
            q = select(EquityPoint.ts_ms, EquityPoint.equity).where(EquityPoint.mode == mode).order_by(EquityPoint.ts_ms.desc()).limit(limit)
            rows = list(s.execute(q))
        rows.reverse()
        return [(int(t), float(e)) for t, e in rows]

    def log_mode_switch(self, ts_ms: int, from_mode: str, to_mode: str, stats: dict[str, Any]) -> None:
        from cryptobot.db.schema import ModeSwitch

        with Session(self.engine) as s:
            s.add(ModeSwitch(ts_ms=ts_ms, from_mode=from_mode, to_mode=to_mode, stats_json=json.dumps(stats, default=str)))
            s.commit()

    # -- pulse market memory ------------------------------------------------

    def insert_pulse_event(self, **fields: Any) -> int:
        with Session(self.engine) as s:
            row = PulseEventRow(**fields)
            s.add(row)
            s.commit()
            return int(row.id)

    def update_pulse_event(self, event_id: int, **fields: Any) -> None:
        with Session(self.engine) as s:
            s.execute(update(PulseEventRow).where(PulseEventRow.id == event_id).values(**fields))
            s.commit()

    def load_pulse_events(self, limit: int = 500, resolved_only: bool = False, direction: str | None = None) -> list[dict[str, Any]]:
        with Session(self.engine) as s:
            q = select(PulseEventRow).order_by(PulseEventRow.ts_ms.desc()).limit(limit)
            if resolved_only:
                q = q.where(PulseEventRow.outcome != "pending")
            if direction:
                q = q.where(PulseEventRow.direction == direction)
            rows = list(s.scalars(q))
        return [{c.name: getattr(r, c.name) for c in PulseEventRow.__table__.columns} for r in rows]

    # -- audit --------------------------------------------------------------

    def audit(self, category: str, message: str, level: str = "INFO", **data: Any) -> None:
        with Session(self.engine) as s:
            s.add(
                AuditLog(
                    ts_ms=int(time.time() * 1000),
                    level=level,
                    category=category,
                    message=message,
                    data_json=json.dumps(data, default=str),
                )
            )
            s.commit()
