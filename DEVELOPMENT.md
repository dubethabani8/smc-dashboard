# SMC Dashboard — Development & Deployment

> This is the technical setup/deploy guide. For how to *use* the running
> dashboard, see `README.md` instead.

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

## 4. Deploy for free on Fly.io

**Why Fly.io over Railway/Render**: Railway's free tier is a one-time trial
credit, not permanently free. Render's free tier is permanently free but
**sleeps after 15 minutes of inactivity** and takes ~50s to wake up — bad for
a chart meant to stream live continuously. Fly.io has a genuine small
always-on free allowance with no forced sleep, which is the closest fit to
"free but actually always-on" for an app holding a persistent WebSocket
connection. One caveat: Fly requires a card on file even to stay within the
free allowance (anti-abuse measure) — you won't be charged as long as you
stay under the limit, but it's not literally card-free to sign up.

### One-time setup

1. Install the Fly CLI (PowerShell):
   ```powershell
   iwr https://fly.io/install.ps1 -useb | iex
   ```
   Close and reopen your terminal after this so `fly` is on your PATH.
2. Sign up / log in:
   ```powershell
   fly auth signup
   # or, if you already have an account:
   fly auth login
   ```

### Deploy

From the project root (`smc-dashboard`, the folder with `Dockerfile` and
`fly.toml` in it):

```powershell
fly launch --no-deploy
```
- It'll detect the existing `fly.toml`/`Dockerfile` and mostly just ask you to
  confirm — say **no** to adding a Postgres/Redis database, this app doesn't
  use one.
- Pick a region close to you (default `iad` = US East is fine to start, but
  closer to Deriv's/your location may reduce latency).

Set your Deriv App ID as a secret (this is the equivalent of the
`DERIV_APP_ID` environment variable):
```powershell
fly secrets set DERIV_APP_ID=34eYt3MX5g02E8iqZlXmi
```

Then deploy:
```powershell
fly deploy
```

Once it finishes, open it:
```powershell
fly open
```

That gives you a public URL like `https://smc-dashboard.fly.dev`.

### Redeploying after changes

Fly.io doesn't auto-redeploy on `git push` the way Railway does — you deploy
explicitly:
```powershell
fly deploy
```
This is a deliberate trade-off for staying on the free tier: Railway's
auto-deploy-on-push convenience isn't available on Fly's free allowance in
the same way. If auto-deploy-on-push matters more to you than staying fully
free, Railway (paid Hobby plan, ~$5/mo) is the better fit — the same
`Procfile`/`requirements.txt` in this repo work there too, no code changes
needed, see the alternative instructions further below.

### Checking on it / logs

```powershell
fly status       # is it running
fly logs         # live logs, useful for debugging the Deriv connection
```

---


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
