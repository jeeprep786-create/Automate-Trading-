"""IITian Scalper — FastAPI backend + dashboard.

Run:  uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config as C
from .backtest import run_backtest
from .broker import BrokerFeed, PaperBroker, SyntheticFeed, spec_for
from .broker.base import Broker
from .config import AppConfig, SymbolConfig
from .engine import Engine
from .models import utcnow

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")
STATIC = Path(__file__).resolve().parent.parent / "static"


# --------------------------------------------------------------------------- #
# App state
# --------------------------------------------------------------------------- #
class State:
    config: AppConfig
    broker: Broker
    live: Optional[object] = None          # MetaApiBroker when connected
    engine: Engine
    data_source: str = "synthetic"
    connect_error: str = ""


S = State()


async def make_live_broker(backend: str, creds: dict):
    """Instantiate + connect the chosen live backend. Raises on failure."""
    if backend == "exness":
        if not C.exness_configured(creds):
            raise RuntimeError("Exness API key, private key, account ID and Gateway URL are required (Connect API panel)")
        from .broker.exness_broker import ExnessBroker
        b = ExnessBroker(creds["EXN_API_KEY"], creds["EXN_PRIVATE_KEY"], creds["EXN_ACCOUNT_ID"],
                         creds["EXN_DISCOVERY_BASE_URL"], creds["EXN_API_BASE_URL"])
        await asyncio.wait_for(b.connect(), timeout=40)
        return b
    if backend == "metaapi":
        if not C.metaapi_configured(creds):
            raise RuntimeError("METAAPI_TOKEN / METAAPI_ACCOUNT_ID not set")
        from .broker.metaapi_broker import MetaApiBroker
        b = MetaApiBroker(creds["METAAPI_TOKEN"], creds["METAAPI_ACCOUNT_ID"], creds["METAAPI_REGION"])
        await asyncio.wait_for(b.connect(), timeout=90)
        return b
    raise RuntimeError(f"unknown backend {backend}")


async def build_broker(cfg: AppConfig) -> None:
    """(Re)create the broker according to cfg.mode / cfg.backend and stored credentials."""
    creds = C.load_creds()
    if S.live is None or getattr(S.live, "name", "") != cfg.backend:
        if S.live is not None:
            try:
                await S.live.close()
            except Exception:
                pass
            S.live = None
        configured = C.exness_configured(creds) if cfg.backend == "exness" else C.metaapi_configured(creds)
        if configured:
            try:
                S.live = await make_live_broker(cfg.backend, creds)
                S.connect_error = ""
            except Exception as e:
                S.connect_error = f"{type(e).__name__}: {e}"
                log.error("%s connect failed: %s", cfg.backend, S.connect_error)
                S.live = None

    if cfg.mode == "live":
        if S.live is None:
            raise HTTPException(400, f"Live mode needs a working {cfg.backend} connection ({S.connect_error or 'not configured'})")
        S.broker = S.live
        S.data_source = f"{cfg.backend} LIVE"
    else:
        if S.live is not None:
            feed = BrokerFeed(S.live)
            S.data_source = f"{cfg.backend} prices (paper fills)"
        else:
            feed = SyntheticFeed(cfg.paper_spread_pts)
            S.data_source = "synthetic"
        S.broker = PaperBroker(feed, cfg.paper_balance)


@asynccontextmanager
async def lifespan(app: FastAPI):
    S.config = AppConfig.load()
    if S.config.mode == "live":          # never auto-start live; boot in paper and let the user switch
        S.config.mode = "paper"
    await build_broker(S.config)
    S.engine = Engine(S.broker, S.config)
    log.info("Ready. mode=%s data=%s", S.config.mode, S.data_source)
    yield
    if S.engine.running:
        await S.engine.stop("server shutdown")
    if S.live:
        await S.live.close()


app = FastAPI(title="IITian Scalper", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

# --------------------------------------------------------------------------- #
# Optional password (set DASHBOARD_PASSWORD in .env)
# --------------------------------------------------------------------------- #
security = HTTPBasic(auto_error=False)


def auth(creds: Optional[HTTPBasicCredentials] = Depends(security)):
    if not C.DASHBOARD_PASSWORD:
        return True
    if creds and secrets.compare_digest(creds.password, C.DASHBOARD_PASSWORD):
        return True
    raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Basic"})


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.get("/")
async def index(_=Depends(auth)):
    return FileResponse(STATIC / "index.html")


@app.get("/api/status")
async def status(_=Depends(auth)):
    e = S.engine
    acct = await S.broker.account()
    positions = [p.to_dict() for p in await S.broker.positions()]
    return {
        "time": utcnow().isoformat(),
        "mode": S.config.mode,
        "data_source": S.data_source,
        "backend": S.config.backend,
        "exness_configured": C.exness_configured(),
        "metaapi_configured": C.metaapi_configured(),
        "live_connected": S.live is not None,
        "live_backend": getattr(S.live, "name", None),
        "connect_error": S.connect_error,
        "running": e.running, "killed": e.killed, "halt_reason": e.halt_reason,
        "started_at": e.started_at.isoformat() if e.started_at else None,
        "last_tick": e.last_tick.isoformat() if e.last_tick else None,
        "account": acct.__dict__,
        "positions": positions,
        "symbols": e.symbol_state,
        "stats": e.stats(),
        "events": list(e.events)[:60],
    }


@app.get("/api/trades")
async def trades(limit: int = 200, _=Depends(auth)):
    return [t.to_dict() for t in S.engine.trades[-limit:]][::-1]


@app.get("/api/equity")
async def equity(_=Depends(auth)):
    return list(S.engine.equity_curve)


@app.get("/api/candles")
async def candles(symbol: str, timeframe: str = "5m", limit: int = 200, _=Depends(auth)):
    cs = await S.broker.candles(symbol, timeframe, limit)
    return [{"t": int(c.time.timestamp()), "o": c.open, "h": c.high, "l": c.low, "c": c.close, "v": c.volume} for c in cs]


@app.get("/api/config")
async def get_config(_=Depends(auth)):
    return S.config.to_dict()


@app.put("/api/config")
async def put_config(body: dict, _=Depends(auth)):
    was_running = S.engine.running
    new = AppConfig.from_dict({**S.config.to_dict(), **body})
    mode_changed = new.mode != S.config.mode or new.backend != S.config.backend
    if was_running and mode_changed:
        raise HTTPException(400, "Stop the engine before switching mode or backend")
    if mode_changed:
        try:
            await build_broker(new)          # may raise if live backend is not connected
        except HTTPException:
            new.mode = "paper"               # never persist "live" without a working broker
            await build_broker(new)
            S.config = new
            S.config.save()
            S.engine = Engine(S.broker, S.config)
            raise
        S.config = new
        S.config.save()
        S.engine = Engine(S.broker, S.config)
    else:
        S.config = new
        S.config.save()
        S.engine.config = S.config
    return S.config.to_dict()


class Action(BaseModel):
    confirm: Optional[str] = None


@app.post("/api/start")
async def start(body: Action = Action(), _=Depends(auth)):
    if S.config.mode == "live" and body.confirm != "LIVE":
        raise HTTPException(400, 'Live trading: send {"confirm":"LIVE"} to acknowledge real money is at risk')
    await S.engine.start()
    return {"ok": True}


@app.post("/api/stop")
async def stop(_=Depends(auth)):
    await S.engine.stop()
    return {"ok": True}


@app.post("/api/kill")
async def kill(_=Depends(auth)):
    await S.engine.kill()
    return {"ok": True}


@app.post("/api/reconnect")
async def reconnect(_=Depends(auth)):
    """Retry the live connection (after saving credentials, for instance)."""
    if S.engine.running:
        raise HTTPException(400, "Stop the engine first")
    if S.live:
        try:
            await S.live.close()
        except Exception:
            pass
        S.live = None
    await build_broker(S.config)
    S.engine = Engine(S.broker, S.config)
    return {"connected": S.live is not None, "error": S.connect_error, "data_source": S.data_source}


# --------------------------------------------------------------------------- #
# Exness API connection: credentials, test, probe, keygen
# --------------------------------------------------------------------------- #
def _mask(v: str) -> str:
    v = v or ""
    return "" if not v else (v[:6] + "…" + v[-4:] if len(v) > 12 else "•" * len(v))


@app.get("/api/credentials")
async def get_credentials(_=Depends(auth)):
    c = C.load_creds()
    return {
        "EXN_API_KEY": _mask(c["EXN_API_KEY"]), "EXN_PRIVATE_KEY": "set" if c["EXN_PRIVATE_KEY"] else "",
        "EXN_ACCOUNT_ID": c["EXN_ACCOUNT_ID"], "EXN_DISCOVERY_BASE_URL": c["EXN_DISCOVERY_BASE_URL"],
        "EXN_API_BASE_URL": c["EXN_API_BASE_URL"],
        "METAAPI_TOKEN": _mask(c["METAAPI_TOKEN"]), "METAAPI_ACCOUNT_ID": c["METAAPI_ACCOUNT_ID"],
        "METAAPI_REGION": c["METAAPI_REGION"],
        "exness_configured": C.exness_configured(c), "metaapi_configured": C.metaapi_configured(c),
        "stored_in": str(C.CREDS_PATH),
    }


@app.put("/api/credentials")
async def put_credentials(body: dict, _=Depends(auth)):
    """Store keys locally (data/credentials.json, mode 0600). Masked values ('…') are ignored."""
    clean = {k: v for k, v in body.items() if k in C.CRED_KEYS and not (isinstance(v, str) and "…" in v)}
    if "EXN_PRIVATE_KEY" in clean and clean["EXN_PRIVATE_KEY"] not in ("", None):
        from .broker.exness_auth import load_private_key
        try:
            load_private_key(clean["EXN_PRIVATE_KEY"])
        except Exception as e:
            raise HTTPException(400, f"Private key not understood: {e}")
    for k in ("EXN_DISCOVERY_BASE_URL", "EXN_API_BASE_URL"):
        if clean.get(k):
            clean[k] = clean[k].rstrip("/")
            if not clean[k].startswith(("https://", "http://")):
                clean[k] = "https://" + clean[k]
    C.save_creds(clean)
    return await get_credentials()


@app.post("/api/exness/test")
async def exness_test(_=Depends(auth)):
    """Discovery + account read with the stored keys. Returns raw responses so field names can be checked."""
    creds = C.load_creds()
    if not C.exness_configured(creds):
        raise HTTPException(400, "Fill in API key, private key, account ID and Gateway URL first")
    from .broker.exness_broker import ExnessBroker, ExnessError
    b = ExnessBroker(creds["EXN_API_KEY"], creds["EXN_PRIVATE_KEY"], creds["EXN_ACCOUNT_ID"],
                     creds["EXN_DISCOVERY_BASE_URL"], creds["EXN_API_BASE_URL"])
    out = {"ok": False, "access_point": "", "steps": {}}
    try:
        await asyncio.wait_for(b.connect(), timeout=40)
        out["access_point"] = b.access_point
        out["steps"]["discovery"] = b.last_raw.get("discovery")
        out["steps"]["account"] = b.last_raw.get("account")
        for what in ("limits", "snapshot"):
            try:
                out["steps"][what] = await b.raw(what)
            except ExnessError as e:
                out["steps"][what] = {"error": str(e)}
        out["ok"] = True
        if b.access_point and not creds["EXN_API_BASE_URL"]:
            C.save_creds({"EXN_API_BASE_URL": b.access_point})       # cache per docs (discovery is rate-limited)
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    finally:
        await b.close()
    return out


@app.get("/api/exness/probe")
async def exness_probe(what: str, instrument: str = "XAUUSD", timeframe: str = "5m", _=Depends(auth)):
    """Raw response from one endpoint (account | limits | instruments | conditions | snapshot | candles)."""
    live = S.live if getattr(S.live, "name", "") == "exness" else None
    creds = C.load_creds()
    from .broker.exness_broker import ExnessBroker
    b = live or ExnessBroker(creds["EXN_API_KEY"], creds["EXN_PRIVATE_KEY"], creds["EXN_ACCOUNT_ID"],
                             creds["EXN_DISCOVERY_BASE_URL"], creds["EXN_API_BASE_URL"])
    try:
        if not live:
            await asyncio.wait_for(b.connect(), timeout=40)
        return {"what": what, "raw": await b.raw(what, instrument, timeframe)}
    except Exception as e:
        raise HTTPException(502, f"{type(e).__name__}: {e}")
    finally:
        if not live:
            await b.close()


@app.post("/api/exness/keygen")
async def exness_keygen(_=Depends(auth)):
    """Generate an Ed25519 key pair for the "I'll use my own public key" flow in Key Management.
    The private key is returned once to the browser (your own server → your own browser); save it
    via the Connect panel. Nothing is stored until you click Save."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from .broker.exness_auth import public_key_registration_b64
    k = Ed25519PrivateKey.generate()
    pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                          serialization.NoEncryption()).decode()
    return {"private_key_pem": pem, "public_key_registration": public_key_registration_b64(k)}


@app.post("/api/positions/{pid}/close")
async def close_pos(pid: str, _=Depends(auth)):
    t = await S.engine.close_one(pid)
    return {"ok": True, "trade": t.to_dict() if t else None}


@app.post("/api/reset-paper")
async def reset_paper(_=Depends(auth)):
    if S.config.mode != "paper":
        raise HTTPException(400, "Only in paper mode")
    if S.engine.running:
        await S.engine.stop("paper reset")
    await build_broker(S.config)
    S.engine = Engine(S.broker, S.config)
    S.engine.trades = []
    S.engine.equity_curve.clear()
    S.engine._save()
    return {"ok": True}


class BacktestReq(BaseModel):
    symbol: str
    timeframe: str = "5m"
    bars: int = 3000
    balance: float = 10000.0
    overrides: dict = {}


@app.post("/api/backtest")
async def backtest(req: BacktestReq, _=Depends(auth)):
    base = S.config.symbol(req.symbol) or SymbolConfig(req.symbol, req.timeframe)
    d = {**base.__dict__, **{k: v for k, v in req.overrides.items() if k in SymbolConfig.__dataclass_fields__}}
    d["symbol"], d["timeframe"] = req.symbol, req.timeframe
    cfg = SymbolConfig(**d)
    source = S.broker
    cs = await source.candles(req.symbol, req.timeframe, min(req.bars, 5000))
    if len(cs) < 100:
        raise HTTPException(400, f"Only {len(cs)} candles available for {req.symbol} {req.timeframe}")
    spec = await source.spec(req.symbol)
    q = await source.quote(req.symbol)
    result = run_backtest(cs, cfg, spec, req.balance, q.spread)
    result["data_source"] = S.data_source
    return result


@app.exception_handler(Exception)
async def on_error(request: Request, exc: Exception):
    log.exception("Unhandled error")
    return JSONResponse(status_code=500, content={"detail": f"{type(exc).__name__}: {exc}"})
