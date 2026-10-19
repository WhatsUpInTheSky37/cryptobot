"""Thin synchronous data-access layer over SQLite (WAL mode).

Both the GUI process and the engine process open their own ``Store`` on the
same file; WAL mode makes concurrent readers and a single writer safe.
"""
from __future__ import annotations

import json
import time
from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event, inspect, select, text, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from cryptobot.core.models import Bar
from cryptobot.db.schema import OUTCOME_COLUMNS, AppliedExecution, AuditLog, BarRow, Base, EquityPoint, OrderJournal, PositionRow, PulseDecisionRow, PulseEventRow, Setting, TradeRow


class Store:
    def __init__(self, path: Path | str) -> None:
        self._active_session: ContextVar[Session | None] = ContextVar("store_session", default=None)
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
                # The client-ID journal must survive power loss before an order
                # can leave the process, not just an ordinary process restart.
                cur.execute("PRAGMA synchronous=FULL")
                cur.execute("PRAGMA busy_timeout=5000")
                cur.close()

        Base.metadata.create_all(self.engine)
        self.migrated = self._migrate()

    @contextmanager
    def transaction(self):
        """Compose writes into a single commit; state and fill deduplication share it."""
        current = self._active_session.get()
        if current is not None:
            yield current
            return
        with Session(self.engine, expire_on_commit=False) as session:
            token = self._active_session.set(session)
            try:
                yield session
                session.commit()
            except BaseException:
                session.rollback()
                raise
            finally:
                self._active_session.reset(token)

    def journal_order(self, client_id: str, scope: str, symbol: str, request: dict[str, Any]) -> None:
        with self.transaction() as s:
            s.add(OrderJournal(client_id=client_id, scope=scope, symbol=symbol, status="submitting", request_json=json.dumps(request), updated_ms=int(time.time() * 1000)))

    def update_order(self, client_id: str, status: str, response: dict[str, Any] | None = None, fill: dict[str, Any] | None = None, applied: bool | None = None) -> None:
        values: dict[str, Any] = {"status": status, "updated_ms": int(time.time() * 1000)}
        if response is not None:
            values["response_json"] = json.dumps(response, default=str)
        if fill is not None:
            values["fill_json"] = json.dumps(fill)
        if applied is not None:
            values["applied"] = applied
        with self.transaction() as s:
            s.execute(update(OrderJournal).where(OrderJournal.client_id == client_id).values(**values))

    def pending_orders(self, scope: str) -> list[dict[str, Any]]:
        with self.transaction() as s:
            rows = list(s.scalars(select(OrderJournal).where(OrderJournal.scope == scope, OrderJournal.applied.is_(False), OrderJournal.status.not_in(("rejected", "canceled_empty")))))
            return [{"client_id": r.client_id, "symbol": r.symbol, "status": r.status, "request": json.loads(r.request_json), "response": json.loads(r.response_json), "fill": json.loads(r.fill_json) if r.fill_json else None} for r in rows]

    def execution_applied(self, key: str) -> bool:
        with self.transaction() as s:
            return s.get(AppliedExecution, key) is not None

    def mark_execution(self, key: str, ts_ms: int) -> None:
        with self.transaction() as s:
            s.add(AppliedExecution(execution_key=key, ts_ms=ts_ms))

    def _migrate(self) -> list[str]:
        """Add columns that exist in the models but not in an older database file.

        ``create_all`` creates missing tables only. Every added column is nullable
        or has a default, so old rows stay valid."""
        added: list[str] = []
        insp = inspect(self.engine)
        with self.engine.begin() as conn:
            for table in Base.metadata.sorted_tables:
                have = {c["name"] for c in insp.get_columns(table.name)}
                for col in table.columns:
                    if col.name in have:
                        continue
                    ctype = col.type.compile(dialect=self.engine.dialect)
                    ddl = f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {ctype}'
                    if col.default is not None and getattr(col.default, "arg", None) is not None and not callable(col.default.arg):
                        v = col.default.arg
                        lit = ("1" if v else "0") if isinstance(v, bool) else (repr(v) if isinstance(v, str) else str(v))
                        ddl += f" DEFAULT {lit}"
                    conn.execute(text(ddl))
                    added.append(f"{table.name}.{col.name}")
        return added

    # -- settings -----------------------------------------------------------

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self.transaction() as s:
            row = s.get(Setting, key)
            return json.loads(row.value) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        with self.transaction() as s:
            stmt = insert(Setting).values(key=key, value=json.dumps(value))
            stmt = stmt.on_conflict_do_update(index_elements=[Setting.key], set_={"value": stmt.excluded.value})
            s.execute(stmt)
            s.flush()

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
        with self.transaction() as s:
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
            s.flush()
        return len(rows)

    def load_bars(self, exchange: str, symbol: str, timeframe_s: int, limit: int = 500) -> list[Bar]:
        with self.transaction() as s:
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
        with self.transaction() as s:
            q = (
                select(BarRow.ts_ms)
                .where(BarRow.exchange == exchange, BarRow.symbol == symbol, BarRow.timeframe_s == timeframe_s)
                .order_by(BarRow.ts_ms.desc())
                .limit(1)
            )
            return s.scalar(q)

    # -- trades / positions / equity ---------------------------------------

    def insert_trade(self, **fields: Any) -> int:
        with self.transaction() as s:
            row = TradeRow(**fields)
            s.add(row)
            s.flush()
            return int(row.id)

    def load_trades(self, mode: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        with self.transaction() as s:
            q = select(TradeRow).order_by(TradeRow.ts_ms.desc()).limit(limit)
            if mode:
                q = q.where(TradeRow.mode == mode)
            rows = list(s.scalars(q))
        return [{c.name: getattr(r, c.name) for c in TradeRow.__table__.columns} for r in rows]

    def count_trades(self, mode: str) -> int:
        with self.transaction() as s:
            return len(list(s.scalars(select(TradeRow.id).where(TradeRow.mode == mode))))

    def open_position(self, **fields: Any) -> int:
        with self.transaction() as s:
            row = PositionRow(**fields)
            s.add(row)
            s.flush()
            return int(row.id)

    def update_position(self, position_id: int, **fields: Any) -> None:
        with self.transaction() as s:
            s.execute(update(PositionRow).where(PositionRow.id == position_id).values(**fields))
            s.flush()

    def load_positions(self, mode: str, status: str = "open") -> list[dict[str, Any]]:
        with self.transaction() as s:
            rows = list(s.scalars(select(PositionRow).where(PositionRow.mode == mode, PositionRow.status == status)))
        return [{c.name: getattr(r, c.name) for c in PositionRow.__table__.columns} for r in rows]

    def add_equity(self, mode: str, ts_ms: int, equity: float, cash: float, in_use: float) -> None:
        with self.transaction() as s:
            s.add(EquityPoint(mode=mode, ts_ms=ts_ms, equity=equity, cash=cash, in_use=in_use))
            s.flush()

    def load_equity(self, mode: str, limit: int = 5000) -> list[tuple[int, float]]:
        with self.transaction() as s:
            q = select(EquityPoint.ts_ms, EquityPoint.equity).where(EquityPoint.mode == mode).order_by(EquityPoint.ts_ms.desc()).limit(limit)
            rows = list(s.execute(q))
        rows.reverse()
        return [(int(t), float(e)) for t, e in rows]

    def log_mode_switch(self, ts_ms: int, from_mode: str, to_mode: str, stats: dict[str, Any]) -> None:
        from cryptobot.db.schema import ModeSwitch

        with self.transaction() as s:
            s.add(ModeSwitch(ts_ms=ts_ms, from_mode=from_mode, to_mode=to_mode, stats_json=json.dumps(stats, default=str)))
            s.flush()

    # -- pulse market memory ------------------------------------------------

    def insert_pulse_event(self, **fields: Any) -> int:
        with self.transaction() as s:
            row = PulseEventRow(**fields)
            s.add(row)
            s.flush()
            return int(row.id)

    def update_pulse_event(self, event_id: int, **fields: Any) -> None:
        """Only outcome columns may change after a signal is recorded. Signal-time
        values are immutable so later price data can never leak into them."""
        bad = set(fields) - OUTCOME_COLUMNS
        if bad:
            raise ValueError(f"signal-time columns are immutable: {sorted(bad)}")
        with self.transaction() as s:
            s.execute(update(PulseEventRow).where(PulseEventRow.id == event_id).values(**fields))
            s.flush()

    def pulse_event(self, event_id: int) -> dict[str, Any] | None:
        with self.transaction() as s:
            r = s.get(PulseEventRow, event_id)
            return {c.name: getattr(r, c.name) for c in PulseEventRow.__table__.columns} if r else None

    def load_pulse_events(self, limit: int = 500, resolved_only: bool = False, direction: str | None = None, *, symbol: str | None = None,
                          regime: str | None = None, since_ms: int | None = None, pending_only: bool = False, ascending: bool = False) -> list[dict[str, Any]]:
        with self.transaction() as s:
            q = select(PulseEventRow).order_by(PulseEventRow.ts_ms.asc() if ascending else PulseEventRow.ts_ms.desc()).limit(limit)
            if resolved_only:
                q = q.where(PulseEventRow.outcome.not_in(("pending", "abandoned")))
            if pending_only:
                q = q.where(PulseEventRow.outcome == "pending")
            if direction:
                q = q.where(PulseEventRow.direction == direction)
            if symbol:
                q = q.where(PulseEventRow.symbol == symbol)
            if regime:
                q = q.where(PulseEventRow.regime == regime)
            if since_ms is not None:
                q = q.where(PulseEventRow.ts_ms >= since_ms)
            rows = list(s.scalars(q))
        return [{c.name: getattr(r, c.name) for c in PulseEventRow.__table__.columns} for r in rows]

    def count_pulse_events(self, resolved_only: bool = False) -> int:
        with self.transaction() as s:
            q = select(PulseEventRow.id)
            if resolved_only:
                q = q.where(PulseEventRow.outcome.not_in(("pending", "abandoned")))
            return len(list(s.scalars(q)))

    def insert_decision(self, **fields: Any) -> int:
        with self.transaction() as s:
            row = PulseDecisionRow(**fields)
            s.add(row)
            s.flush()
            return int(row.id)

    def update_decision(self, decision_id: int, **fields: Any) -> None:
        with self.transaction() as s:
            s.execute(update(PulseDecisionRow).where(PulseDecisionRow.id == decision_id).values(**fields))
            s.flush()

    def load_decisions(self, *, event_id: int | None = None, mode: str | None = None, accepted: bool | None = None, limit: int = 5000) -> list[dict[str, Any]]:
        with self.transaction() as s:
            q = select(PulseDecisionRow).order_by(PulseDecisionRow.ts_ms.desc()).limit(limit)
            if event_id is not None:
                q = q.where(PulseDecisionRow.event_id == event_id)
            if mode:
                q = q.where(PulseDecisionRow.mode == mode)
            if accepted is not None:
                q = q.where(PulseDecisionRow.accepted == accepted)
            rows = list(s.scalars(q))
        return [{c.name: getattr(r, c.name) for c in PulseDecisionRow.__table__.columns} for r in rows]

    # -- audit --------------------------------------------------------------

    def audit(self, category: str, message: str, level: str = "INFO", **data: Any) -> None:
        with self.transaction() as s:
            s.add(
                AuditLog(
                    ts_ms=int(time.time() * 1000),
                    level=level,
                    category=category,
                    message=message,
                    data_json=json.dumps(data, default=str),
                )
            )
            s.flush()

