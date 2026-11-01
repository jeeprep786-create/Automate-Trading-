"""Bar-by-bar backtester that reuses the live strategy + trailing-stop code.

Fills: entries at next bar open (+/- spread), SL/TP checked against bar high/low
(SL takes priority when both are hit in one bar — conservative). Trailing SL is
updated once per closed bar using that bar's extreme.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import List, Optional

from ..config import SymbolConfig
from ..models import Candle, SymbolSpec
from ..strategy import evaluate, stops_for, trail_level
from ..engine.risk import lots_for_risk


@dataclass
class BTTrade:
    side: str
    entry_time: str
    exit_time: str
    entry: float
    exit: float
    volume: float
    pnl: float
    r: float
    reason: str


def run_backtest(candles: List[Candle], cfg: SymbolConfig, spec: SymbolSpec, balance: float = 10000.0,
                 spread: float = 0.0, warmup: Optional[int] = None) -> dict:
    warmup = warmup or max(cfg.ema_trend, cfg.ema_slow, cfg.rsi_period, cfg.atr_period, cfg.supertrend_period) + 5
    trades: List[BTTrade] = []
    equity = [balance]
    eq_times = [candles[warmup].time.isoformat()] if len(candles) > warmup else []
    pos = None
    cooldown = 0
    half = spread / 2

    for i in range(warmup, len(candles) - 1):
        bar = candles[i]
        nxt = candles[i + 1]
        window = candles[max(0, i - 300): i + 1]
        sig = evaluate(window, cfg, spread=spread, use_last_closed=False)

        # ---- manage open position on bar i+1 ------------------------------ #
        if pos:
            # trailing update based on extremes so far (through bar i)
            extreme = max(pos["extreme"], bar.high) if pos["side"] == "buy" else min(pos["extreme"], bar.low)
            pos["extreme"] = extreme
            new_sl = trail_level(pos["side"], pos["entry"], pos["risk"], extreme, pos["sl"], sig.atr, cfg, spec.digits)
            if new_sl is not None:
                pos["sl"], pos["trailing"] = new_sl, True
            # strategy exit on opposite cross → close at next open
            exit_px = None; reason = None
            if (pos["side"] == "buy" and sig.exit_long) or (pos["side"] == "sell" and sig.exit_short):
                exit_px = nxt.open - half if pos["side"] == "buy" else nxt.open + half
                reason = "signal"
            else:
                # check SL / TP on next bar's range
                if pos["side"] == "buy":
                    if nxt.low - half <= pos["sl"]:
                        exit_px, reason = pos["sl"], ("trail" if pos["trailing"] else "sl")
                    elif nxt.high - half >= pos["tp"]:
                        exit_px, reason = pos["tp"], "tp"
                else:
                    if nxt.high + half >= pos["sl"]:
                        exit_px, reason = pos["sl"], ("trail" if pos["trailing"] else "sl")
                    elif nxt.low + half <= pos["tp"]:
                        exit_px, reason = pos["tp"], "tp"
            if exit_px is not None:
                diff = (exit_px - pos["entry"]) if pos["side"] == "buy" else (pos["entry"] - exit_px)
                pnl = diff * pos["vol"] * spec.contract_size
                balance += pnl
                trades.append(BTTrade(pos["side"], pos["time"], nxt.time.isoformat(), pos["entry"], round(exit_px, spec.digits),
                                      pos["vol"], round(pnl, 2), round(diff / pos["risk"], 2) if pos["risk"] else 0, reason))
                pos = None
                cooldown = cfg.cooldown_bars
                equity.append(round(balance, 2)); eq_times.append(nxt.time.isoformat())
                continue

        # ---- new entry at next bar open ----------------------------------- #
        if cooldown > 0:
            cooldown -= 1
        elif pos is None and sig.side:
            entry = nxt.open + half if sig.side == "buy" else nxt.open - half
            sl, tp, dist = stops_for(sig.side, entry, sig.atr, cfg, spec.digits)
            vol = lots_for_risk(balance, cfg.risk_pct, dist, spec, cfg.fixed_volume)
            if vol > 0:
                pos = {"side": sig.side, "entry": round(entry, spec.digits), "sl": sl, "tp": tp, "risk": dist,
                       "vol": vol, "time": nxt.time.isoformat(), "extreme": entry, "trailing": False}

    return summarize(trades, equity, eq_times, cfg, spec, balance, len(candles))


def summarize(trades: List[BTTrade], equity: List[float], eq_times: List[str], cfg, spec, final_balance, n_bars: int = 0) -> dict:
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    gp, gl = sum(t.pnl for t in wins), -sum(t.pnl for t in losses)
    peak, mdd = equity[0] if equity else 0, 0.0
    for e in equity:
        peak = max(peak, e)
        mdd = max(mdd, (peak - e) / peak * 100 if peak else 0)
    exit_reasons = {}
    for t in trades:
        exit_reasons[t.reason] = exit_reasons.get(t.reason, 0) + 1
    return {
        "symbol": cfg.symbol, "timeframe": cfg.timeframe, "bars": n_bars,
        "trades": len(trades), "wins": len(wins), "losses": len(losses),
        "win_rate": round(100 * len(wins) / len(trades), 1) if trades else 0,
        "net_pnl": round(sum(t.pnl for t in trades), 2),
        "profit_factor": round(gp / gl, 2) if gl else (99.0 if gp else 0),
        "avg_win": round(gp / len(wins), 2) if wins else 0, "avg_loss": round(-gl / len(losses), 2) if losses else 0,
        "avg_r": round(sum(t.r for t in trades) / len(trades), 2) if trades else 0,
        "expectancy_r": round(sum(t.r for t in trades) / len(trades), 3) if trades else 0,
        "max_drawdown_pct": round(mdd, 2), "final_balance": round(final_balance, 2),
        "start_balance": equity[0] if equity else 0,
        "exit_reasons": exit_reasons,
        "equity": equity, "equity_times": eq_times,
        "trade_list": [asdict(t) for t in trades[-200:]],
    }
