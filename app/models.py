"""Shared data models for the IITian Scalper."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Literal, Optional

Side = Literal["buy", "sell"]


@dataclass
class Candle:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class SymbolSpec:
    symbol: str
    contract_size: float = 100.0      # units per 1.0 lot
    volume_min: float = 0.01
    volume_max: float = 100.0
    volume_step: float = 0.01
    digits: int = 2
    tick_size: float = 0.01


@dataclass
class Quote:
    symbol: str
    bid: float
    ask: float
    time: datetime

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return self.ask - self.bid


@dataclass
class Position:
    id: str
    symbol: str
    side: Side
    volume: float
    entry_price: float
    stop_loss: Optional[float]
    take_profit: Optional[float]
    open_time: datetime
    initial_risk: float = 0.0          # |entry - initial SL| in price units
    trailing_active: bool = False
    highest_since_entry: float = 0.0
    lowest_since_entry: float = 0.0
    unrealized_pnl: float = 0.0
    comment: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["open_time"] = self.open_time.isoformat()
        return d


@dataclass
class Trade:
    """A closed trade."""
    id: str
    symbol: str
    side: Side
    volume: float
    entry_price: float
    exit_price: float
    open_time: datetime
    close_time: datetime
    pnl: float
    reason: str                        # 'tp' | 'sl' | 'trail' | 'signal' | 'manual' | 'kill'
    r_multiple: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["open_time"] = self.open_time.isoformat()
        d["close_time"] = self.close_time.isoformat()
        return d


@dataclass
class Signal:
    side: Optional[Side]               # None = no entry
    reason: str
    atr: float
    close: float
    ema_fast: float
    ema_slow: float
    ema_trend: float
    rsi: float
    exit_long: bool = False            # strategy-level exit for open longs
    exit_short: bool = False


@dataclass
class AccountInfo:
    balance: float
    equity: float
    currency: str = "USD"
    leverage: float = 0.0
    margin: float = 0.0
    free_margin: float = 0.0


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
