"""Native Exness Trader API broker (REST, Ed25519-signed). No MetaTrader, no Windows.

Flow (per docs): sign every request → discover the account Access Point once via the
Gateway host → use the Access Point for account-scoped REST calls. Mutating calls return a
202 ACK with operation_id; the final result is read via GET .../operations/{operation_id}
(we poll briefly) and the trading snapshot is the source of truth for open positions.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import httpx

from ..models import AccountInfo, Candle, Position, Quote, Side, SymbolSpec, Trade, utcnow
from .base import Broker
from .exness_auth import ExnessSigner
from .exness_schema import find_list, load_schema, pick

log = logging.getLogger("exness")
BOT_TAG = "IITianScalper"


class ExnessError(RuntimeError):
    def __init__(self, status: int, body: Any, path: str):
        super().__init__(f"HTTP {status} on {path}: {json.dumps(body)[:300] if not isinstance(body, str) else body[:300]}")
        self.status, self.body, self.path = status, body, path


class ExnessBroker(Broker):
    name = "exness"

    def __init__(self, api_key: str, private_key: str, account_id: str, gateway_url: str,
                 access_point: str = ""):
        self.signer = ExnessSigner(api_key, private_key)
        self.account_id = str(account_id).strip()
        self.gateway = gateway_url.strip().rstrip("/")
        self.access_point = access_point.strip().rstrip("/")
        self.S = load_schema()
        self._http = httpx.AsyncClient(timeout=20)
        self._specs: Dict[str, SymbolSpec] = {}
        self._known: Dict[str, Position] = {}
        self._pending_closed: List[Trade] = []
        self._last_snapshot: dict = {}
        self.connected = False
        self.last_raw: Dict[str, Any] = {}      # raw responses for the dashboard probe panel

    # ------------------------------------------------------------------ http
    async def _req(self, method: str, path: str, base: str = "", query: Optional[dict] = None,
                   body: Optional[dict] = None) -> Any:
        base = base or self.access_point
        if not base:
            raise ExnessError(0, "Access Point not resolved — call connect()", path)
        full = path + ("?" + urlencode(query) if query else "")       # signed EXACTLY as transmitted
        raw = json.dumps(body, separators=(",", ":")).encode() if body is not None else None
        headers = self.signer.headers(method, full, raw)
        if raw is not None:
            headers["Content-Type"] = "application/json"
        r = await self._http.request(method, base + full, headers=headers, content=raw)
        try:
            data = r.json() if r.content else {}
        except ValueError:
            data = r.text
        if r.status_code == 429:
            wait = float(r.headers.get("Retry-After", "2") or 2)
            await asyncio.sleep(min(wait, 10))
            raise ExnessError(429, data, full)
        if r.status_code >= 400:
            raise ExnessError(r.status_code, data, full)
        return data

    def _p(self, key: str, **kw) -> str:
        return self.S[key].format(acc=self.account_id, **kw)

    # ------------------------------------------------------------- lifecycle
    async def connect(self) -> None:
        if not self.access_point:
            data = await self._req("GET", self.S["path_discovery"], base=self.gateway,
                                   query={"account_id": self.account_id})
            self.last_raw["discovery"] = data
            self.access_point = str(pick(data, ["access_point"], "")).rstrip("/")
            if not self.access_point:
                raise ExnessError(0, data, "discovery returned no access_point")
        acct = await self._req("GET", self._p("path_account"))
        self.last_raw["account"] = acct
        self.connected = True
        log.info("Exness connected: account %s via %s", self.account_id, self.access_point)

    async def close(self) -> None:
        await self._http.aclose()
        self.connected = False

    # ------------------------------------------------------------------ read
    async def raw(self, what: str, instrument: str = "", timeframe: str = "5m") -> Any:
        """Unparsed responses for the dashboard probe (so field names can be verified)."""
        if what == "account":
            return await self._req("GET", self._p("path_account"))
        if what == "limits":
            return await self._req("GET", self._p("path_limits"))
        if what == "instruments":
            return await self._req("GET", self._p("path_instruments"))
        if what == "conditions":
            return await self._req("GET", self._p("path_conditions", instrument=instrument))
        if what == "snapshot":
            return await self._req("GET", self._p("path_snapshot"))
        if what == "candles":
            return await self._candles_raw(instrument, timeframe, 5, "bid")
        raise ValueError(what)

    async def account(self) -> AccountInfo:
        snap = await self._snapshot()
        a = pick(snap, self.S["snap_account"], {}) or {}
        if not a:                                    # fall back to configuration endpoint
            a = await self._req("GET", self._p("path_account"))
        f = lambda k, d=0.0: float(pick(a, self.S[k], d) or d)
        return AccountInfo(balance=f("acct_balance"), equity=f("acct_equity") or f("acct_balance"),
                           currency=str(pick(a, self.S["acct_currency"], "USD")),
                           leverage=f("acct_leverage"), margin=f("acct_margin"))

    async def spec(self, symbol: str) -> SymbolSpec:
        if symbol not in self._specs:
            c = await self._req("GET", self._p("path_conditions", instrument=symbol))
            self.last_raw[f"conditions:{symbol}"] = c
            g = lambda k, d: pick(c, self.S[k], d)
            digits = int(g("cond_digits", 2))
            self._specs[symbol] = SymbolSpec(
                symbol, contract_size=float(g("cond_contract", 0) or _default_contract(symbol)),
                volume_min=float(g("cond_vmin", 0.01)), volume_max=float(g("cond_vmax", 100)),
                volume_step=float(g("cond_vstep", 0.01)), digits=digits, tick_size=10 ** -digits)
        return self._specs[symbol]

    async def _candles_raw(self, symbol: str, timeframe: str, count: int, price_type: str) -> Any:
        tf = self.S["timeframe_map"].get(timeframe, timeframe)
        q = {self.S["q_instrument"]: symbol, self.S["q_timeframe"]: tf,
             "from": int(time.time() * 1000), "count": -abs(count), self.S["q_price_type"]: price_type}
        return await self._req("GET", self._p("path_candles"), query=q)

    def _parse_candles(self, data: Any) -> List[Candle]:
        out = []
        for c in find_list(data, self.S["candles_root"]):
            if isinstance(c, list) and len(c) >= 5:            # [t,o,h,l,c,(v)]
                t, o, h, l, cl = c[:5]; v = c[5] if len(c) > 5 else 0
            else:
                t = pick(c, self.S["candle_time"]); o = pick(c, self.S["candle_open"])
                h = pick(c, self.S["candle_high"]); l = pick(c, self.S["candle_low"])
                cl = pick(c, self.S["candle_close"]); v = pick(c, self.S["candle_volume"], 0)
            if t is None or cl is None:
                continue
            out.append(Candle(_ts(t), float(o), float(h), float(l), float(cl), float(v or 0)))
        out.sort(key=lambda x: x.time)
        return out

    async def candles(self, symbol: str, timeframe: str, limit: int = 300) -> List[Candle]:
        data = await self._candles_raw(symbol, timeframe, limit, "bid")
        return self._parse_candles(data)

    async def quote(self, symbol: str) -> Quote:
        """No REST tick endpoint is documented; use the latest M1 bid and ask candle closes."""
        bid_c = self._parse_candles(await self._candles_raw(symbol, "1m", 1, "bid"))
        ask_c = self._parse_candles(await self._candles_raw(symbol, "1m", 1, "ask"))
        bid = bid_c[-1].close if bid_c else 0.0
        ask = ask_c[-1].close if ask_c else bid
        if not bid:
            raise ExnessError(0, "no candles", f"quote {symbol}")
        return Quote(symbol, bid, max(ask, bid), utcnow())

    async def _snapshot(self) -> dict:
        snap = await self._req("GET", self._p("path_snapshot"))
        self.last_raw["snapshot"] = snap
        self._last_snapshot = snap if isinstance(snap, dict) else {}
        return self._last_snapshot

    async def positions(self) -> List[Position]:
        snap = await self._snapshot()
        S = self.S
        current: Dict[str, Position] = {}
        for r in find_list(snap, S["snap_positions"]):
            comment = str(pick(r, S["pos_comment"], "") or "")
            pid = str(pick(r, S["pos_id"], ""))
            prev = self._known.get(pid)
            if BOT_TAG not in comment and prev is None:
                continue                                    # not ours (manual trade)
            side_raw = str(pick(r, S["pos_side"], "")).lower()
            side: Side = "sell" if ("sell" in side_raw or side_raw in ("1", "short")) else "buy"
            entry = float(pick(r, S["pos_open_price"], 0) or 0)
            sl = pick(r, S["pos_sl"]); tp = pick(r, S["pos_tp"])
            pos = Position(
                id=pid, symbol=str(pick(r, S["pos_instrument"], "")), side=side,
                volume=float(pick(r, S["pos_volume"], 0) or 0), entry_price=entry,
                stop_loss=float(sl) if sl not in (None, "", 0, "0") else None,
                take_profit=float(tp) if tp not in (None, "", 0, "0") else None,
                open_time=_ts(pick(r, S["pos_open_time"], utcnow())),
                unrealized_pnl=float(pick(r, S["pos_profit"], 0) or 0), comment=comment,
                initial_risk=prev.initial_risk if prev else (abs(entry - float(sl)) if sl else 0.0),
                trailing_active=prev.trailing_active if prev else False,
                highest_since_entry=prev.highest_since_entry if prev else entry,
                lowest_since_entry=prev.lowest_since_entry if prev else entry,
            )
            current[pid] = pos
        for pid, old in list(self._known.items()):           # closed by SL/TP on the server
            if pid not in current:
                self._pending_closed.append(self._trade_from(old, None))
        self._known = current
        return list(current.values())

    async def closed_trades(self) -> List[Trade]:
        out, self._pending_closed = self._pending_closed, []
        return out

    # ----------------------------------------------------------------- write
    async def _ack_wait(self, ack: Any, timeout: float = 8.0) -> dict:
        """Poll operation status until it leaves the pending state (or timeout)."""
        op = pick(ack, self.S["op_id"])
        if not op:
            return ack if isinstance(ack, dict) else {}
        deadline = time.monotonic() + timeout
        last = {}
        while time.monotonic() < deadline:
            await asyncio.sleep(0.6)
            try:
                last = await self._req("GET", self._p("path_operation", op=op))
            except ExnessError as e:
                if e.status == 404:
                    continue
                raise
            st = str(pick(last, self.S["op_status"], "")).lower()
            if st and st not in ("pending", "in_progress", "processing", "accepted", "new"):
                if st in ("rejected", "failed", "error"):
                    raise ExnessError(0, last, f"operation {op} {st}")
                return last
        return last

    async def open_market(self, symbol, side, volume, stop_loss, take_profit, comment="") -> Position:
        S = self.S
        spec = await self.spec(symbol)
        body = {S["b_instrument"]: symbol, S["b_side"]: S["side_buy" if side == "buy" else "side_sell"],
                S["b_volume"]: _dec(volume, 2), "comment": (comment or BOT_TAG)[:31]}
        if BOT_TAG not in body["comment"]:
            body["comment"] = BOT_TAG
        if stop_loss:
            body[S["b_sl"]] = _dec(stop_loss, spec.digits)
        if take_profit:
            body[S["b_tp"]] = _dec(take_profit, spec.digits)
        if S.get("open_deviation_points"):
            body[S["b_deviation"]] = int(S["open_deviation_points"])
        ack = await self._req("POST", self._p("path_positions"), body=body)
        self.last_raw["open_ack"] = ack
        res = await self._ack_wait(ack)
        self.last_raw["open_result"] = res
        q = await self.quote(symbol)
        price = q.ask if side == "buy" else q.bid
        pos_obj = pick(res, S["op_position"], None)
        pid = str(pick(pos_obj, S["pos_id"], "") if isinstance(pos_obj, dict) else "") or f"op:{pick(ack, S['op_id'], '')}"
        if isinstance(pos_obj, dict):
            price = float(pick(pos_obj, S["pos_open_price"], price) or price)
        pos = Position(id=pid, symbol=symbol, side=side, volume=volume, entry_price=price,
                       stop_loss=stop_loss, take_profit=take_profit, open_time=utcnow(),
                       initial_risk=abs(price - stop_loss) if stop_loss else 0.0,
                       highest_since_entry=price, lowest_since_entry=price, comment=BOT_TAG)
        if not pid.startswith("op:"):
            self._known[pid] = pos
        else:                                          # id arrives with the next snapshot
            await asyncio.sleep(1.0)
            for p in await self.positions():
                if p.symbol == symbol and p.id not in self._known:
                    pos = p
            if pos.id in self._known or not pos.id.startswith("op:"):
                self._known[pos.id] = pos
        return pos

    async def modify(self, position_id, stop_loss, take_profit) -> None:
        S = self.S
        p = self._known.get(position_id)
        digits = (await self.spec(p.symbol)).digits if p else 5
        fields = {}
        if stop_loss is not None:
            fields[S["b_sl"]] = _dec(stop_loss, digits)
        if take_profit is not None:
            fields[S["b_tp"]] = _dec(take_profit, digits)
        if S["modify_strategy"] == "patch_position":
            ack = await self._req("PATCH", self._p("path_position", pid=position_id), body=fields)
        else:   # new SLTP order attached to the position (documented on POST /orders)
            body = {S["b_order_type"]: S["order_type_sltp"], S["b_position_id"]: position_id, **fields}
            if p:
                body[S["b_instrument"]] = p.symbol
            ack = await self._req("POST", self._p("path_orders"), body=body)
        self.last_raw["modify_ack"] = ack
        await self._ack_wait(ack, timeout=5)
        if p:
            if stop_loss is not None:
                p.stop_loss = stop_loss
            if take_profit is not None:
                p.take_profit = take_profit

    async def close_position(self, position_id, reason="manual") -> Optional[Trade]:
        old = self._known.pop(position_id, None)
        ack = await self._req("DELETE", self._p("path_position", pid=position_id))
        self.last_raw["close_ack"] = ack
        res = await self._ack_wait(ack)
        self.last_raw["close_result"] = res
        if not old:
            return None
        t = self._trade_from(old, res)
        t.reason = reason
        return t

    # --------------------------------------------------------------- helpers
    def _trade_from(self, old: Position, res: Optional[dict]) -> Trade:
        """Build a Trade for a position that is gone. Exit price/pnl from the operation result
        when available; otherwise from the position's last known mark."""
        S = self.S
        px, pnl = old.entry_price, old.unrealized_pnl
        if isinstance(res, dict):
            deal = res.get("deal") if isinstance(res.get("deal"), dict) else None
            posr = pick(res, S["op_position"], None)
            if deal:
                px = float(pick(deal, ["price", "execution_price"], px) or px)
                pnl = float(pick(deal, ["profit", "pnl"], pnl) or pnl)
            elif isinstance(posr, dict):
                px = float(pick(posr, ["close_price", "price"], px) or px)
                pnl = float(pick(posr, S["pos_profit"], pnl) or pnl)
        reason = "manual"
        if old.stop_loss and ((old.side == "buy" and px <= old.stop_loss) or (old.side == "sell" and px >= old.stop_loss)):
            reason = "trail" if old.trailing_active else "sl"
        elif old.take_profit and ((old.side == "buy" and px >= old.take_profit) or (old.side == "sell" and px <= old.take_profit)):
            reason = "tp"
        r = 0.0
        if old.initial_risk:
            r = ((px - old.entry_price) if old.side == "buy" else (old.entry_price - px)) / old.initial_risk
        return Trade(id=old.id, symbol=old.symbol, side=old.side, volume=old.volume, entry_price=old.entry_price,
                     exit_price=px, open_time=old.open_time, close_time=utcnow(), pnl=round(pnl, 2),
                     reason=reason, r_multiple=round(r, 2))


def _dec(v: float, digits: int) -> str:
    """Trading volumes/prices are decimal strings per the API intro."""
    return f"{v:.{digits}f}"


def _default_contract(symbol: str) -> float:
    s = symbol.upper()
    if s.startswith("XAU"):
        return 100.0
    if s.startswith(("BTC", "ETH", "LTC", "XRP", "SOL")):
        return 1.0
    if s.startswith("XAG"):
        return 5000.0
    return 100000.0


def _ts(v) -> datetime:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, (int, float)):
        v = float(v)
        if v > 1e14:
            v /= 1e6          # microseconds
        elif v > 1e11:
            v /= 1e3          # milliseconds
        return datetime.fromtimestamp(v, tz=timezone.utc)
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return _ts(float(v))
    return utcnow()
