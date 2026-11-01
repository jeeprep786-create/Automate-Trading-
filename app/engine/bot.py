"""The trading engine: polls prices, evaluates the strategy, places and manages trades."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Deque, Dict, List, Optional

from ..config import DATA_DIR, AppConfig, SymbolConfig
from ..broker.base import Broker
from ..models import Position, Signal, Trade, utcnow
from ..strategy import evaluate, stops_for, trail_level
from . import risk as riskmod

log = logging.getLogger("engine")
TRADES_PATH = DATA_DIR / "trades.json"
EQUITY_PATH = DATA_DIR / "equity.json"


class Engine:
    def __init__(self, broker: Broker, config: AppConfig):
        self.broker = broker
        self.config = config
        self.running = False
        self.killed = False
        self.halt_reason: Optional[str] = None
        self.started_at: Optional[datetime] = None
        self.last_tick: Optional[datetime] = None
        self.trades: List[Trade] = self._load_trades()
        self.equity_curve: Deque[dict] = deque(self._load_equity(), maxlen=5000)
        self.events: Deque[dict] = deque(maxlen=300)
        self.symbol_state: Dict[str, dict] = {}
        self._cooldown_until: Dict[str, datetime] = {}
        self._last_bar: Dict[str, datetime] = {}
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    # ---- persistence ----------------------------------------------------- #
    @staticmethod
    def _load_trades() -> List[Trade]:
        if not TRADES_PATH.exists():
            return []
        try:
            out = []
            for d in json.loads(TRADES_PATH.read_text()):
                d["open_time"] = datetime.fromisoformat(d["open_time"])
                d["close_time"] = datetime.fromisoformat(d["close_time"])
                out.append(Trade(**d))
            return out
        except Exception:
            return []

    @staticmethod
    def _load_equity() -> list:
        try:
            return json.loads(EQUITY_PATH.read_text()) if EQUITY_PATH.exists() else []
        except Exception:
            return []

    def _save(self) -> None:
        TRADES_PATH.write_text(json.dumps([t.to_dict() for t in self.trades[-2000:]], indent=1))
        EQUITY_PATH.write_text(json.dumps(list(self.equity_curve)))

    def log_event(self, level: str, msg: str, symbol: str = "") -> None:
        self.events.appendleft({"time": utcnow().isoformat(), "level": level, "symbol": symbol, "msg": msg})
        getattr(log, level if level in ("info", "warning", "error") else "info")("%s %s", symbol, msg)

    # ---- control --------------------------------------------------------- #
    async def start(self) -> None:
        if self.running:
            return
        self.killed = False
        self.halt_reason = None
        self.running = True
        self.started_at = utcnow()
        self._task = asyncio.create_task(self._loop())
        self.log_event("info", f"Engine started in {self.config.mode.upper()} mode")

    async def stop(self, reason: str = "stopped by user") -> None:
        self.running = False
        self.halt_reason = reason
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        self.log_event("warning", f"Engine stopped: {reason}")

    async def kill(self) -> None:
        """Emergency: close everything and stop."""
        self.killed = True
        await self.stop("KILL SWITCH")
        for p in await self.broker.positions():
            t = await self.broker.close_position(p.id, "kill")
            if t:
                self._record(t)
        self.log_event("error", "KILL SWITCH: all positions closed")

    async def close_one(self, position_id: str) -> Optional[Trade]:
        t = await self.broker.close_position(position_id, "manual")
        if t:
            self._record(t)
        return t

    # ---- main loop ------------------------------------------------------- #
    async def _loop(self) -> None:
        while self.running:
            t0 = time.monotonic()
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.log_event("error", f"tick error: {e!r}")
            dt = max(0.5, self.config.poll_seconds - (time.monotonic() - t0))
            await asyncio.sleep(dt)

    async def tick(self) -> None:
        async with self._lock:
            self.last_tick = utcnow()
            positions = await self.broker.positions()
            for t in await self.broker.closed_trades():
                self._record(t)
            acct = await self.broker.account()
            self.equity_curve.append({"t": self.last_tick.isoformat(), "balance": acct.balance, "equity": acct.equity})

            # ---- account-level guards -------------------------------------- #
            n_today, pnl_today = riskmod.today_stats(self.trades)
            daily_limit = -acct.balance * self.config.risk.max_daily_loss_pct / 100
            can_open = True
            if pnl_today <= daily_limit:
                can_open = False
                if self.halt_reason != "daily loss limit":
                    self.halt_reason = "daily loss limit"
                    self.log_event("warning", f"Daily loss limit hit ({pnl_today:.2f}); no new entries today")
            elif n_today >= self.config.risk.max_daily_trades:
                can_open = False
                self.halt_reason = "daily trade cap"
            elif not riskmod.in_trading_window(self.config.risk):
                can_open = False
                self.halt_reason = "outside trading hours"
            elif len(positions) >= self.config.risk.max_open_positions:
                can_open = False
            else:
                self.halt_reason = None

            for scfg in self.config.symbols:
                if not scfg.enabled:
                    continue
                try:
                    await self._process_symbol(scfg, positions, acct.balance, can_open)
                except Exception as e:
                    self.log_event("error", f"{e!r}", scfg.symbol)
            if len(self.equity_curve) % 20 == 0:
                self._save()

    async def _process_symbol(self, scfg: SymbolConfig, positions: List[Position], balance: float,
                              can_open: bool) -> None:
        sym = scfg.symbol
        candles = await self.broker.candles(sym, scfg.timeframe, 300)
        if not candles:
            self.symbol_state[sym] = {"status": "no data"}
            return
        quote = await self.broker.quote(sym)
        spec = await self.broker.spec(sym)
        sig = evaluate(candles, scfg, spread=quote.spread)
        open_here = [p for p in positions if p.symbol == sym]

        # ---- manage open positions (trailing / signal exits) ------------- #
        for p in open_here:
            if (p.side == "buy" and sig.exit_long) or (p.side == "sell" and sig.exit_short):
                t = await self.broker.close_position(p.id, "signal")
                if t:
                    self._record(t)
                    self.log_event("info", f"Closed {p.side} on opposite cross: {t.pnl:+.2f}", sym)
                continue
            extreme = p.highest_since_entry if p.side == "buy" else p.lowest_since_entry
            if p.side == "buy":
                extreme = max(extreme, quote.bid)
            else:
                extreme = min(extreme, quote.ask)
            new_sl = trail_level(p.side, p.entry_price, p.initial_risk, extreme, p.stop_loss,
                                 sig.atr, scfg, spec.digits)
            if new_sl is not None:
                await self.broker.modify(p.id, new_sl, p.take_profit)
                p.trailing_active = True
                self.log_event("info", f"Trail SL → {new_sl}", sym)

        # ---- new entry ------------------------------------------------------ #
        bar_time = candles[-2].time if len(candles) > 1 else candles[-1].time
        new_bar = self._last_bar.get(sym) != bar_time
        self._last_bar[sym] = bar_time
        cooldown = self._cooldown_until.get(sym)
        in_cooldown = cooldown is not None and utcnow() < cooldown
        status = sig.reason
        if sig.side and new_bar and can_open and not in_cooldown and len(open_here) < scfg.max_positions:
            entry = quote.ask if sig.side == "buy" else quote.bid
            sl, tp, dist = stops_for(sig.side, entry, sig.atr, scfg, spec.digits)
            lots = riskmod.lots_for_risk(balance, scfg.risk_pct, dist, spec, scfg.fixed_volume)
            if lots <= 0:
                status = "size = 0 (check balance / risk%)"
            else:
                pos = await self.broker.open_market(sym, sig.side, lots, sl, tp, comment="IITianScalper")
                self.log_event("info", f"OPEN {sig.side.upper()} {lots} @ {pos.entry_price} SL {sl} TP {tp} — {sig.reason}", sym)
                status = f"in {sig.side}"
        elif sig.side and in_cooldown:
            status = "cooldown"
        elif open_here:
            status = f"in {open_here[0].side} ({open_here[0].unrealized_pnl:+.2f})"

        self.symbol_state[sym] = {
            "status": status, "bid": quote.bid, "ask": quote.ask, "spread": round(quote.spread, spec.digits),
            "atr": round(sig.atr, spec.digits), "rsi": round(sig.rsi, 1),
            "ema_fast": round(sig.ema_fast, spec.digits), "ema_slow": round(sig.ema_slow, spec.digits),
            "ema_trend": round(sig.ema_trend, spec.digits), "trend": "up" if sig.close > sig.ema_trend else "down",
            "bar": bar_time.isoformat(), "tf": scfg.timeframe, "positions": len(open_here),
        }

    def _record(self, t: Trade) -> None:
        self.trades.append(t)
        secs = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}
        scfg = self.config.symbol(t.symbol)
        if scfg and scfg.cooldown_bars > 0:
            from datetime import timedelta
            self._cooldown_until[t.symbol] = utcnow() + timedelta(seconds=secs.get(scfg.timeframe, 300) * scfg.cooldown_bars)
        self.log_event("info" if t.pnl >= 0 else "warning",
                       f"CLOSED {t.side} {t.volume} @ {t.exit_price} [{t.reason}] PnL {t.pnl:+.2f} ({t.r_multiple:+.2f}R)", t.symbol)
        self._save()

    # ---- stats for dashboard --------------------------------------------- #
    def stats(self) -> dict:
        ts = self.trades
        wins = [t for t in ts if t.pnl > 0]
        losses = [t for t in ts if t.pnl <= 0]
        gp = sum(t.pnl for t in wins)
        gl = -sum(t.pnl for t in losses)
        n_today, pnl_today = riskmod.today_stats(ts)
        return {
            "trades": len(ts), "wins": len(wins), "losses": len(losses),
            "win_rate": round(100 * len(wins) / len(ts), 1) if ts else 0.0,
            "net_pnl": round(sum(t.pnl for t in ts), 2),
            "profit_factor": round(gp / gl, 2) if gl else (float("inf") if gp else 0.0),
            "avg_r": round(sum(t.r_multiple for t in ts) / len(ts), 2) if ts else 0.0,
            "today_trades": n_today, "today_pnl": round(pnl_today, 2),
        }
