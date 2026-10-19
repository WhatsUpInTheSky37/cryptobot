"""Bounded, checkpointable indicators with the same seeds as the batch signals."""
from __future__ import annotations

import math
from dataclasses import asdict

import numpy as np

from cryptobot.core.models import Bar
from cryptobot.strategy.library import STRATEGIES


class OnlineSignals:
    def __init__(self, config, state=None):
        self.config = config
        state = state or {}
        if state.get("config") != config.model_dump():
            state = {}
        self.history = [Bar(**b) for b in state.get("history", [])]
        self.smooth = state.get("smooth", {})
        self.states = state.get("states", {})
        self.last_ts = state.get("last_ts", -1)
        self.count = state.get("count", 0)
        self.exposure = state.get("exposure")
        self.atr = state.get("atr")

    def to_dict(self):
        return dict(config=self.config.model_dump(), history=[asdict(b) for b in self.history], smooth=self.smooth,
                    states=self.states, last_ts=self.last_ts, count=self.count, exposure=self.exposure, atr=self.atr)

    def _average(self, key, x, n, alpha):
        if x is None or not math.isfinite(x):
            return None
        state = self.smooth.setdefault(key, {"seed": [], "value": None, "ts": -1})
        if state["ts"] == self.last_ts:
            return state["value"]
        state["ts"] = self.last_ts
        if state["value"] is None:
            state["seed"].append(x)
            if len(state["seed"]) == n:
                state["value"] = sum(state["seed"]) / n
                state["seed"] = []
        else:
            state["value"] = alpha * x + (1 - alpha) * state["value"]
        return state["value"]

    def _ema(self, name, x, n):
        return self._average(f"ema:{name}:{n}", x, n, 2 / (n + 1))

    def _rma(self, name, x, n):
        return self._average(f"rma:{name}:{n}", x, n, 1 / n)

    def evaluate(self, bars):
        for bar in bars:
            if bar.closed and bar.ts_ms > self.last_ts:
                self._step(bar)
        return self.exposure, self.atr

    def _step(self, bar):
        previous = self.history[-1] if self.history else None
        self.last_ts = bar.ts_ms
        self.count += 1
        tr = max(bar.high - bar.low, abs(bar.high - previous.close), abs(bar.low - previous.close)) if previous else bar.high - bar.low
        self.atr = self._rma("atr", tr, self.config.risk.atr_n)
        adx = None
        if previous:
            up, dn = bar.high - previous.high, previous.low - bar.low
            an = self.config.adx_n
            atr = self._rma("adx-tr", tr, an)
            pdm = self._rma("plus-dm", up if up > dn and up > 0 else 0, an)
            mdm = self._rma("minus-dm", dn if dn > up and dn > 0 else 0, an)
            dx = None if atr is None else 100 * abs(pdm - mdm) / (pdm + mdm) if pdm + mdm else 0
            adx = self._rma("adx", dx, an)
        history = self.history + [bar]
        closes = [b.close for b in history]
        values = []
        for index, spec in enumerate(self.config.strategies):
            params = spec.full_params()
            sid, key = spec.id, str(index)
            state = float(self.states.get(key, 0))
            value = None
            if sid == "ema_cross":
                fast = self._ema(f"{key}-fast", bar.close, int(params["fast"]))
                slow = self._ema(f"{key}-slow", bar.close, int(params["slow"]))
                value = float(fast > slow) if fast is not None and slow is not None else None
            elif sid == "macd":
                fast = self._ema(f"{key}-fast", bar.close, int(params["fast"]))
                slow = self._ema(f"{key}-slow", bar.close, int(params["slow"]))
                line = fast - slow if fast is not None and slow is not None else None
                signal = self._ema(f"{key}-signal", line, int(params["signal"]))
                value = float(line > signal) if line is not None and signal is not None else None
            elif sid == "rsi_reversion":
                if previous:
                    delta, n = bar.close - previous.close, int(params["n"])
                    up = self._rma(f"{key}-up", max(delta, 0), n)
                    down = self._rma(f"{key}-down", max(-delta, 0), n)
                    if up is not None:
                        rsi = 100 if down == 0 else 100 - 100 / (1 + up / down)
                        if rsi < params["buy_below"]:
                            state = 1
                        elif rsi > params["sell_above"]:
                            state = 0
                        value = state
            elif sid == "bollinger_reversion":
                n = int(params["n"])
                if len(closes) >= n:
                    window = closes[-n:]
                    mid = float(np.mean(window))
                    if bar.close < mid - params["k"] * float(np.std(window)):
                        state = 1
                    elif bar.close >= mid:
                        state = 0
                    value = state
            elif sid == "donchian_breakout":
                en, ex = int(params["entry"]), int(params["exit"])
                if len(self.history) >= max(en, ex, 20):
                    high = max(b.high for b in self.history[-en:])
                    low = min(b.low for b in self.history[-ex:])
                    volume = sum(b.volume for b in self.history[-20:]) / 20
                    if not state and bar.close > high and (params["vol_mult"] <= 0 or bar.volume >= params["vol_mult"] * volume):
                        state = 1
                    elif state and bar.close < low:
                        state = 0
                    value = state
            elif sid == "grid":
                n = int(params["anchor"])
                if len(self.history) >= n:
                    anchor = sum(b.close for b in self.history[-n:]) / n
                    half = params["range_pct"] / 200
                    upper, lower = anchor * (1 + half), anchor * (1 - half)
                    fraction = min(1, max(0, (upper - bar.close) / (upper - lower)))
                    value = float(np.round(fraction * params["levels"]) / params["levels"])
                    if bar.close < lower * (1 - params["floor_pct"] / 100):
                        value = 0
            elif sid == "dca":
                value = min((self.count - 1) // int(params["interval"]) + 1, params["purchases"]) / params["purchases"]
            self.states[key] = state
            kind = STRATEGIES[sid].kind
            if self.config.regime_gate and kind != "schedule":
                if adx is None or value is None:
                    value = None
                elif not (adx >= self.config.adx_threshold if kind == "trend" else adx < self.config.adx_threshold):
                    value = 0
            values.append(value)
        exposure = None if any(v is None for v in values) else sum(values) / len(values)
        if self.config.vol_target and not all(STRATEGIES[s.id].kind == "schedule" for s in self.config.strategies):
            n = self.config.vol_window
            if len(closes) < n + 1:
                exposure = None
            elif exposure is not None:
                vol = float(np.std(np.diff(np.log(closes[-n - 1:])))) * math.sqrt(365 * 86400 / self.config.timeframe_s)
                exposure *= min(1, self.config.target_annual_vol / vol) if vol > 0 else 1
        self.exposure = min(1, max(0, exposure)) if exposure is not None else None
        self.history = history[-600:]
