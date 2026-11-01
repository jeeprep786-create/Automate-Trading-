"""Mock Exness Trader API for local testing of the signed-request flow.

It enforces the documented authentication rules (EXN-* headers, Ed25519 signature over the
decoded EXN-DATA bytes, body hash, path+query match, empty idempotency key on GET, non-empty on
mutating calls, fresh timestamp) and imitates the endpoint shapes. Field names of responses
follow app/broker/exness_schema.py defaults — if the real API differs, adjust the schema file,
not the bot.

Run:  uvicorn tools.mock_exness:app --port 9001
Then point the dashboard at Gateway URL http://localhost:9001 with any API key and the
public key printed on startup registered (the mock accepts the key pair from MOCK_PRIVATE_KEY
env or generates one and prints it).
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import random
import time
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

app = FastAPI(title="Mock Exness Trader API")

# ---- key material ----------------------------------------------------------
_priv_text = os.getenv("MOCK_PRIVATE_KEY")
if _priv_text and "BEGIN" in _priv_text:
    PRIV = serialization.load_pem_private_key(_priv_text.encode(), None)
elif _priv_text:
    PRIV = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(_priv_text)[:32])
else:
    PRIV = Ed25519PrivateKey.generate()
PUB = PRIV.public_key()
API_KEY = os.getenv("MOCK_API_KEY", "exnsk-mock")
PEM = PRIV.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
print("MOCK private key (PEM) — paste into the dashboard:\n" + PEM)

# ---- state -------------------------------------------------------------------
STATE = {"balance": 10000.0, "positions": {}, "orders": {}, "ops": {}, "seen_idem": set()}
PRICES = {"XAUUSD": 4000.0, "BTCUSD": 100000.0, "EURUSD": 1.085, "GBPUSD": 1.27}
COND = {"XAUUSD": (0.01, 200, 0.01, 2, 100), "BTCUSD": (0.01, 100, 0.01, 2, 1),
        "EURUSD": (0.01, 200, 0.01, 5, 100000), "GBPUSD": (0.01, 200, 0.01, 5, 100000)}
rng = random.Random(1)


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def err(status: int, code: int, msg: str):
    return JSONResponse(status_code=status, content={"error_code": code, "error_message": msg})


@app.middleware("http")
async def verify(request: Request, call_next):
    h = request.headers
    for k in ("EXN-API-KEY", "EXN-TIMESTAMP", "EXN-SIGN-VERSION", "EXN-DATA", "EXN-SIGN"):
        if k not in h:
            return err(401, 1000, f"AUTH_INVALID_API_KEY (missing {k})")
    if "exn-idempotency-key" not in {k.lower() for k in h.keys()}:
        return err(400, 1001, "AUTH_INVALID_SIGNATURE (EXN-IDEMPOTENCY-KEY header must be present)")
    if h["EXN-API-KEY"] != API_KEY:
        return err(401, 1000, "AUTH_INVALID_API_KEY")
    body = await request.body()
    try:
        data = _unb64u(h["EXN-DATA"])
        payload = json.loads(data)
        PUB.verify(_unb64u(h["EXN-SIGN"]), data)
    except Exception as e:
        return err(401, 1001, f"AUTH_INVALID_SIGNATURE ({type(e).__name__})")
    path_q = request.url.path + ("?" + request.url.query if request.url.query else "")
    idem = h.get("EXN-IDEMPOTENCY-KEY", "")
    checks = [
        (payload.get("api_key") == h["EXN-API-KEY"], "api_key mismatch"),
        (str(payload.get("timestamp")) == h["EXN-TIMESTAMP"], "timestamp mismatch"),
        (abs(int(h["EXN-TIMESTAMP"]) - time.time() * 1000) < 30_000, "stale timestamp"),
        (payload.get("sign_version") == 1 and h["EXN-SIGN-VERSION"] == "1", "sign_version"),
        (payload.get("method") == request.method, "method mismatch"),
        (payload.get("path") == path_q, f"path mismatch {payload.get('path')!r} != {path_q!r}"),
        (payload.get("body_hash") == _b64u(hashlib.sha256(body).digest()), "body_hash mismatch"),
        (payload.get("idempotency_key") == idem, "idempotency_key mismatch"),
        ((idem == "") if request.method == "GET" else (5 <= len(idem) <= 128), "idempotency rule"),
    ]
    for ok, msg in checks:
        if not ok:
            return err(401, 1001, f"AUTH_INVALID_SIGNATURE ({msg})")
    if request.method != "GET":
        if idem in STATE["seen_idem"]:
            return err(409, 1003, "DUPLICATE_IDEMPOTENCY_KEY")
        STATE["seen_idem"].add(idem)
    request.state.body = body
    return await call_next(request)


# ---- market simulation -----------------------------------------------------
def tick():
    for s in PRICES:
        PRICES[s] *= math.exp(rng.gauss(0, 0.0004))
    for p in STATE["positions"].values():
        px = PRICES[p["instrument"]]
        d = (px - p["open_price"]) if p["side"] == "buy" else (p["open_price"] - px)
        p["profit"] = round(d * p["volume"] * COND[p["instrument"]][4], 2)


def spread(s):
    return {"XAUUSD": 0.2, "BTCUSD": 15, "EURUSD": 0.0001, "GBPUSD": 0.00015}[s]


# ---- endpoints -------------------------------------------------------------
@app.get("/v1/trading/access-point")
async def discovery(request: Request, account_id: str):
    return {"access_point": str(request.base_url).rstrip("/"), "account_id": account_id}


@app.get("/v1/configuration/accounts/{acc}/account")
async def account(acc: str):
    return {"account_id": acc, "currency": "USD", "leverage": 2000, "trade_mode": "full_access",
            "account_status": "active", "balance": round(STATE["balance"], 2)}


@app.get("/v1/configuration/accounts/{acc}/limits")
async def limits(acc: str):
    return {"candles_limit": 5000, "requests_per_minute": 300}


@app.get("/v1/configuration/accounts/{acc}/instruments")
async def instruments(acc: str):
    return {"instruments": [{"instrument": s} for s in PRICES]}


@app.get("/v1/configuration/accounts/{acc}/instruments/{ins}/conditions")
async def conditions(acc: str, ins: str):
    if ins not in COND:
        return err(404, 2000, "MARKET_INSTRUMENT_NOT_FOUND")
    vmin, vmax, vstep, digits, contract = COND[ins]
    return {"instrument": ins, "volume_min": str(vmin), "volume_max": str(vmax), "volume_step": str(vstep),
            "point_digits": digits, "contract_size": contract, "trade_mode": "full"}


@app.get("/v1/market-data/accounts/{acc}/candles")
async def candles(acc: str, request: Request):
    q = request.query_params
    ins, tf, pt = q.get("instrument"), q.get("timeframe", "M5"), q.get("price_type", "bid")
    if ins not in PRICES:
        return err(400, 3000, "MARKET_INSTRUMENT_NOT_FOUND")
    count = abs(int(q.get("count", "-300")))
    secs = {"M1": 60, "M3": 180, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600}.get(tf)
    if not secs:
        return err(400, 3001, "REQUEST_INVALID_TIMEFRAME")
    tick()
    now = int(time.time()) // secs * secs
    px = PRICES[ins] + (spread(ins) / 2 if pt == "ask" else -spread(ins) / 2)
    out = []
    r = random.Random(hash((ins, now)))
    for i in range(count, 0, -1):
        c = px; o = c * math.exp(r.gauss(0, 0.0005 * math.sqrt(secs / 60)))
        h, l = max(o, c) * (1 + abs(r.gauss(0, 0.0002))), min(o, c) * (1 - abs(r.gauss(0, 0.0002)))
        out.append({"time": (now - (i - 1) * secs) * 1000, "open": o, "high": h, "low": l, "close": c, "volume": 100})
        px = o
    out.reverse()
    return {"candles": out}


@app.get("/v1/trading/accounts/{acc}/snapshot")
async def snapshot(acc: str):
    tick()
    pos = list(STATE["positions"].values())
    for p in pos:                                     # server-side SL/TP
        px = PRICES[p["instrument"]]
        hit = None
        if p["side"] == "buy":
            if p.get("stop_loss_price") and px <= float(p["stop_loss_price"]): hit = float(p["stop_loss_price"])
            elif p.get("take_profit_price") and px >= float(p["take_profit_price"]): hit = float(p["take_profit_price"])
        else:
            if p.get("stop_loss_price") and px >= float(p["stop_loss_price"]): hit = float(p["stop_loss_price"])
            elif p.get("take_profit_price") and px <= float(p["take_profit_price"]): hit = float(p["take_profit_price"])
        if hit is not None:
            _close(p["id"], hit)
    eq = STATE["balance"] + sum(p["profit"] for p in STATE["positions"].values())
    return {"positions": list(STATE["positions"].values()), "orders": list(STATE["orders"].values()),
            "account_state": {"balance": round(STATE["balance"], 2), "equity": round(eq, 2), "used_margin": 0, "currency": "USD"}}


def _ack(kind: str, result: dict):
    op = str(uuid.uuid4())
    STATE["ops"][op] = {"operation_id": op, "status": "success", "type": kind, **result}
    return JSONResponse(status_code=202, content={"operation_id": op, "status": "accepted"})


def _close(pid: str, price: float):
    p = STATE["positions"].pop(pid)
    d = (price - p["open_price"]) if p["side"] == "buy" else (p["open_price"] - price)
    profit = round(d * p["volume"] * COND[p["instrument"]][4], 2)
    STATE["balance"] += profit
    return {"deal": {"price": price, "profit": profit, "position_id": pid}, "position": {**p, "close_price": price, "profit": profit}}


@app.post("/v1/trading/accounts/{acc}/positions")
async def open_position(acc: str, request: Request):
    b = json.loads(request.state.body)
    ins = b.get("instrument")
    if ins not in PRICES:
        return err(400, 3000, "MARKET_INSTRUMENT_NOT_FOUND")
    vol = float(b["volume"]); vmin, vmax, vstep, digits, _ = COND[ins]
    if vol < vmin or vol > vmax:
        return err(400, 4000, "TRADING_RULE_VOLUME_TOO_SMALL")
    tick()
    side = b["side"]
    px = PRICES[ins] + (spread(ins) / 2 if side == "buy" else -spread(ins) / 2)
    pid = str(1000 + len(STATE["ops"]))
    pos = {"id": pid, "instrument": ins, "side": side, "volume": vol, "open_price": round(px, digits),
           "stop_loss_price": b.get("stop_loss_price"), "take_profit_price": b.get("take_profit_price"),
           "profit": 0.0, "open_time": int(time.time() * 1000), "comment": b.get("comment", "")}
    STATE["positions"][pid] = pos
    return _ack("open", {"position": pos, "deal": {"price": pos["open_price"], "position_id": pid}})


@app.post("/v1/trading/accounts/{acc}/orders")
async def place_order(acc: str, request: Request):
    b = json.loads(request.state.body)
    if b.get("type") == "sltp":
        p = STATE["positions"].get(str(b.get("position_id")))
        if not p:
            return err(404, 4001, "POSITION_NOT_FOUND")
        for k in ("stop_loss_price", "take_profit_price"):
            if k in b:
                p[k] = b[k]
        return _ack("sltp", {"position": p})
    oid = str(uuid.uuid4())[:8]
    STATE["orders"][oid] = {"id": oid, **b}
    return _ack("order", {"order": STATE["orders"][oid]})


@app.patch("/v1/trading/accounts/{acc}/positions/{pid}")
async def patch_position(acc: str, pid: str, request: Request):
    b = json.loads(request.state.body)
    p = STATE["positions"].get(pid)
    if not p:
        return err(404, 4001, "POSITION_NOT_FOUND")
    p.update({k: v for k, v in b.items() if k in ("stop_loss_price", "take_profit_price")})
    return _ack("modify", {"position": p})


@app.delete("/v1/trading/accounts/{acc}/positions/{pid}")
async def close_position(acc: str, pid: str):
    p = STATE["positions"].get(pid)
    if not p:
        return err(404, 4001, "POSITION_NOT_FOUND")
    tick()
    px = PRICES[p["instrument"]] - (spread(p["instrument"]) / 2 if p["side"] == "buy" else -spread(p["instrument"]) / 2)
    return _ack("close", _close(pid, round(px, COND[p["instrument"]][3])))


@app.get("/v1/trading/accounts/{acc}/operations/{op}")
async def operation(acc: str, op: str):
    r = STATE["ops"].get(op)
    return r if r else err(404, 5000, "OPERATION_NOT_FOUND")
