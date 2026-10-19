import numpy as np
import pytest

from cryptobot.core.models import Bar
from cryptobot.strategy import indicators as ind
from cryptobot.strategy.base import BarArrays
from cryptobot.strategy.config import StrategyConfig, StrategySpec
from cryptobot.strategy.library import STRATEGIES
from cryptobot.strategy.signals import compute_target_exposure


def synthetic(n=400, seed=0, drift=0.0005, vol=0.03) -> BarArrays:
    rng = np.random.default_rng(seed)
    px = 100 * np.exp(np.cumsum(rng.normal(drift, vol, n)))
    bars = []
    for i in range(n):
        o = px[i - 1] if i else px[0]
        c = px[i]
        h = max(o, c) * (1 + abs(rng.normal(0, 0.01)))
        l = min(o, c) * (1 - abs(rng.normal(0, 0.01)))
        bars.append(Bar("BTC/USD", 86400, i * 86_400_000, o, h, l, c, rng.uniform(10, 100), 5, True))
    return BarArrays.from_bars(bars)


# ---------------- indicators ----------------


def test_sma_and_ema_reference_values():
    x = np.array([1, 2, 3, 4, 5], dtype=float)
    np.testing.assert_allclose(ind.sma(x, 3), [np.nan, np.nan, 2, 3, 4])
    np.testing.assert_allclose(ind.ema(x, 3), [np.nan, np.nan, 2, 3, 4])  # alpha 0.5: 2, 3, 4
    x2 = np.array([10, 10, 10, 20], dtype=float)
    e = ind.ema(x2, 3)
    assert e[3] == pytest.approx(0.5 * 20 + 0.5 * 10)


def test_rsi_against_straightforward_loop():
    rng = np.random.default_rng(3)
    close = 100 + np.cumsum(rng.normal(0, 1, 60))
    n = 14
    got = ind.rsi(close, n)
    # independent reference: Wilder smoothing written out longhand
    d = np.diff(close)
    up, dn = np.maximum(d, 0), np.maximum(-d, 0)
    au, ad = up[:n].mean(), dn[:n].mean()
    ref = {n: 100 - 100 / (1 + au / ad)}
    for i in range(n, len(d)):
        au = (au * (n - 1) + up[i]) / n
        ad = (ad * (n - 1) + dn[i]) / n
        ref[i + 1] = 100 - 100 / (1 + au / ad)
    for i, v in ref.items():
        assert got[i] == pytest.approx(v)
    assert np.isnan(got[:n]).all()
    assert ind.rsi(np.arange(1.0, 40.0), 14)[-1] == 100.0
    assert ind.rsi(np.arange(40.0, 1.0, -1), 14)[-1] == 0.0


def test_bollinger_and_atr_and_donchian():
    b = synthetic(100)
    lo, mid, hi = ind.bollinger(b.close, 20, 2.0)
    np.testing.assert_allclose(mid, ind.sma(b.close, 20))
    np.testing.assert_allclose(hi - lo, 4 * ind.rolling_std(b.close, 20))
    h = np.full(50, 102.0)
    l = np.full(50, 98.0)
    c = np.full(50, 100.0)
    assert ind.atr(h, l, c, 14)[-1] == pytest.approx(4.0)
    dh, dl = ind.donchian(b.high, b.low, 10)
    for i in range(11, 100):
        assert dh[i] == b.high[i - 10 : i].max() and dl[i] == b.low[i - 10 : i].min()


def test_adx_separates_trend_from_chop():
    n = 200
    t = np.arange(n, dtype=float)
    trend_c = 100 + 2 * t
    trend = ind.adx(trend_c + 1, trend_c - 1, trend_c, 14)
    chop_c = 100 + 3 * np.sin(t)
    chop = ind.adx(chop_c + 1, chop_c - 1, chop_c, 14)
    assert trend[-1] > 40
    assert chop[-1] < 25
    assert np.nanmin(trend) >= 0 and np.nanmax(trend) <= 100


# ---------------- strategies ----------------


@pytest.mark.parametrize("sid", list(STRATEGIES))
def test_strategies_output_shape_and_range(sid):
    b = synthetic(300)
    e = STRATEGIES[sid]().compute(b, STRATEGIES[sid].defaults())
    assert e.shape == (300,)
    v = e[~np.isnan(e)]
    assert len(v) > 100
    assert v.min() >= 0 and v.max() <= 1


@pytest.mark.parametrize("sid", list(STRATEGIES))
@pytest.mark.parametrize("mods", [False, True])
def test_no_look_ahead(sid, mods):
    """Exposure at bar i must not change when later bars are added."""
    b = synthetic(400, seed=7)
    cfg = StrategyConfig(strategies=[StrategySpec(id=sid)], regime_gate=mods, vol_target=mods)
    full, _ = compute_target_exposure(cfg, b)
    for cut in (120, 250, 399):
        part, _ = compute_target_exposure(cfg, b.head(cut))
        np.testing.assert_array_equal(np.nan_to_num(full[:cut], nan=-1), np.nan_to_num(part, nan=-1))


def test_ema_cross_logic_and_regime_gate():
    b = synthetic(300, seed=1)
    cfg = StrategyConfig(strategies=[StrategySpec(id="ema_cross", params={"fast": 5, "slow": 20})], regime_gate=False, vol_target=False)
    e, warm = compute_target_exposure(cfg, b)
    f, s = ind.ema(b.close, 5), ind.ema(b.close, 20)
    assert warm == 19
    assert np.array_equal(e[warm:], (f > s)[warm:].astype(float))
    gated = StrategyConfig(strategies=[StrategySpec(id="ema_cross", params={"fast": 5, "slow": 20})], regime_gate=True, adx_threshold=25, vol_target=False)
    g, _ = compute_target_exposure(gated, b)
    a = ind.adx(b.high, b.low, b.close, 14)
    ok = ~np.isnan(g)
    assert np.all(g[ok] <= e[ok])
    assert np.all(g[ok & (a < 25)] == 0)


def test_vol_target_never_scales_up_and_blend_averages():
    b = synthetic(300, seed=2)
    base = StrategyConfig(strategies=[StrategySpec(id="macd")], regime_gate=False, vol_target=False)
    vt = StrategyConfig(strategies=[StrategySpec(id="macd")], regime_gate=False, vol_target=True, target_annual_vol=0.10)
    e0, _ = compute_target_exposure(base, b)
    e1, w1 = compute_target_exposure(vt, b)
    assert np.all(e1[w1:] <= e0[w1:] + 1e-12)
    assert np.any(e1[w1:] < e0[w1:])
    blend = StrategyConfig(strategies=[StrategySpec(id="macd"), StrategySpec(id="ema_cross")], regime_gate=False, vol_target=False)
    eb, wb = compute_target_exposure(blend, b)
    ee, _ = compute_target_exposure(StrategyConfig(strategies=[StrategySpec(id="ema_cross")], regime_gate=False, vol_target=False), b)
    np.testing.assert_allclose(eb[wb:], (e0[wb:] + ee[wb:]) / 2)


def test_dca_schedule_and_no_stops():
    b = synthetic(100)
    e = STRATEGIES["dca"]().compute(b, {"interval": 7, "purchases": 4})
    assert e[0] == 0.25 and e[6] == 0.25 and e[7] == 0.5 and e[21] == 1.0 and e[-1] == 1.0
    cfg = StrategyConfig(strategies=[StrategySpec(id="dca")])
    assert cfg.effective_risk().stop_atr_mult == 0
    assert StrategyConfig(strategies=[StrategySpec(id="ema_cross")]).effective_risk().stop_atr_mult == 2.0


def test_grid_floor_stop_and_ladder_direction():
    b = synthetic(200, seed=5)
    g = STRATEGIES["grid"]()
    p = g.defaults()
    e = g.compute(b, p)
    anchor = ind.shift(ind.sma(b.close, 50), 1)
    ok = ~np.isnan(e)
    # higher price relative to anchor -> lower exposure
    ratio = b.close / anchor
    hi, lo = ratio[ok] > 1.08, ratio[ok] < 0.92
    assert e[ok][hi].mean() < e[ok][lo].mean()
    assert np.all(e[ok][b.close[ok] < anchor[ok] * 0.9 * 0.9] == 0)  # below the floor: out

