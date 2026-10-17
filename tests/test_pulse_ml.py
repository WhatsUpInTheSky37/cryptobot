"""Machine learning: chronological walk-forward with embargo, leakage prevention, retain only on improvement."""
import numpy as np
import pytest

from cryptobot.pulse import ml
from cryptobot.pulse.memory import KEYS

T0 = 1_760_000_000_000


def synth(n: int, signal: float, seed: int = 0, step_ms: int = 600_000):
    """Events where 'f' predicts the outcome with strength ``signal`` (0 = pure noise)."""
    rng = np.random.default_rng(seed)
    out = []
    for i in range(n):
        f = rng.uniform(0, 100)
        p = 0.5 + signal * (f - 50) / 100
        hit = rng.uniform() < p
        e = {k: float(rng.uniform(0, 100)) for k in KEYS}
        e.update({"f": f, "vol_pct": 0.2, "spread_pct": 0.01, "imbalance": 0.3, "ts_ms": T0 + i * step_ms, "regime": "range", "prs": float(rng.uniform(60, 100)), "confirmed": bool(i % 2),
                  "outcome": "target" if hit else "stop", "first_hit": "target" if hit else "stop", "net_pct": 0.3 if hit else -0.4})
        out.append(e)
    return out


def test_refuses_to_train_on_too_little_data():
    r = ml.walk_forward(synth(100, 0.8))
    assert not r.ok and "need at least" in r.reason
    m = ml.train(synth(100, 0.8))
    assert not m.usable and m.p({"f": 90}) is None


def test_walk_forward_uses_chronological_folds_with_embargo():
    events = synth(400, 0.9)
    seen = []

    class Spy(ml.LogitModel):
        def fit(self, X, y):
            seen.append(len(y))
            return super().fit(X, y)

    r = ml.walk_forward(events, model_factory=Spy, n_folds=4, min_train=100)
    assert r.ok and len(r.folds) >= 3
    assert seen == sorted(seen)  # expanding training windows
    assert all(f.auc > 0.6 for f in r.folds), [f.auc for f in r.folds]
    # embargo: the training set ends at least one horizon before the test set starts
    X, y, net, ts, rule = ml.build_dataset(events)
    for f in r.folds:
        assert ts[f.train_n - 1] < f.test_start_ms - ml.HORIZON_MS


def test_noise_model_is_not_retained_and_signal_model_is():
    noise = ml.train(synth(400, 0.0, seed=3))
    assert noise.report.ok and not noise.report.improved and not noise.usable
    strong = ml.train(synth(400, 0.95, seed=4))
    assert strong.report.ok and strong.report.improved and strong.usable
    p_hi = strong.p({"f": 95, "regime": "range", "vol_pct": 0.2, "spread_pct": 0.01, "imbalance": 0.3})
    p_lo = strong.p({"f": 5, "regime": "range", "vol_pct": 0.2, "spread_pct": 0.01, "imbalance": 0.3})
    assert p_hi > 0.7 > 0.3 > p_lo
    # round-trips through JSON (what the engine persists)
    again = ml.PulseModel.from_json(strong.to_json())
    assert again is not None and again.usable and again.p({"f": 95, "regime": "range", "vol_pct": 0.2, "spread_pct": 0.01, "imbalance": 0.3}) == pytest.approx(p_hi, abs=1e-9)


def test_dataset_excludes_unresolved_events():
    events = synth(10, 0.5)
    events[0]["outcome"] = "pending"
    events[1]["net_pct"] = None
    X, y, net, ts, rule = ml.build_dataset(events)
    assert len(y) == 8 and X.shape[1] == len(ml.FEATURES)
