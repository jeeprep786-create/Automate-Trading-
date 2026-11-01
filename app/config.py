"""Runtime configuration: .env for secrets, config.json for strategy/risk settings."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
CONFIG_PATH = DATA_DIR / "config.json"

load_dotenv(ROOT / ".env")


# --------------------------------------------------------------------------- #
# Secrets (never stored in config.json, only read from environment / .env)
# --------------------------------------------------------------------------- #
def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


DASHBOARD_PASSWORD = env("DASHBOARD_PASSWORD")          # optional basic protection
HOST = env("HOST", "0.0.0.0")
PORT = int(env("PORT", "8000") or 8000)

# Credentials come from .env OR from data/credentials.json (written by the dashboard's
# "Connect API" panel, file mode 0600). The JSON file wins when both are set.
CREDS_PATH = DATA_DIR / "credentials.json"
CRED_KEYS = ["EXN_API_KEY", "EXN_PRIVATE_KEY", "EXN_ACCOUNT_ID", "EXN_DISCOVERY_BASE_URL", "EXN_API_BASE_URL",
             "METAAPI_TOKEN", "METAAPI_ACCOUNT_ID", "METAAPI_REGION"]


def load_creds() -> Dict[str, str]:
    out = {k: env(k) for k in CRED_KEYS}
    if CREDS_PATH.exists():
        try:
            for k, v in json.loads(CREDS_PATH.read_text()).items():
                if k in CRED_KEYS and v:
                    out[k] = str(v).strip()
        except Exception:
            pass
    out.setdefault("METAAPI_REGION", "new-york")
    return out


def save_creds(update: Dict[str, str]) -> Dict[str, str]:
    cur = {}
    if CREDS_PATH.exists():
        try:
            cur = json.loads(CREDS_PATH.read_text())
        except Exception:
            cur = {}
    for k, v in update.items():
        if k in CRED_KEYS:
            if v is None or v == "":
                cur.pop(k, None)
            else:
                cur[k] = str(v).strip()
    CREDS_PATH.write_text(json.dumps(cur, indent=1))
    try:
        os.chmod(CREDS_PATH, 0o600)
    except OSError:
        pass
    return load_creds()


def exness_configured(c: Dict[str, str] | None = None) -> bool:
    c = c or load_creds()
    return bool(c["EXN_API_KEY"] and c["EXN_PRIVATE_KEY"] and c["EXN_ACCOUNT_ID"]
                and (c["EXN_DISCOVERY_BASE_URL"] or c["EXN_API_BASE_URL"]))


# --------------------------------------------------------------------------- #
# Strategy / risk settings (editable from the dashboard)
# --------------------------------------------------------------------------- #
@dataclass
class SymbolConfig:
    symbol: str
    timeframe: str = "5m"              # 1m, 5m, 15m, 1h
    enabled: bool = True
    # Indicators
    ema_fast: int = 9
    ema_slow: int = 21
    ema_trend: int = 50
    rsi_period: int = 14
    rsi_long_min: float = 50.0
    rsi_long_max: float = 70.0
    rsi_short_min: float = 30.0
    rsi_short_max: float = 50.0
    atr_period: int = 14
    use_trend_filter: bool = True      # only trade in direction of EMA-trend
    use_vwap_filter: bool = False      # long above VWAP / short below
    use_supertrend_filter: bool = False
    supertrend_period: int = 10
    supertrend_mult: float = 3.0
    min_atr_pct: float = 0.0           # skip if ATR / price < this (dead market)
    max_spread_atr: float = 0.35       # skip if spread > this * ATR
    # Stops
    sl_atr_mult: float = 1.5
    tp_rr: float = 1.5                 # take-profit = sl_distance * tp_rr
    trail_trigger_r: float = 1.0       # start trailing once profit >= this many R
    breakeven_r: float = 1.0           # move SL to BE (+buffer) at this R
    trail_atr_mult: float = 1.0        # trail distance = ATR * this
    trail_step_atr: float = 0.25       # only move SL if improves by >= this * ATR
    exit_on_opposite_cross: bool = True
    # Per-symbol risk
    risk_pct: float = 0.5              # % of balance risked per trade
    max_positions: int = 1
    fixed_volume: float = 0.0          # if > 0, overrides risk-based sizing
    cooldown_bars: int = 2             # bars to wait after a closed trade


@dataclass
class RiskConfig:
    max_daily_loss_pct: float = 3.0    # stop trading for the day after this loss
    max_daily_trades: int = 30
    max_open_positions: int = 3
    trading_hours_utc: str = "00:00-23:59"   # e.g. "07:00-20:00"
    trade_days: List[int] = field(default_factory=lambda: [0, 1, 2, 3, 4, 5, 6])  # Mon=0


@dataclass
class AppConfig:
    mode: str = "paper"                # paper | live
    backend: str = "exness"            # exness (native Trader API) | metaapi (cloud MT5 gateway)
    poll_seconds: float = 5.0          # how often the engine checks prices
    paper_balance: float = 10000.0
    paper_spread_pts: Dict[str, float] = field(default_factory=lambda: {
        "XAUUSDm": 0.20, "BTCUSDm": 15.0, "EURUSDm": 0.00010, "GBPUSDm": 0.00015,
        "XAUUSD": 0.20, "BTCUSD": 15.0, "EURUSD": 0.00010, "GBPUSD": 0.00015,
    })
    risk: RiskConfig = field(default_factory=RiskConfig)
    symbols: List[SymbolConfig] = field(default_factory=lambda: [
        SymbolConfig("XAUUSDm", "1m", sl_atr_mult=1.5, tp_rr=1.5, risk_pct=0.5),
        SymbolConfig("BTCUSDm", "5m", sl_atr_mult=1.8, tp_rr=1.5, risk_pct=0.5),
        SymbolConfig("EURUSDm", "5m", sl_atr_mult=1.5, tp_rr=1.5, risk_pct=0.5),
        SymbolConfig("GBPUSDm", "5m", sl_atr_mult=1.5, tp_rr=1.5, risk_pct=0.5),
    ])

    # ---- persistence ---------------------------------------------------- #
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "AppConfig":
        cfg = cls()
        for k, v in d.items():
            if k == "risk":
                cfg.risk = RiskConfig(**{kk: vv for kk, vv in v.items() if kk in RiskConfig.__dataclass_fields__})
            elif k == "symbols":
                cfg.symbols = [
                    SymbolConfig(**{kk: vv for kk, vv in s.items() if kk in SymbolConfig.__dataclass_fields__})
                    for s in v
                ]
            elif k in cls.__dataclass_fields__:
                setattr(cfg, k, v)
        return cfg

    def save(self) -> None:
        CONFIG_PATH.write_text(json.dumps(self.to_dict(), indent=2))

    @classmethod
    def load(cls) -> "AppConfig":
        if CONFIG_PATH.exists():
            try:
                return cls.from_dict(json.loads(CONFIG_PATH.read_text()))
            except Exception:
                pass
        cfg = cls()
        cfg.save()
        return cfg

    def symbol(self, name: str) -> SymbolConfig | None:
        return next((s for s in self.symbols if s.symbol == name), None)


def metaapi_configured(c: Dict[str, str] | None = None) -> bool:
    c = c or load_creds()
    return bool(c["METAAPI_TOKEN"] and c["METAAPI_ACCOUNT_ID"])
