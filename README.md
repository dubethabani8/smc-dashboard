# SMC Dashboard — live Smart Money Concepts chart for Deriv synthetic indices

A live-updating trading chart for Deriv's Volatility/Boom/Crash/Step/Jump indices,
with Smart Money Concepts overlays (swing highs/lows, BOS/CHoCH, Fair Value Gaps,
Order Blocks, Liquidity) computed by the
[`smartmoneyconcepts`](https://github.com/joshyattridge/smart-money-concepts) package,
rendered with [lightweight-charts](https://github.com/tradingview/lightweight-charts).

Pick any synthetic index and any timeframe from the dropdowns in the top bar; the
chart streams live and recomputes the overlays as new candles form.

```
backend/    FastAPI app: talks to Deriv over WebSocket, runs the SMC indicators,
            streams candles + overlays to the browser over its own WebSocket.
frontend/   Single-page dashboard (vanilla JS + lightweight-charts), served as
            static files by the same FastAPI app - one deployable unit, one URL.
```

## 1. Get a Deriv `app_id` (5 minutes, uses your existing account)

Deriv migrated their API in 2026 to a new platform at **developers.deriv.com**.
App IDs there are alphanumeric strings (e.g. `34eYt3MX5g02E8iqZlXmi`), not the
short numbers older Deriv docs still floating around the web show — that's
expected, not a mistake.

1. Go to **[developers.deriv.com](https://developers.deriv.com)** → **Dashboard**
   → **Registered apps** → **Create new app**.
2. Log in with the same account you already use for manual trading.
3. Fill in the app registration form:
   - **Name**: anything, e.g. `smc-dashboard`
   - **Redirect URL**: required by the form but unused by this project (we
     never trigger OAuth) — any placeholder works, e.g. your future deployed
     URL or `https://example.com`.
   - **Scopes**: leave minimal/read-only — this app never places trades.
4. Submit — you'll get an **App ID** (the alphanumeric string) in the
   **Registered Apps** table. That's the only value this project needs.

Set it as the `DERIV_APP_ID` environment variable (see below).

> This app never asks for or stores your API token/login. It only ever calls
> Deriv's public, no-auth market-data endpoints (`active_symbols`,
> `ticks_history`, `ohlc` subscription) on `wss://api.derivws.com/trading/v1/options/ws/public`,
> identifying itself only via the `Deriv-App-ID` header.

## 2. Run it locally first

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export DERIV_APP_ID=your_app_id_here
uvicorn main:app --reload --port 8000
```

Open **http://localhost:8000** — you should see the symbol list populate and a
live chart start streaming within a couple of seconds.

## 3. Push to GitHub

```bash
git init
git add .
git commit -m "Initial SMC dashboard"
git branch -M main
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

## 4. Auto-deploy on Railway (recommended)

Railway was picked over Vercel because this app holds a **persistent WebSocket
connection** to Deriv and streams to the browser continuously — that needs a
long-running server process, which serverless platforms like Vercel aren't built
for. Railway runs your app as an always-on service and re-deploys automatically
on every `git push`.

1. Go to [railway.app](https://railway.app) → sign in with GitHub.
2. **New Project → Deploy from GitHub repo** → pick this repo.
3. Railway auto-detects Python via the root `requirements.txt` and uses the
   `Procfile`'s start command — no extra config needed.
4. Open the service → **Variables** tab → add:
   - `DERIV_APP_ID` = your app_id from step 1
5. **Settings → Networking → Generate Domain** to get a public URL
   (`your-app.up.railway.app`).
6. From now on, every `git push` to `main` redeploys automatically.

Railway's free trial credit is enough to try this; a persistent WebSocket app
like this one typically needs their ~$5/month Hobby plan for continuous uptime
beyond the trial (their free tier has been known to sleep/limit long-lived
services — check Railway's current pricing page, it does change).

**Alternatives** if you'd rather not use Railway: **Render** (Web Service, not
the free tier — free sleeps after inactivity, bad for a live chart) or **Fly.io**
(more setup, but full control and cheap always-on VMs). The code doesn't change
between any of these — only the deploy target does.

## Notes, limits, and what to build next

- **No real volume**: Deriv's synthetic indices don't have real traded volume.
  The Order Block "strength" score is computed from a synthesized activity proxy
  (candle range/body size), not real order flow — treat it as relative, not
  literal.
- **Recompute cadence**: overlays recompute at most once per second as candles
  update, to keep CPU usage sane; the candle itself still updates live every tick.
- **Swing length / liquidity range %** are adjustable live from the top bar —
  lower swing length = more (noisier) structure on lower timeframes; raise it on
  higher timeframes.
- **This is analysis support, not an execution bot** — it doesn't place trades.
  Good next steps if you want to go further: alerts (Telegram/webhook) on new
  BOS/CHoCH or liquidity sweeps, a multi-symbol watchlist view, or (bigger step)
  wiring in Deriv's authenticated endpoints to place trades from signals — that
  would need real risk controls and is a separate, more careful project.
