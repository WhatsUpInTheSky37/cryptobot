"""Optional machine learning for Mode C.

Trains a classifier for "target before stop" (and regressors for net return
and adverse excursion) on resolved Market Memory events, validates it
walk-forward with chronological splits and an embargo equal to the outcome
horizon so overlapping windows cannot leak, and compares it with the
rule-based gate on the same out-of-sample folds. The model is kept only if it
improves out-of-sample net return per taken trade. LightGBM is used when
installed; otherwise a small numpy logistic regression so the pipeline works
everywhere (and in tests).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from cryptobot.pulse.config import OUTCOME_HORIZONS_MS
from cryptobot.pulse.memory import KEYS
from cryptobot.pulse.regime import REGIMES

FEATURES: tuple[str, ...] = KEYS + tuple(f"regime_{r}" for r in REGIMES) + ("target_pct", "stop_pct", "horizon_minutes", "is_short")
MIN_EVENTS = 300
MIN_POSITIVES = 50
HORIZON_MS = max(OUTCOME_HORIZONS_MS.values())

try:  # pragma: no cover - depends on the environment
    import lightgbm  # type: ignore

    HAVE_LGBM = True
except Exception:  # noqa: BLE001
    lightgbm = None
    HAVE_LGBM = False


def featurize(e: dict[str, Any]) -> list[float]:
    vec = [float(e.get(k) or 0.0) for k in KEYS]
    reg = e.get("regime") or "unknown"
    vec += [1.0 if reg == r else 0.0 for r in REGIMES]
    vec += [float(e.get("target_pct") or 0), float(e.get("stop_pct") or 0), float(e.get("horizon_ms") or HORIZON_MS) / 60_000, float(e.get("direction") == "short")]
    return vec


class LogitModel:
    """Standardised L2-regularised logistic regression by gradient descent (no dependencies)."""

    def __init__(self, l2: float = 1.0, iters: int = 400, lr: float = 0.1) -> None:
        self.l2, self.iters, self.lr = l2, iters, lr
        self.mu: np.ndarray | None = None
        self.sd: np.ndarray | None = None
        self.w: np.ndarray | None = None
        self.b = 0.0

    def fit(self, X: np.ndarray, y: np.ndarray) -> "LogitModel":
        self.mu = X.mean(axis=0)
        self.sd = X.std(axis=0) + 1e-9
        Z = (X - self.mu) / self.sd
        n, d = Z.shape
        self.w = np.zeros(d)
        self.b = float(np.log((y.mean() + 1e-6) / (1 - y.mean() + 1e-6)))
        for _ in range(self.iters):
            p = 1 / (1 + np.exp(-(Z @ self.w + self.b)))
            g = Z.T @ (p - y) / n + self.l2 * self.w / n
            self.w -= self.lr * g
            self.b -= self.lr * float((p - y).mean())
        return self

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        Z = (X - self.mu) / self.sd
        return 1 / (1 + np.exp(-(Z @ self.w + self.b)))

    def to_json(self) -> dict[str, Any]:
        return {"kind": "logit", "mu": self.mu.tolist(), "sd": self.sd.tolist(), "w": self.w.tolist(), "b": self.b}

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "LogitModel":
        m = cls()
        m.mu, m.sd, m.w, m.b = np.array(d["mu"]), np.array(d["sd"]), np.array(d["w"]), float(d["b"])
        return m


def make_model():
    if HAVE_LGBM:  # pragma: no cover
        return _LGBM()
    return LogitModel()


class _LGBM:  # pragma: no cover - only when lightgbm is installed
    def __init__(self) -> None:
        self.m = lightgbm.LGBMClassifier(n_estimators=200, learning_rate=0.03, num_leaves=15, min_child_samples=20, subsample=0.8, colsample_bytree=0.8, verbose=-1)

    def fit(self, X, y):
        self.m.fit(X, y)
        return self

    def predict_proba(self, X):
        return self.m.predict_proba(X)[:, 1]

    def to_json(self) -> dict[str, Any]:
        return {"kind": "lightgbm", "model": self.m.booster_.model_to_string()}

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "_LGBM":
        m = cls.__new__(cls)
        booster = lightgbm.Booster(model_str=d["model"])
        m.m = type("B", (), {"predict_proba": lambda self, X: np.c_[1 - booster.predict(X), booster.predict(X)]})()
        return m


@dataclass
class Fold:
    train_n: int
    test_n: int
    auc: float
    logloss: float
    model_trades: int
    model_net_pct: float  # mean net return of trades the model would take
    rule_trades: int
    rule_net_pct: float
    base_rate: float
    test_start_ms: int = 0


@dataclass
class Report:
    ok: bool
    reason: str
    events: int = 0
    positives: int = 0
    folds: list[Fold] = field(default_factory=list)
    improved: bool = False
    threshold: float = 0.5
    backend: str = "logit"
    trained_at_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "reason": self.reason, "events": self.events, "positives": self.positives, "improved": self.improved, "threshold": self.threshold,
                "backend": self.backend, "trained_at_ms": self.trained_at_ms, "folds": [f.__dict__ for f in self.folds]}


def auc(y: np.ndarray, p: np.ndarray) -> float:
    pos, neg = p[y == 1], p[y == 0]
    if len(pos) == 0 or len(neg) == 0:
        return 0.5
    values = np.concatenate([pos, neg])
    order = np.argsort(values, kind="stable")
    ranks = np.empty(len(order))
    ranks[order] = np.arange(1, len(order) + 1)
    for value in np.unique(values):
        tied = values == value
        ranks[tied] = ranks[tied].mean()
    return float((ranks[: len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def logloss(y: np.ndarray, p: np.ndarray) -> float:
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def build_dataset(events: list[dict[str, Any]]) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """(X, y_target, net_pct, ts_ms, rule_ok) from resolved events, chronological."""
    rows = sorted((e for e in events if e.get("outcome") in ("target", "stop", "expired") and e.get("net_pct") is not None), key=lambda e: e["ts_ms"])
    X = np.array([featurize(e) for e in rows], dtype=float) if rows else np.zeros((0, len(FEATURES)))
    y = np.array([1.0 if (e.get("first_hit") or e.get("outcome")) == "target" else 0.0 for e in rows])
    net = np.array([float(e["net_pct"]) for e in rows])
    ts = np.array([int(e["ts_ms"]) for e in rows])
    rule = np.array([1.0 if (e.get("prs") or 0) >= 85 and e.get("confirmed") else 0.0 for e in rows])
    return X, y, net, ts, rule


def walk_forward(events: list[dict[str, Any]], *, n_folds: int = 4, min_train: int = 150, embargo_ms: int = HORIZON_MS, threshold: float = 0.55, model_factory=make_model) -> Report:
    X, y, net, ts, rule = build_dataset(events)
    n = len(y)
    if n < MIN_EVENTS:
        return Report(False, f"need at least {MIN_EVENTS} resolved signals, have {n}", n, int(y.sum()))
    if y.sum() < MIN_POSITIVES or (n - y.sum()) < MIN_POSITIVES:
        return Report(False, f"need at least {MIN_POSITIVES} of each outcome, have {int(y.sum())} hits / {int(n - y.sum())} misses", n, int(y.sum()))
    folds: list[Fold] = []
    edges = np.linspace(min_train, n, n_folds + 1).astype(int)
    for i in range(n_folds):
        tr_end, te_end = edges[i], edges[i + 1]
        if te_end - tr_end < 20:
            continue
        # embargo: drop training rows whose outcome window overlaps the test start
        cutoff = ts[tr_end] - embargo_ms
        tr = np.where(ts[:tr_end] < cutoff)[0]
        te = np.arange(tr_end, te_end)
        if len(tr) < min_train or y[tr].sum() < 5 or (len(tr) - y[tr].sum()) < 5:
            continue
        m = model_factory().fit(X[tr], y[tr])
        p = np.asarray(m.predict_proba(X[te]))
        take = p >= threshold
        rk = rule[te] == 1
        folds.append(Fold(len(tr), len(te), auc(y[te], p), logloss(y[te], p), int(take.sum()), float(net[te][take].mean()) if take.any() else 0.0,
                          int(rk.sum()), float(net[te][rk].mean()) if rk.any() else 0.0, float(y[te].mean()), int(ts[tr_end])))
    if not folds:
        return Report(False, "not enough data for a walk-forward split", n, int(y.sum()))
    model_net = sum(f.model_net_pct * f.model_trades for f in folds) / max(1, sum(f.model_trades for f in folds))
    rule_net = sum(f.rule_net_pct * f.rule_trades for f in folds) / max(1, sum(f.rule_trades for f in folds))
    total_model_trades = sum(f.model_trades for f in folds)
    improved = total_model_trades >= 20 and model_net > rule_net and model_net > 0 and np.mean([f.auc for f in folds]) > 0.55
    reason = (f"out-of-sample net per trade: model {model_net:+.3f}% ({total_model_trades} trades) vs rules {rule_net:+.3f}%; mean AUC {np.mean([f.auc for f in folds]):.2f}")
    return Report(True, reason, n, int(y.sum()), folds, bool(improved), threshold, "lightgbm" if HAVE_LGBM else "logit", int(time.time() * 1000))


class PulseModel:
    """A validated model plus its report. ``p`` returns None unless the report says the model improved OOS."""

    def __init__(self, model, report: Report) -> None:
        self.model = model
        self.report = report

    @property
    def usable(self) -> bool:
        return self.report.ok and self.report.improved

    def p(self, event_like: dict[str, Any]) -> float | None:
        if not self.usable:
            return None
        x = np.array([featurize(event_like)], dtype=float)
        return float(np.asarray(self.model.predict_proba(x))[0])

    def to_json(self) -> str:
        return json.dumps({"model": self.model.to_json(), "report": self.report.as_dict()})

    @classmethod
    def from_json(cls, s: str) -> "PulseModel | None":
        try:
            d = json.loads(s)
            md = d["model"]
            model = _LGBM.from_json(md) if md.get("kind") == "lightgbm" and HAVE_LGBM else LogitModel.from_json(md) if md.get("kind") == "logit" else None
            if model is None:
                return None
            r = d["report"]
            rep = Report(r["ok"], r["reason"], r["events"], r["positives"], [Fold(**f) for f in r.get("folds", [])], r["improved"], r.get("threshold", 0.5), r.get("backend", "logit"), r.get("trained_at_ms", 0))
            return cls(model, rep)
        except Exception:  # noqa: BLE001
            return None


def train(events: list[dict[str, Any]], **kw) -> PulseModel:
    """Walk-forward validate, then fit on everything. The result is usable only if validation improved."""
    report = walk_forward(events, **kw)
    if not report.ok:
        return PulseModel(None, report)
    X, y, _net, _ts, _rule = build_dataset(events)
    model = (kw.get("model_factory") or make_model)().fit(X, y)
    return PulseModel(model, report)

