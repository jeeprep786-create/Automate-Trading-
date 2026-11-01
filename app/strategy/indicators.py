"""Vectorised indicators (numpy only) — EMA, RSI (Wilder), ATR, VWAP, Supertrend."""
from __future__ import annotations

import numpy as np


def ema(x: np.ndarray, period: int) -> np.ndarray:
    out = np.empty_like(x, dtype=float)
    out[:] = np.nan
    if len(x) < period:
        return out
    k = 2.0 / (period + 1)
    out[period - 1] = x[:period].mean()
    for i in range(period, len(x)):
        out[i] = x[i] * k + out[i - 1] * (1 - k)
    return out


def rsi(close: np.ndarray, period: int = 14) -> np.ndarray:
    out = np.full(len(close), np.nan)
    if len(close) <= period:
        return out
    d = np.diff(close)
    gain = np.where(d > 0, d, 0.0)
    loss = np.where(d < 0, -d, 0.0)
    ag = gain[:period].mean()
    al = loss[:period].mean()
    out[period] = 100 - 100 / (1 + ag / al) if al else 100.0
    for i in range(period + 1, len(close)):
        ag = (ag * (period - 1) + gain[i - 1]) / period
        al = (al * (period - 1) + loss[i - 1]) / period
        out[i] = 100 - 100 / (1 + ag / al) if al else 100.0
    return out


def true_range(high, low, close) -> np.ndarray:
    prev = np.roll(close, 1)
    prev[0] = close[0]
    return np.maximum(high - low, np.maximum(abs(high - prev), abs(low - prev)))


def atr(high, low, close, period: int = 14) -> np.ndarray:
    tr = true_range(high, low, close)
    out = np.full(len(close), np.nan)
    if len(close) < period:
        return out
    out[period - 1] = tr[:period].mean()
    for i in range(period, len(close)):
        out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return out


def vwap(high, low, close, volume, session_ids: np.ndarray | None = None) -> np.ndarray:
    """Session VWAP (resets whenever session_ids changes). Without ids: rolling cumulative."""
    tp = (high + low + close) / 3.0
    out = np.full(len(close), np.nan)
    cum_pv = cum_v = 0.0
    last = None
    for i in range(len(close)):
        sid = session_ids[i] if session_ids is not None else 0
        if sid != last:
            cum_pv = cum_v = 0.0
            last = sid
        v = volume[i] if volume[i] > 0 else 1.0
        cum_pv += tp[i] * v
        cum_v += v
        out[i] = cum_pv / cum_v
    return out


def supertrend(high, low, close, period: int = 10, mult: float = 3.0):
    """Returns (line, direction) with direction +1 bullish / -1 bearish."""
    a = atr(high, low, close, period)
    hl2 = (high + low) / 2
    upper = hl2 + mult * a
    lower = hl2 - mult * a
    n = len(close)
    line = np.full(n, np.nan)
    direction = np.zeros(n, dtype=int)
    fu, fl = upper.copy(), lower.copy()
    for i in range(1, n):
        if np.isnan(a[i]):
            continue
        fl[i] = lower[i] if (lower[i] > fl[i - 1] or close[i - 1] < fl[i - 1]) else fl[i - 1]
        fu[i] = upper[i] if (upper[i] < fu[i - 1] or close[i - 1] > fu[i - 1]) else fu[i - 1]
        if direction[i - 1] <= 0:
            direction[i] = 1 if close[i] > fu[i - 1] else -1
        else:
            direction[i] = -1 if close[i] < fl[i - 1] else 1
        line[i] = fl[i] if direction[i] == 1 else fu[i]
    return line, direction
