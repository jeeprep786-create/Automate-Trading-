# IITian Scalper — automated scalping dashboard for Exness

A self-hosted website that connects to your Exness trading account through an API
key, runs an EMA/RSI/ATR scalping strategy on XAUUSD, BTCUSD, EURUSD, GBPUSD (or
anything you add), and places trades automatically with a stop-loss, a take-profit
and a trailing stop. It also has a paper-trading mode and a backtester that runs the
exact same code, so you can test before any real money is involved.

Two broker backends, switchable in Settings:

```
A) Exness Trader API (native, default)          B) MetaApi (cloud MT5 gateway, optional)
┌──────────────┐  Ed25519-signed REST  ┌────────┐  ┌──────────────┐ REST ┌─────────┐ MT5 ┌────────┐
│ This website │ ────────────────────▶ │ Exness │  │ This website │ ───▶ │ MetaApi │ ──▶ │ Exness │
└──────────────┘  EXN-* headers        └────────┘  └──────────────┘      └─────────┘     └────────┘
```

No MetaTrader terminal, no Windows: the native backend talks to Exness directly over
HTTPS with requests signed by your private key.

---

## 0. Connect your Exness API (native backend)

1. In the Exness Personal Area open **Key Management** → create an API key for your
   trading account (demo first!). Two options:
   * **Exness generates the key pair** → copy the API key identifier (`exnsk-…`) and
     the 128-hex private signing key.
   * **Your own key pair** → in the dashboard click **Generate my own key pair**, paste
     the shown public key into "I'll use my own public key", keep the private key.
2. Copy the **Gateway host** from the DNS widget in Key Management.
3. Open the dashboard → **Connect Exness API** panel → fill in API key, account ID,
   Gateway URL, private key → **Save keys** (stored only in `data/credentials.json`,
   file mode 0600, on your server) → **Test connection**. The test runs discovery
   (`/v1/trading/access-point`), reads the account, limits and trading snapshot, and
   shows the raw JSON. The resolved Access Point is cached, as the docs require.
4. Settings → **Reconnect**. Paper mode now uses real Exness prices; switch Mode to
   **live** when you're ready.

Alternatively put the same values in `.env` (`EXN_API_KEY`, `EXN_PRIVATE_KEY`,
`EXN_ACCOUNT_ID`, `EXN_DISCOVERY_BASE_URL`).

**One thing to verify once.** The docs portal publishes every endpoint path, the
signing protocol and the validation rules (all implemented exactly), but it renders
the JSON *field names* of request/response bodies client-side, so a few of them in
`app/broker/exness_schema.py` are marked `VERIFY` (e.g. whether the order body uses
`side: "buy"` or something else, and the candle query parameter names). Use the
**Probe** panel to fetch raw responses from your account, compare with
<https://www.exness-api.com/openapi/trading.public.yaml>, and override any name in
`data/exness_schema.json` — no code change needed. Readers already accept several
alternative names, so most things work even before you do this.

### Test the whole flow without an Exness account

`tools/mock_exness.py` is a mock Trader API that enforces the documented auth rules
(signature, body hash, path, idempotency, timestamp) and simulates fills/SL/TP:

```bash
uvicorn tools.mock_exness:app --port 9001     # prints a private key PEM on startup
# Dashboard → Connect panel: API key "exnsk-mock", account "123456",
# Gateway URL "http://localhost:9001", paste the printed PEM → Save → Test.
```

---

## 1. Run it (paper mode, no credentials needed)

```bash
./run.sh            # Linux / macOS / WSL
# Windows PowerShell:
#   python -m venv .venv ; .\.venv\Scripts\activate ; pip install -r requirements.txt
#   copy .env.example .env ; uvicorn app.main:app --port 8000
```

Open http://localhost:8000 . With no MetaApi credentials it runs on a **synthetic
price feed** so you can see the engine, dashboard, trailing stops and backtester
work. Synthetic backtest numbers prove the plumbing, **not** the strategy — the fake
feed is smoother than real gold.

Docker: `docker compose up -d` (put your `.env` next to the compose file).

## 2. Alternative: MetaApi backend (MT5 gateway)

Only needed if you prefer the MT5 route or the native API isn't enabled for your
account type. Settings → Broker backend → MetaApi.

1. Open an Exness MT5 account — **start with a demo account**.
2. Go to https://app.metaapi.cloud → *Accounts* → *Add account* → MetaTrader 5,
   enter your Exness MT5 login, server (e.g. `Exness-MT5Trial8` / `Exness-MT5Real12`)
   and password. Your password lives in MetaApi's vault, never in this project.
3. Copy the **Account ID** shown there and your **API token** from
   https://app.metaapi.cloud/token .
4. Put them in `.env`:
   ```
   METAAPI_TOKEN=eyJ...
   METAAPI_ACCOUNT_ID=xxxxxxxx-xxxx-....
   ```
5. Restart (or click **Reconnect MetaApi** in Settings). The header will say
   `data: metaapi (paper fills)` — real Exness prices, simulated orders.

## 3. Go live

Settings → Mode → **live** → Save → **Start** (type `LIVE` to confirm). Orders now
go to your Exness account through whichever backend is connected. Native-API
orders are market executions with `stop_loss_price` / `take_profit_price` attached,
a max deviation of 50 points, and the comment `IITianScalper` so the bot only ever
manages its own positions. Do this on a **demo account first** for at
least a couple of weeks of forward testing.

Safety rails that are always on:

| Rail | Default | Where |
|---|---|---|
| Risk per trade | 0.5 % of balance (ATR-based SL sizing) | per symbol |
| Max daily loss | 3 % → no new entries until next UTC day | Settings |
| Max daily trades | 30 | Settings |
| Max open positions | 3 account-wide, 1 per symbol | Settings |
| Trading hours | configurable UTC window + weekdays | Settings |
| Spread guard | skip entry if spread > 0.35 × ATR | per symbol |
| Cooldown | 2 bars after any closed trade | per symbol |
| Kill switch | closes every bot position and stops the engine | header |
| Boot safety | server always boots in paper mode; live must be re-enabled by you | — |
| Live guard | mode can't be saved as live unless the broker connection actually works | — |

The bot only touches positions it opened itself (magic number `786110`); your
manual trades on the same account are ignored.

## 4. The strategy ("classic scalp set")

Evaluated on each **closed** bar of the symbol's timeframe:

* **Entry long** — EMA 9 crosses above EMA 21, close above EMA 50 (trend filter),
  RSI 14 between 50 and 70. Optional extra gates: price above session VWAP,
  Supertrend bullish, minimum ATR %, maximum spread.
* **Entry short** — mirror image.
* **Stop-loss** — `entry ∓ ATR(14) × 1.5`. **Take-profit** — SL distance × 1.5 (R:R 1.5).
* **Position size** — `balance × risk% / (SL distance × contract size)`, rounded to
  the broker's lot step.
* **Trailing stop** — at +1R the SL jumps to break-even (+10 % of risk); from +1R the
  stop trails `ATR × 1.0` behind the best price reached (chandelier), moving only
  when it improves by ≥ 0.25 ATR so the broker isn't spammed.
* **Opposite cross** closes the position early (can be disabled).

Everything above is a per-symbol setting in the dashboard; defaults are in
`app/config.py`. Gold runs on M1, the rest on M5. Exness Standard symbols carry an
`m` suffix (`XAUUSDm`); change the names in Settings if your account type differs
(`XAUUSD`, `XAUUSDz`, …).

## 5. Backtest

Dashboard → *Backtest*: pick symbol / timeframe / bars. It pulls up to 5 000
candles from the current data source and runs `app/backtest/backtester.py`, which
calls the same `evaluate()` / `stops_for()` / `trail_level()` the live engine uses.
Fills are modelled at next-bar open ± half spread; SL beats TP when both touch in
one bar (conservative). Stats: win rate, profit factor, avg R, max drawdown, exit
reasons (tp / sl / trail / signal) and an equity curve.

## 6. Project layout

```
app/
  main.py                 FastAPI routes + dashboard serving
  config.py               .env secrets + data/config.json settings (dataclasses)
  models.py               Candle / Quote / Position / Trade / Signal
  broker/
    base.py               Broker interface
    exness_auth.py        EXN-* request signing (Ed25519), key loading, keygen
    exness_schema.py      endpoint paths + JSON field-name map (override in data/exness_schema.json)
    exness_broker.py      native Exness Trader API execution (discovery, snapshot, orders, ACK polling)
    metaapi_broker.py     alternative: live execution via metaapi-cloud-sdk
    paper.py              simulated fills, SL/TP, P&L
    feeds.py              SyntheticFeed, BrokerFeed, candle aggregation
  strategy/
    indicators.py         EMA, RSI, ATR, VWAP, Supertrend (numpy)
    scalper.py            evaluate() / stops_for() / trail_level()
  engine/
    bot.py                polling loop, entries, trailing, journal, events
    risk.py               lot sizing, trading window, daily stats
  backtest/backtester.py  bar-by-bar simulator
static/index.html         the dashboard (vanilla JS + lightweight-charts)
data/                     config.json, trades.json, equity.json (auto-created)
```

API (all JSON): `GET /api/status`, `/api/trades`, `/api/equity`, `/api/candles`,
`/api/config`, `/api/credentials`, `/api/exness/probe`; `PUT /api/config`,
`/api/credentials`; `POST /api/start`, `/api/stop`, `/api/kill`, `/api/reconnect`,
`/api/reset-paper`, `/api/backtest`, `/api/exness/test`, `/api/exness/keygen`,
`/api/positions/{id}/close`.

Native-API notes: mutating calls return a 202 ACK with `operation_id`; the bot polls
`/operations/{id}` for the final result and treats the trading snapshot as the source
of truth for open positions. Quotes are taken from the latest M1 bid/ask candles (no
REST tick endpoint is documented); a WebSocket tick feed is the natural next upgrade.

## 7. Hosting it as a real website

**Fastest (10 minutes, gives you an https link):**

* **Fly.io** — install `flyctl`, then in this folder:
  `fly launch --copy-config --no-deploy` → `fly volumes create data -s 1` →
  `fly secrets set DASHBOARD_PASSWORD=yourpassword` → `fly deploy`.
  Your site is `https://iitian-scalper.fly.dev` (pick a free name if taken).
* **Render.com** — push this folder to a GitHub repo, New → Blueprint → select the repo;
  `render.yaml` sets up the Docker service and a persistent disk. Set
  `DASHBOARD_PASSWORD` in the Render dashboard. Link: `https://iitian-scalper.onrender.com`.
  Use the Starter plan, not Free — free instances sleep and the engine stops.

**Classic VPS (cheapest long-term):**

Any Linux VPS (₹300–500/month tier is plenty): `docker compose up -d`, then put
Caddy or nginx in front with HTTPS and set `DASHBOARD_PASSWORD` in `.env`. The
engine keeps running whether or not the page is open. Keep the VPS near MetaApi's
region you chose (`METAAPI_REGION`: `new-york`, `london`, `singapore`).

## 8. Honest notes

* Scalping on a retail spread is a thin edge; most EMA-cross systems are roughly
  break-even on real tick data once spread and slippage are included. The
  backtester and paper mode exist so you find that out with zero rupees at risk.
* Live P&L uses the broker's own numbers (including commission and swap). Paper
  P&L assumes USD-quoted symbols on a USD account.
* Set **allowed IPs** on the API key in Key Management to your server's IP, and set
  `DASHBOARD_PASSWORD` before exposing the site to the internet. Revoke the key from
  Key Management if you ever suspect a leak.
* MetaApi's free tier covers one account; check their pricing for more.
* This is software, not advice. You are the one pressing Start.
