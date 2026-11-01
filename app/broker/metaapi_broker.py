"""Live execution through MetaApi (cloud MT5 gateway) — works with Exness MT5 accounts.

Setup (one-time, done by YOU, never by the bot):
  1. https://app.metaapi.cloud  → add your Exness MT5 account (login / server / password)
  2. https://app.metaapi.cloud/token → copy the API token
  3. Put METAAPI_TOKEN and METAAPI_ACCOUNT_ID in .env

Requires:  pip install metaapi-cloud-sdk
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

from ..models import AccountInfo, Candle, Position, Quote, Side, SymbolSpec, Trade, utcnow
from .base import Broker

log = logging.getLogger("metaapi")

MAGIC = 786110   # identifies positions opened by this bot


class MetaApiBroker(Broker):
    name = "metaapi"

    def __init__(self, token: str, account_id: str, region: str = "new-york"):
        self.token, self.account_id, self.region = token, account_id, region
        self.api = None
        self.account = None
        self.conn = None
        self._specs: Dict[str, SymbolSpec] = {}
        self._known: Dict[str, Position] = {}
        self._pending_closed: List[Trade] = []
        self.connected = False

    # ---- lifecycle ------------------------------------------------------- #
    async def connect(self) -> None:
        from metaapi_cloud_sdk import MetaApi   # imported lazily so paper mode needs no SDK
        self.api = MetaApi(self.token, {"region": self.region})
        self.account = await self.api.metatrader_account_api.get_account(self.account_id)
        if self.account.state != "DEPLOYED":
            log.info("Deploying MetaApi account server…")
            await self.account.deploy()
        await self.account.wait_connected()
        self.conn = self.account.get_rpc_connection()
        await self.conn.connect()
        await self.conn.wait_synchronized()
        self.connected = True
        log.info("MetaApi connected: %s", self.account.name)

    async def close(self) -> None:
        try:
            if self.conn:
                await self.conn.close()
            if self.api:
                self.api.close()
        finally:
            self.connected = False

    # ---- read ------------------------------------------------------------ #
    async def account_info(self) -> dict:
        return await self.conn.get_account_information()

    async def account(self) -> AccountInfo:  # type: ignore[override]
        i = await self.conn.get_account_information()
        return AccountInfo(balance=i["balance"], equity=i["equity"], currency=i.get("currency", "USD"),
                           leverage=i.get("leverage", 0), margin=i.get("margin", 0),
                           free_margin=i.get("freeMargin", 0))

    async def quote(self, symbol: str) -> Quote:
        p = await self.conn.get_symbol_price(symbol)
        return Quote(symbol, p["bid"], p["ask"], utcnow())

    async def spec(self, symbol: str) -> SymbolSpec:
        if symbol not in self._specs:
            s = await self.conn.get_symbol_specification(symbol)
            self._specs[symbol] = SymbolSpec(
                symbol, contract_size=s.get("contractSize", 1), volume_min=s.get("minVolume", 0.01),
                volume_max=s.get("maxVolume", 100), volume_step=s.get("volumeStep", 0.01),
                digits=s.get("digits", 2), tick_size=s.get("tickSize", 0.01))
        return self._specs[symbol]

    async def candles(self, symbol: str, timeframe: str, limit: int = 300) -> List[Candle]:
        raw = await self.account.get_historical_candles(symbol, timeframe, None, limit)
        out = []
        for c in raw:
            t = c["time"]
            if isinstance(t, str):
                t = datetime.fromisoformat(t.replace("Z", "+00:00"))
            elif t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            out.append(Candle(t, c["open"], c["high"], c["low"], c["close"], c.get("tickVolume", 0)))
        out.sort(key=lambda x: x.time)
        return out

    async def positions(self) -> List[Position]:
        raw = await self.conn.get_positions()
        current: Dict[str, Position] = {}
        for r in raw:
            if r.get("magic") != MAGIC:
                continue   # ignore manual / other-EA positions
            pid = str(r["id"])
            side: Side = "buy" if r["type"] == "POSITION_TYPE_BUY" else "sell"
            prev = self._known.get(pid)
            price = r.get("currentPrice", r["openPrice"])
            pos = Position(
                id=pid, symbol=r["symbol"], side=side, volume=r["volume"], entry_price=r["openPrice"],
                stop_loss=r.get("stopLoss"), take_profit=r.get("takeProfit"),
                open_time=_ts(r["time"]), unrealized_pnl=r.get("profit", 0.0), comment=r.get("comment", ""),
                initial_risk=prev.initial_risk if prev else (abs(r["openPrice"] - r["stopLoss"]) if r.get("stopLoss") else 0.0),
                trailing_active=prev.trailing_active if prev else False,
                highest_since_entry=max(prev.highest_since_entry, price) if prev else price,
                lowest_since_entry=min(prev.lowest_since_entry, price) if prev else price,
            )
            current[pid] = pos
        # detect positions the broker closed (SL / TP hit)
        for pid, old in list(self._known.items()):
            if pid not in current:
                t = await self._trade_from_history(old)
                if t:
                    self._pending_closed.append(t)
        self._known = current
        return list(current.values())

    async def closed_trades(self) -> List[Trade]:
        out, self._pending_closed = self._pending_closed, []
        return out

    # ---- write ----------------------------------------------------------- #
    async def open_market(self, symbol, side, volume, stop_loss, take_profit, comment="") -> Position:
        opts = {"comment": comment[:31] or "IITianScalper", "magic": MAGIC}
        if side == "buy":
            res = await self.conn.create_market_buy_order(symbol, volume, stop_loss, take_profit, opts)
        else:
            res = await self.conn.create_market_sell_order(symbol, volume, stop_loss, take_profit, opts)
        pid = str(res.get("positionId") or res.get("orderId"))
        q = await self.quote(symbol)
        price = q.ask if side == "buy" else q.bid
        pos = Position(id=pid, symbol=symbol, side=side, volume=volume, entry_price=price,
                       stop_loss=stop_loss, take_profit=take_profit, open_time=utcnow(),
                       initial_risk=abs(price - stop_loss) if stop_loss else 0.0,
                       highest_since_entry=price, lowest_since_entry=price, comment=comment)
        self._known[pid] = pos
        return pos

    async def modify(self, position_id, stop_loss, take_profit) -> None:
        await self.conn.modify_position(position_id, stop_loss, take_profit)
        p = self._known.get(position_id)
        if p:
            if stop_loss is not None:
                p.stop_loss = stop_loss
            if take_profit is not None:
                p.take_profit = take_profit

    async def close_position(self, position_id, reason="manual") -> Optional[Trade]:
        old = self._known.pop(position_id, None)
        await self.conn.close_position(position_id)
        if old:
            t = await self._trade_from_history(old)
            if t:
                t.reason = reason
                return t
        return None

    # ---- helpers --------------------------------------------------------- #
    async def _trade_from_history(self, old: Position) -> Optional[Trade]:
        try:
            deals = await self.conn.get_deals_by_position(old.id)
            deals = deals.get("deals", deals) if isinstance(deals, dict) else deals
            exits = [d for d in deals if d.get("entryType") in ("DEAL_ENTRY_OUT", "DEAL_ENTRY_OUT_BY", "DEAL_ENTRY_INOUT")]
            if not exits:
                return None
            pnl = sum(d.get("profit", 0) + d.get("commission", 0) + d.get("swap", 0) for d in deals)
            last = exits[-1]
            px = last.get("price", old.entry_price)
            reason = {"DEAL_REASON_SL": "sl", "DEAL_REASON_TP": "tp"}.get(last.get("reason", ""), "manual")
            if reason == "sl" and old.trailing_active:
                reason = "trail"
            r = 0.0
            if old.initial_risk:
                r = ((px - old.entry_price) if old.side == "buy" else (old.entry_price - px)) / old.initial_risk
            return Trade(id=old.id, symbol=old.symbol, side=old.side, volume=old.volume,
                         entry_price=old.entry_price, exit_price=px, open_time=old.open_time,
                         close_time=_ts(last.get("time", utcnow())), pnl=round(pnl, 2), reason=reason,
                         r_multiple=round(r, 2))
        except Exception as e:   # history may lag a second behind
            log.warning("Could not read deal history for %s: %s", old.id, e)
            return None


def _ts(v) -> datetime:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, str):
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    return utcnow()
