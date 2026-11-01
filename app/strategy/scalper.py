"""Classic scalp set: EMA 9/21 cross + EMA 50 trend bias + RSI momentum gate + ATR stops.

Entry (LONG):   EMA9 crosses above EMA21 on the last CLOSED bar,
                close > EMA50 (trend filter), RSI in [rsi_long_min, rsi_long_max],
                optional: close > VWAP, Supertrend bullish, ATR% and spread checks.
Entry (SHORT):  mirror.
Exit:           SL = entry -/+ ATR*sl_mult,  TP = SL distance * tp_rr,
                trailing SL managed by the engine; optional exit on opposite EMA cross.
"""
from __future__ import annotations

from typing import List

import numpy as np

from ..config import SymbolConfig
from ..models import Candle, Signal
from . import indicators as ind


def evaluate(candles: List[Candle], cfg: SymbolConfig, spread: float = 0.0,
             use_last_closed: bool = True) -> Signal:
    """Evaluate the strategy on a candle list.

    use_last_closed=True ignores the still-forming last candle (live trading).
    The backtester passes use_last_closed=False with an already-closed slice.
    """
    need = max(cfg.ema_trend, cfg.ema_slow, cfg.rsi_period, cfg.atr_period, cfg.supertrend_period) + 3
    cs = candles[:-1] if use_last_closed else candles
    if len(cs) < need:
        return Signal(None, "warming-up", 0, cs[-1].close if cs else 0, 0, 0, 0, 50)

    close = np.array([c.close for c in cs])
    high = np.array([c.high for c in cs])
    low = np.array([c.low for c in cs])
    vol = np.array([c.volume for c in cs])

    ef = ind.ema(close, cfg.ema_fast)
    es = ind.ema(close, cfg.ema_slow)
    et = ind.ema(close, cfg.ema_trend)
    r = ind.rsi(close, cfg.rsi_period)
    a = ind.atr(high, low, close, cfg.atr_period)

    i = len(close) - 1
    sig = Signal(None, "", float(a[i]), float(close[i]), float(ef[i]), float(es[i]), float(et[i]), float(r[i]))
    if np.isnan(a[i]) or np.isnan(et[i]) or np.isnan(r[i]):
        sig.reason = "warming-up"
        return sig

    cross_up = ef[i - 1] <= es[i - 1] and ef[i] > es[i]
    cross_dn = ef[i - 1] >= es[i - 1] and ef[i] < es[i]
    sig.exit_long = bool(cross_dn) and cfg.exit_on_opposite_cross
    sig.exit_short = bool(cross_up) and cfg.exit_on_opposite_cross

    # ---- hard filters ---------------------------------------------------- #
    if cfg.min_atr_pct and a[i] / close[i] * 100 < cfg.min_atr_pct:
        sig.reason = "atr too low"
        return sig
    if spread and a[i] > 0 and spread > cfg.max_spread_atr * a[i]:
        sig.reason = f"spread {spread:.5g} > {cfg.max_spread_atr}×ATR"
        return sig

    trend_up = close[i] > et[i]
    trend_dn = close[i] < et[i]
    if cfg.use_vwap_filter:
        day = np.array([c.time.date().toordinal() for c in cs])
        v = ind.vwap(high, low, close, vol, day)
        vw_up, vw_dn = close[i] > v[i], close[i] < v[i]
    else:
        vw_up = vw_dn = True
    if cfg.use_supertrend_filter:
        _, d = ind.supertrend(high, low, close, cfg.supertrend_period, cfg.supertrend_mult)
        st_up, st_dn = d[i] == 1, d[i] == -1
    else:
        st_up = st_dn = True

    long_ok = cross_up and (trend_up or not cfg.use_trend_filter) and vw_up and st_up \
        and cfg.rsi_long_min <= r[i] <= cfg.rsi_long_max
    short_ok = cross_dn and (trend_dn or not cfg.use_trend_filter) and vw_dn and st_dn \
        and cfg.rsi_short_min <= r[i] <= cfg.rsi_short_max

    if long_ok:
        sig.side, sig.reason = "buy", f"EMA{cfg.ema_fast}>{cfg.ema_slow} cross, RSI {r[i]:.0f}"
    elif short_ok:
        sig.side, sig.reason = "sell", f"EMA{cfg.ema_fast}<{cfg.ema_slow} cross, RSI {r[i]:.0f}"
    elif cross_up or cross_dn:
        sig.reason = "cross but filters blocked"
    else:
        sig.reason = "no setup"
    return sig


def stops_for(side: str, entry: float, atr: float, cfg: SymbolConfig, digits: int):
    dist = atr * cfg.sl_atr_mult
    if side == "buy":
        sl, tp = entry - dist, entry + dist * cfg.tp_rr
    else:
        sl, tp = entry + dist, entry - dist * cfg.tp_rr
    return round(sl, digits), round(tp, digits), dist


def trail_level(side: str, pos_entry: float, initial_risk: float, extreme: float, current_sl: float,
                atr: float, cfg: SymbolConfig, digits: int):
    """Compute a new (higher for long / lower for short) stop, or None if no change.

    1. once profit >= breakeven_r * R  → SL at least break-even (+10% of risk buffer)
    2. once profit >= trail_trigger_r * R → chandelier trail: extreme -/+ ATR*trail_atr_mult
    3. move only if improvement >= trail_step_atr * ATR (avoids spamming the broker)
    """
    if initial_risk <= 0 or atr <= 0:
        return None
    profit = (extreme - pos_entry) if side == "buy" else (pos_entry - extreme)
    r = profit / initial_risk
    candidates = []
    if r >= cfg.breakeven_r:
        be = pos_entry + initial_risk * 0.1 if side == "buy" else pos_entry - initial_risk * 0.1
        candidates.append(be)
    if r >= cfg.trail_trigger_r:
        tr = extreme - atr * cfg.trail_atr_mult if side == "buy" else extreme + atr * cfg.trail_atr_mult
        candidates.append(tr)
    if not candidates:
        return None
    new = max(candidates) if side == "buy" else min(candidates)
    step = atr * cfg.trail_step_atr
    if side == "buy" and (current_sl is None or new - current_sl >= step):
        return round(new, digits)
    if side == "sell" and (current_sl is None or current_sl - new >= step):
        return round(new, digits)
    return None
