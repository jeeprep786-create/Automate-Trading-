"""Market-data feeds used by the paper broker and the backtester.

A feed only READS prices; it never places orders.
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone
from typing import Dict, List

from ..models import Candle, Quote, SymbolSpec

TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}

DEFAULT_SPECS: Dict[str, SymbolSpec] = {
    "XAUUSD": SymbolSpec("XAUUSD", contract_size=100, digits=2, tick_size=0.01),
    "BTCUSD": SymbolSpec("BTCUSD", contract_size=1, digits=2, tick_size=0.01),
    "EURUSD": SymbolSpec("EURUSD", contract_size=100000, digits=5, tick_size=0.00001),
    "GBPUSD": SymbolSpec("GBPUSD", contract_size=100000, digits=5, tick_size=0.00001),
    "USDJPY": SymbolSpec("USDJPY", contract_size=100000, digits=3, tick_size=0.001),
    "ETHUSD": SymbolSpec("ETHUSD", contract_size=1, digits=2, tick_size=0.01),
}

START_PRICES = {"XAUUSD": 4000.0, "BTCUSD": 100000.0, "EURUSD": 1.0850, "GBPUSD": 1.2700,
                "USDJPY": 150.0, "ETHUSD": 3500.0}
# annualised-ish volatility used by the synthetic generator (per-minute sigma)
MINUTE_SIGMA = {"XAUUSD": 0.00035, "BTCUSD": 0.0006, "EURUSD": 0.00008, "GBPUSD": 0.0001,
                "USDJPY": 0.00008, "ETHUSD": 0.0008}


def base_symbol(symbol: str) -> str:
    """Strip Exness suffixes (XAUUSDm, XAUUSDz, XAUUSD.a ...)."""
    s = symbol.upper()
    for suf in ("M", "Z", "C", "E", ".A", ".B", "_RAW", "_PRO"):
        if s.endswith(suf) and len(s) > 6:
            s = s[: -len(suf)]
            break
    return s


def spec_for(symbol: str) -> SymbolSpec:
    base = DEFAULT_SPECS.get(base_symbol(symbol))
    if base:
        return SymbolSpec(symbol, base.contract_size, base.volume_min, base.volume_max,
                          base.volume_step, base.digits, base.tick_size)
    return SymbolSpec(symbol, contract_size=1, digits=2, tick_size=0.01)


class Feed:
    async def quote(self, symbol: str) -> Quote: raise NotImplementedError
    async def candles(self, symbol: str, timeframe: str, limit: int = 300) -> List[Candle]: raise NotImplementedError
    async def spec(self, symbol: str) -> SymbolSpec: return spec_for(symbol)
    def spread(self, symbol: str) -> float: return 0.0


class SyntheticFeed(Feed):
    """Random-walk prices with mean-reverting drift so the engine has something to trade
    when no MetaApi token is configured. Candles are generated once (history) and then
    extended live, minute by minute, as the clock advances."""

    def __init__(self, spreads: Dict[str, float] | None = None, seed: int | None = None,
                 history_minutes: int = 15000):
        self.rng = random.Random(seed)
        self.spreads = spreads or {}
        self.history_minutes = history_minutes
        self._minutes: Dict[str, List[Candle]] = {}   # 1-minute candles per symbol
        self._last_tick: Dict[str, float] = {}

    def spread(self, symbol: str) -> float:
        if symbol in self.spreads:
            return self.spreads[symbol]
        b = base_symbol(symbol)
        return self.spreads.get(b, {"XAUUSD": 0.2, "BTCUSD": 15.0, "EURUSD": 0.0001,
                                     "GBPUSD": 0.00015}.get(b, 0.0))

    # ---- generation ------------------------------------------------------ #
    def _ensure(self, symbol: str) -> None:
        if symbol in self._minutes:
            return
        b = base_symbol(symbol)
        price = START_PRICES.get(b, 100.0)
        sigma = MINUTE_SIGMA.get(b, 0.0005)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        start = now - timedelta(minutes=self.history_minutes)
        candles: List[Candle] = []
        drift = 0.0
        t = start
        start_price = price
        while t <= now:
            # regime-ish drift: slowly varying trend, softly anchored to the start price
            anchor = -0.002 * math.log(price / start_price) * sigma
            drift = max(-sigma * 0.3, min(sigma * 0.3, drift * 0.98 + self.rng.gauss(0, sigma * 0.03) + anchor))
            o = price
            path = [o]
            for _ in range(6):
                path.append(path[-1] * math.exp(drift + self.rng.gauss(0, sigma / math.sqrt(6))))
            c = path[-1]
            h, l = max(path), min(path)
            candles.append(Candle(t, o, h, l, c, volume=abs(self.rng.gauss(100, 30))))
            price = c
            t += timedelta(minutes=1)
        self._minutes[symbol] = candles
        self._drift = drift

    def _advance(self, symbol: str) -> None:
        """Extend 1-minute candles up to the current minute and jitter the live candle."""
        self._ensure(symbol)
        candles = self._minutes[symbol]
        b = base_symbol(symbol)
        sigma = MINUTE_SIGMA.get(b, 0.0005)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        last = candles[-1]
        while last.time < now:
            o = last.close
            c = o * math.exp(self.rng.gauss(0, sigma))
            h = max(o, c) * (1 + abs(self.rng.gauss(0, sigma / 3)))
            l = min(o, c) * (1 - abs(self.rng.gauss(0, sigma / 3)))
            last = Candle(last.time + timedelta(minutes=1), o, h, l, c, abs(self.rng.gauss(100, 30)))
            candles.append(last)
        # intrabar tick movement on the live candle
        tick = last.close * math.exp(self.rng.gauss(0, sigma / 4))
        last.close = tick
        last.high = max(last.high, tick)
        last.low = min(last.low, tick)
        if len(candles) > self.history_minutes * 2:
            del candles[: len(candles) - self.history_minutes]

    # ---- Feed API -------------------------------------------------------- #
    async def quote(self, symbol: str) -> Quote:
        self._advance(symbol)
        mid = self._minutes[symbol][-1].close
        sp = self.spread(symbol)
        return Quote(symbol, mid - sp / 2, mid + sp / 2, datetime.now(timezone.utc))

    async def candles(self, symbol: str, timeframe: str, limit: int = 300) -> List[Candle]:
        self._advance(symbol)
        return aggregate(self._minutes[symbol], timeframe)[-limit:]


def aggregate(minutes: List[Candle], timeframe: str) -> List[Candle]:
    """Aggregate 1-minute candles into a higher timeframe (bucketed on UTC clock)."""
    secs = TF_SECONDS.get(timeframe, 60)
    if secs == 60:
        return list(minutes)
    out: List[Candle] = []
    bucket_start = None
    for c in minutes:
        ts = int(c.time.timestamp())
        bs = ts - ts % secs
        if bucket_start != bs:
            bucket_start = bs
            out.append(Candle(datetime.fromtimestamp(bs, tz=timezone.utc), c.open, c.high, c.low, c.close, c.volume))
        else:
            cur = out[-1]
            cur.high = max(cur.high, c.high)
            cur.low = min(cur.low, c.low)
            cur.close = c.close
            cur.volume += c.volume
    return out


class BrokerFeed(Feed):
    """Wraps a live broker (MetaApi) as a read-only feed for paper trading."""

    def __init__(self, broker):
        self.broker = broker

    async def quote(self, symbol: str) -> Quote:
        return await self.broker.quote(symbol)

    async def candles(self, symbol: str, timeframe: str, limit: int = 300) -> List[Candle]:
        return await self.broker.candles(symbol, timeframe, limit)

    async def spec(self, symbol: str) -> SymbolSpec:
        return await self.broker.spec(symbol)
