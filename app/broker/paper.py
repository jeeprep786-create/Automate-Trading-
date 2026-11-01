"""Paper broker: simulates fills, SL/TP and P&L on top of any price feed.

PnL is computed in quote currency and treated as account currency (correct for
USD-quoted symbols on a USD account: XAUUSD, BTCUSD, EURUSD, GBPUSD).
"""
from __future__ import annotations

import itertools
from typing import Dict, List, Optional

from ..models import AccountInfo, Candle, Position, Quote, Side, SymbolSpec, Trade, utcnow
from .base import Broker
from .feeds import Feed

_ids = itertools.count(1)


class PaperBroker(Broker):
    name = "paper"

    def __init__(self, feed: Feed, balance: float = 10000.0):
        self.feed = feed
        self.balance = balance
        self.start_balance = balance
        self._positions: Dict[str, Position] = {}
        self._closed: List[Trade] = []
        self._pending_closed: List[Trade] = []
        self._specs: Dict[str, SymbolSpec] = {}
        self._last_quote: Dict[str, Quote] = {}

    # ---- read ------------------------------------------------------------ #
    async def account(self) -> AccountInfo:
        unreal = sum(p.unrealized_pnl for p in self._positions.values())
        return AccountInfo(balance=round(self.balance, 2), equity=round(self.balance + unreal, 2))

    async def quote(self, symbol: str) -> Quote:
        q = await self.feed.quote(symbol)
        self._last_quote[symbol] = q
        await self._mark(symbol, q)
        return q

    async def spec(self, symbol: str) -> SymbolSpec:
        if symbol not in self._specs:
            self._specs[symbol] = await self.feed.spec(symbol)
        return self._specs[symbol]

    async def candles(self, symbol: str, timeframe: str, limit: int = 300) -> List[Candle]:
        return await self.feed.candles(symbol, timeframe, limit)

    async def positions(self) -> List[Position]:
        return list(self._positions.values())

    async def closed_trades(self) -> List[Trade]:
        out, self._pending_closed = self._pending_closed, []
        return out

    # ---- write ----------------------------------------------------------- #
    async def open_market(self, symbol, side, volume, stop_loss, take_profit, comment="") -> Position:
        q = await self.quote(symbol)
        price = q.ask if side == "buy" else q.bid
        pos = Position(
            id=f"P{next(_ids)}", symbol=symbol, side=side, volume=volume, entry_price=price,
            stop_loss=stop_loss, take_profit=take_profit, open_time=utcnow(),
            initial_risk=abs(price - stop_loss) if stop_loss else 0.0,
            highest_since_entry=price, lowest_since_entry=price, comment=comment,
        )
        self._positions[pos.id] = pos
        return pos

    async def modify(self, position_id, stop_loss, take_profit) -> None:
        p = self._positions.get(position_id)
        if not p:
            return
        if stop_loss is not None:
            p.stop_loss = stop_loss
        if take_profit is not None:
            p.take_profit = take_profit

    async def close_position(self, position_id, reason="manual") -> Optional[Trade]:
        p = self._positions.get(position_id)
        if not p:
            return None
        q = self._last_quote.get(p.symbol) or await self.feed.quote(p.symbol)
        price = q.bid if p.side == "buy" else q.ask
        return await self._close(p, price, reason)

    # ---- internals ------------------------------------------------------- #
    def _pnl(self, p: Position, price: float, spec: SymbolSpec) -> float:
        diff = (price - p.entry_price) if p.side == "buy" else (p.entry_price - price)
        return diff * p.volume * spec.contract_size

    async def _close(self, p: Position, price: float, reason: str) -> Trade:
        spec = await self.spec(p.symbol)
        pnl = self._pnl(p, price, spec)
        self.balance += pnl
        r = 0.0
        if p.initial_risk:
            r = ((price - p.entry_price) if p.side == "buy" else (p.entry_price - price)) / p.initial_risk
        t = Trade(id=p.id, symbol=p.symbol, side=p.side, volume=p.volume, entry_price=p.entry_price,
                  exit_price=price, open_time=p.open_time, close_time=utcnow(), pnl=round(pnl, 2),
                  reason=reason, r_multiple=round(r, 2))
        self._positions.pop(p.id, None)
        self._closed.append(t)
        self._pending_closed.append(t)
        return t

    async def _mark(self, symbol: str, q: Quote) -> None:
        """Mark-to-market and trigger SL/TP for positions on this symbol."""
        spec = await self.spec(symbol)
        for p in list(self._positions.values()):
            if p.symbol != symbol:
                continue
            exit_px = q.bid if p.side == "buy" else q.ask
            p.highest_since_entry = max(p.highest_since_entry, exit_px)
            p.lowest_since_entry = min(p.lowest_since_entry, exit_px)
            p.unrealized_pnl = round(self._pnl(p, exit_px, spec), 2)
            if p.side == "buy":
                if p.stop_loss is not None and exit_px <= p.stop_loss:
                    await self._close(p, p.stop_loss, "trail" if p.trailing_active else "sl")
                elif p.take_profit is not None and exit_px >= p.take_profit:
                    await self._close(p, p.take_profit, "tp")
            else:
                if p.stop_loss is not None and exit_px >= p.stop_loss:
                    await self._close(p, p.stop_loss, "trail" if p.trailing_active else "sl")
                elif p.take_profit is not None and exit_px <= p.take_profit:
                    await self._close(p, p.take_profit, "tp")
