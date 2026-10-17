from cryptobot.core.models import Bar
from cryptobot.db.store import Store


def test_store_creates_db_and_roundtrips(tmp_path):
    db = tmp_path / "sub" / "t.db"
    store = Store(db)
    assert db.exists()
    assert store.get_setting("missing", 42) == 42
    store.set_setting("market.exchange", "kraken")
    store.set_setting("market.exchange", "coinbase")
    assert store.get_setting("market.exchange") == "coinbase"


def test_upsert_bars_is_idempotent_and_updates(tmp_path):
    store = Store(tmp_path / "t.db")
    b1 = Bar("BTC/USD", 60, 0, 1, 2, 0.5, 1.5, 10, 3, True)
    b2 = Bar("BTC/USD", 60, 60_000, 1.5, 3, 1, 2.5, 20, 4, True)
    assert store.upsert_bars("kraken", [b1, b2]) == 2
    b2.close = 2.9
    store.upsert_bars("kraken", [b2])
    bars = store.load_bars("kraken", "BTC/USD", 60)
    assert [b.ts_ms for b in bars] == [0, 60_000]
    assert bars[1].close == 2.9
    assert store.last_closed_ts("kraken", "BTC/USD", 60) == 60_000
    assert store.load_bars("coinbase", "BTC/USD", 60) == []
    store.audit("test", "hello", x=1)


def test_upsert_many_bars_in_one_call(tmp_path):
    """A year of hourly candles (8,760 rows) used to exceed SQLite's bound-variable limit."""
    store = Store(tmp_path / "t.db")
    bars = [Bar("BTC/USDT", 3600, i * 3_600_000, 1, 2, 0.5, 1.5, 10, 3, True) for i in range(9000)]
    assert store.upsert_bars("binanceus", bars) == 9000
    assert len(store.load_bars("binanceus", "BTC/USDT", 3600, limit=20_000)) == 9000
    assert store.upsert_bars("binanceus", bars[:1200]) == 1200  # idempotent across chunks
    assert len(store.load_bars("binanceus", "BTC/USDT", 3600, limit=20_000)) == 9000
