"""Field-name map for the Exness Trader API.

Paths and the signing protocol are confirmed from the public docs. The JSON field names
inside request bodies / responses are rendered client-side on the docs portal, so the
ones marked VERIFY below are best guesses — confirm them against
https://www.exness-api.com/openapi/trading.public.yaml (or the dashboard's "Probe" panel,
which shows raw responses) and override any of them in data/exness_schema.json.

Every reader in exness_broker.py tries the primary name first and then the alternatives,
so a wrong guess usually still works; writers (order bodies) use the primary name only.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..config import DATA_DIR

SCHEMA = {
    # ---- paths (confirmed) -------------------------------------------------
    "path_discovery": "/v1/trading/access-point",
    "path_account": "/v1/configuration/accounts/{acc}/account",
    "path_limits": "/v1/configuration/accounts/{acc}/limits",
    "path_instruments": "/v1/configuration/accounts/{acc}/instruments",            # VERIFY (from page slug)
    "path_conditions": "/v1/configuration/accounts/{acc}/instruments/{instrument}/conditions",
    "path_candles": "/v1/market-data/accounts/{acc}/candles",
    "path_snapshot": "/v1/trading/accounts/{acc}/snapshot",
    "path_positions": "/v1/trading/accounts/{acc}/positions",
    "path_position": "/v1/trading/accounts/{acc}/positions/{pid}",
    "path_orders": "/v1/trading/accounts/{acc}/orders",
    "path_order": "/v1/trading/accounts/{acc}/orders/{oid}",
    "path_operation": "/v1/trading/accounts/{acc}/operations/{op}",
    "path_deals": "/v1/trading/accounts/{acc}/history/deals",                       # VERIFY
    "path_ws_events": "/v1/server-events/accounts/{acc}/ws/events",

    # ---- candles query (from/to/count confirmed; names of the other three VERIFY) ----
    "q_instrument": "instrument",
    "q_timeframe": "timeframe",
    "q_price_type": "price_type",        # values: ask | bid | both  (VERIFY the exact spelling)
    "candle_time": ["time", "open_time", "t", "timestamp"],
    "candle_open": ["open", "o"], "candle_high": ["high", "h"], "candle_low": ["low", "l"],
    "candle_close": ["close", "c"], "candle_volume": ["volume", "tick_volume", "v"],
    "candles_root": ["candles", "data", "items", "result"],
    # how to pass the timeframe: bot uses 1m/5m/15m/1h; API wants M1/M5/M15/H1 (confirmed style)
    "timeframe_map": {"1m": "M1", "3m": "M3", "5m": "M5", "15m": "M15", "30m": "M30", "1h": "H1", "4h": "H4", "1d": "D1"},

    # ---- open position body (stop_loss_price / take_profit_price / volume confirmed) ----
    "b_instrument": "instrument",         # VERIFY (could be "symbol")
    "b_side": "side",                     # VERIFY
    "side_buy": "buy", "side_sell": "sell",   # VERIFY (could be BUY/SELL or type: 0/1)
    "b_volume": "volume",
    "b_sl": "stop_loss_price",
    "b_tp": "take_profit_price",
    "b_deviation": "deviation",           # max slippage in points (confirmed for close; VERIFY for open)
    "open_deviation_points": 50,

    # ---- modify SL/TP of an open position ----------------------------------
    # Docs: "POST /orders creates ... a new SLTP order that sets SL/TP for an existing position",
    # and "Modify active order" exists for changing an existing order. Strategy A posts an SLTP
    # order; strategy B patches the position. Set "modify_strategy" after checking the YAML.
    "modify_strategy": "sltp_order",      # sltp_order | patch_position
    "b_order_type": "type",               # VERIFY
    "order_type_sltp": "sltp",            # VERIFY
    "b_position_id": "position_id",       # VERIFY

    # ---- snapshot / position fields (VERIFY all) ----------------------------
    "snap_positions": ["positions", "open_positions"],
    "snap_orders": ["orders", "open_orders", "pending_orders"],
    "snap_account": ["account_state", "account"],
    "pos_id": ["id", "position_id", "ticket"],
    "pos_instrument": ["instrument", "symbol"],
    "pos_side": ["side", "type", "direction"],
    "pos_volume": ["volume", "lots"],
    "pos_open_price": ["open_price", "price", "entry_price"],
    "pos_sl": ["stop_loss_price", "sl", "stop_loss"],
    "pos_tp": ["take_profit_price", "tp", "take_profit"],
    "pos_profit": ["profit", "floating_profit", "pnl"],
    "pos_open_time": ["open_time", "time", "created_at"],
    "pos_comment": ["comment", "client_comment"],
    "acct_balance": ["balance"], "acct_equity": ["equity"], "acct_margin": ["used_margin", "margin"],
    "acct_currency": ["currency", "account_currency"], "acct_leverage": ["leverage"],

    # ---- instrument conditions (volume_* and point_digits confirmed) -------
    "cond_vmin": ["volume_min"], "cond_vmax": ["volume_max"], "cond_vstep": ["volume_step"],
    "cond_digits": ["point_digits", "digits"],
    "cond_contract": ["contract_size", "lot_size", "contract"],

    # ---- operation ACK / status ---------------------------------------------
    "op_id": ["operation_id", "id"],
    "op_status": ["status", "state"],
    "op_position": ["position"],
}


def load_schema() -> dict:
    s = dict(SCHEMA)
    p: Path = DATA_DIR / "exness_schema.json"
    if p.exists():
        try:
            s.update(json.loads(p.read_text()))
        except Exception:
            pass
    return s


def pick(d: dict, names, default=None):
    """Return the first present key from `names` (str or list) in dict d."""
    if not isinstance(d, dict):
        return default
    for n in ([names] if isinstance(names, str) else names):
        if n in d and d[n] is not None:
            return d[n]
    return default


def find_list(d, names) -> list:
    """Find a list under any of `names`, searching one level of nesting; or d itself if it's a list."""
    if isinstance(d, list):
        return d
    v = pick(d, names)
    if isinstance(v, list):
        return v
    if isinstance(d, dict):
        for sub in d.values():
            if isinstance(sub, dict):
                v = pick(sub, names)
                if isinstance(v, list):
                    return v
    return []
