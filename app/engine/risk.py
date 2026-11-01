"""Position sizing and account-level guard rails."""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import List

from ..config import RiskConfig, SymbolConfig
from ..models import SymbolSpec, Trade


def lots_for_risk(balance: float, risk_pct: float, sl_distance: float, spec: SymbolSpec,
                  fixed_volume: float = 0.0) -> float:
    """Volume (lots) so that a full stop-out loses ≈ risk_pct % of balance."""
    if fixed_volume > 0:
        return _clamp(fixed_volume, spec)
    if sl_distance <= 0 or spec.contract_size <= 0:
        return 0.0
    risk_amount = balance * risk_pct / 100.0
    lots = risk_amount / (sl_distance * spec.contract_size)
    return _clamp(lots, spec)


def _clamp(lots: float, spec: SymbolSpec) -> float:
    step = spec.volume_step or 0.01
    lots = math.floor(lots / step + 1e-9) * step
    lots = max(spec.volume_min, min(spec.volume_max, lots))
    return round(lots, 2)


def in_trading_window(risk: RiskConfig, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    if now.weekday() not in risk.trade_days:
        return False
    try:
        a, b = risk.trading_hours_utc.split("-")
        h1, m1 = map(int, a.split(":"))
        h2, m2 = map(int, b.split(":"))
    except Exception:
        return True
    t = now.hour * 60 + now.minute
    start, end = h1 * 60 + m1, h2 * 60 + m2
    return start <= t <= end if start <= end else (t >= start or t <= end)


def today_stats(trades: List[Trade], now: datetime | None = None):
    now = now or datetime.now(timezone.utc)
    today = [t for t in trades if t.close_time.date() == now.date()]
    return len(today), sum(t.pnl for t in today)
