from hypothesis import given, settings
from hypothesis import strategies as st

from cryptobot.core.bars import BarAggregator, bucket_ms, resample
from cryptobot.core.models import Tick

S = "BTC/USD"


def tick(ts_ms: int, price: float, amount: float = 1.0) -> Tick:
    return Tick(S, ts_ms, price, amount)


def test_bucket_alignment():
    assert bucket_ms(1_700_000_000_999, 1) == 1_700_000_000_000
    assert bucket_ms(119_999, 60) == 60_000
    assert bucket_ms(120_000, 60) == 120_000
    assert bucket_ms(1_700_000_059_999, 300) == 1_699_999_800_000


def test_ticks_in_same_second_aggregate():
    agg = BarAggregator(S, 1)
    agg.add_tick(tick(1000, 10.0, 1))
    agg.add_tick(tick(1200, 12.0, 2))
    out = agg.add_tick(tick(1900, 9.0, 3))
    assert len(out) == 1
    b = out[0]
    assert (b.open, b.high, b.low, b.close, b.volume, b.trades) == (10.0, 12.0, 9.0, 9.0, 6.0, 3)
    assert b.ts_ms == 1000 and not b.closed


def test_crossing_second_closes_previous():
    agg = BarAggregator(S, 1)
    agg.add_tick(tick(1000, 10.0))
    out = agg.add_tick(tick(2000, 11.0))
    assert [b.ts_ms for b in out] == [1000, 2000]
    assert out[0].closed and not out[1].closed
    assert out[1].open == 11.0


def test_gap_filled_with_flat_bars():
    agg = BarAggregator(S, 1)
    agg.add_tick(tick(1000, 10.0))
    out = agg.add_tick(tick(4500, 12.0))
    assert [b.ts_ms for b in out] == [1000, 2000, 3000, 4000]
    for filler in out[1:3]:
        assert filler.closed and filler.volume == 0 and filler.trades == 0
        assert filler.open == filler.high == filler.low == filler.close == 10.0
    assert out[3].open == 12.0


def test_gap_longer_than_max_fill_leaves_hole():
    agg = BarAggregator(S, 1, max_gap_fill=5)
    agg.add_tick(tick(1000, 10.0))
    out = agg.add_tick(tick(20_000, 12.0))
    assert [b.ts_ms for b in out] == [1000, 20_000]


def test_roll_closes_quiet_bar_and_opens_placeholder():
    agg = BarAggregator(S, 1)
    agg.add_tick(tick(1000, 10.0))
    assert agg.roll(1900) == []
    out = agg.roll(2100)
    assert [b.ts_ms for b in out] == [1000, 2000]
    assert out[0].closed
    assert not out[1].closed and out[1].trades == 0 and out[1].close == 10.0
    # a trade arriving in the placeholder bar adopts its price as the open
    out = agg.add_tick(tick(2500, 13.0))
    assert out[0].open == 13.0 and out[0].trades == 1


def test_late_tick_amends_last_closed_bar():
    agg = BarAggregator(S, 1)
    agg.add_tick(tick(1000, 10.0, 1))
    agg.add_tick(tick(2000, 11.0, 1))
    out = agg.add_tick(tick(1800, 15.0, 2))
    assert len(out) == 1 and out[0].ts_ms == 1000 and out[0].closed
    assert out[0].high == 15.0 and out[0].volume == 3.0
    assert agg.late_ticks == 1 and agg.dropped_ticks == 0


def test_very_late_tick_is_dropped():
    agg = BarAggregator(S, 1)
    agg.add_tick(tick(1000, 10.0))
    agg.add_tick(tick(2000, 11.0))
    agg.add_tick(tick(3000, 11.0))
    assert agg.add_tick(tick(1500, 99.0)) == []
    assert agg.dropped_ticks == 1


def test_resample_1m_to_5m():
    agg = BarAggregator(S, 60, fill_gaps=False)
    bars = []
    for i, p in enumerate([10, 12, 8, 11, 9, 14]):
        bars.extend(b for b in agg.add_tick(tick(i * 60_000, float(p))) if b.closed)
    bars.append(agg.current)
    out = resample(bars, 300)
    assert [b.ts_ms for b in out] == [0, 300_000]
    assert (out[0].open, out[0].high, out[0].low, out[0].close) == (10, 12, 8, 9)
    assert out[0].closed and not out[1].closed


@settings(max_examples=200, deadline=None)
@given(
    st.lists(
        st.tuples(st.integers(min_value=0, max_value=120_000), st.floats(1, 1000), st.floats(0, 10)),
        min_size=1,
        max_size=200,
    )
)
def test_properties_for_any_tick_stream(raw):
    """Bars are contiguous, aligned, OHLC-consistent, and volume is conserved."""
    raw.sort(key=lambda r: r[0])  # exchange streams are time-ordered
    agg = BarAggregator(S, 1)
    seen: dict[int, object] = {}
    for ts, p, a in raw:
        for b in agg.add_tick(tick(ts, p, a)):
            seen[b.ts_ms] = b
    bars = [seen[k] for k in sorted(seen)]
    assert bars, "at least one bar"
    for b in bars:
        assert b.ts_ms % 1000 == 0
        assert b.low <= min(b.open, b.close) <= max(b.open, b.close) <= b.high
    for prev, nxt in zip(bars, bars[1:]):
        assert nxt.ts_ms == prev.ts_ms + 1000, "series must be contiguous"
        assert prev.closed
    assert not bars[-1].closed
    assert abs(sum(b.volume for b in bars) - sum(a for _, _, a in raw)) < 1e-6
    assert sum(b.trades for b in bars) == len(raw)
