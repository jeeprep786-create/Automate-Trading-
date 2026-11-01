"""Broker interface every execution backend implements."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional

from ..models import AccountInfo, Candle, Position, Quote, Side, SymbolSpec, Trade


class Broker(ABC):
    name: str = "base"

    async def connect(self) -> None: ...
    async def close(self) -> None: ...

    @abstractmethod
    async def account(self) -> AccountInfo: ...

    @abstractmethod
    async def quote(self, symbol: str) -> Quote: ...

    @abstractmethod
    async def spec(self, symbol: str) -> SymbolSpec: ...

    @abstractmethod
    async def candles(self, symbol: str, timeframe: str, limit: int = 300) -> List[Candle]: ...

    @abstractmethod
    async def positions(self) -> List[Position]: ...

    @abstractmethod
    async def open_market(self, symbol: str, side: Side, volume: float,
                          stop_loss: Optional[float], take_profit: Optional[float],
                          comment: str = "") -> Position: ...

    @abstractmethod
    async def modify(self, position_id: str, stop_loss: Optional[float],
                     take_profit: Optional[float]) -> None: ...

    @abstractmethod
    async def close_position(self, position_id: str, reason: str = "manual") -> Optional[Trade]: ...

    async def closed_trades(self) -> List[Trade]:
        """Trades closed by the broker itself (SL/TP hits). Default: none."""
        return []
