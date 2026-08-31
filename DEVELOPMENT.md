# Dev notes

Setup/deploy stuff for this repo. If you just want to use the dashboard,
`README.md` is the one you want instead.

Stack: FastAPI backend that talks to Deriv over websocket and runs the SMC
calcs (`smartmoneyconcepts` package), plain JS frontend using
lightweight-charts, served as static files from the same FastAPI app.

```
backend/    FastAPI app - deriv connection, SMC calcs, streams over its own websocket
frontend/   single page, vanilla JS + lightweight-charts
```

## Deriv app_id

Need to register an app on developers.deriv.com to get one. App IDs there
are alphanumeric strings now (like `34eYt3MX5g02E8iqZlXmi`), not the short
numbers you'll see in older docs - they migrated their whole API at some
point.

1. developers.deriv.com → log in → Dashboard → Registered apps → Create new app
2. Name it whatever, redirect URL doesn't actually matter (this app never
   does OAuth, only hits the public market data endpoints), scopes can stay
   minimal since nothing here places trades
3. Grab the App ID from the table

Set it as `DERIV_APP_ID`. Only ever calls the no-auth market data endpoints
(`active_symbols`, `ticks_history`, `ohlc`) on
`wss://api.derivws.com/trading/v1/options/ws/public`, identified via a
`Deriv-App-ID` header. Never touches your actual account/token.

## Running locally

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export DERIV_APP_ID=your_app_id_here
uvicorn main:app --reload --port 8000
```

localhost:8000 should show the symbol list and start streaming within a
couple seconds.

## Deploying

Running on Railway currently: https://web-production-e22932.up.railway.app

Their free tier ended up being the right call - 30 day trial rolls into an
ongoing free plan that's enough for one small always-on service, plus you
get auto-redeploy on push which is nice. (There's still a `Dockerfile` and
`fly.toml` in here from when I was considering Fly instead, kept in case I
ever want to switch.)

1. railway.app, sign in with GitHub
2. New Project → Deploy from GitHub repo → this repo
3. Builds from the root Dockerfile automatically (`railway.json` pins the
   builder to Dockerfile explicitly)
4. Variables tab → `DERIV_APP_ID` = your app id
5. Settings → Networking → Generate Domain

Push to main after that and it redeploys on its own.

### Stuff that went wrong getting this running (keeping notes in case it happens again)

- **backend/ tracked as an empty git submodule** - happened because I ran
  `git init` inside `backend/` at some point before setting up the repo
  properly at the root. Git silently tracks a folder like that as a
  submodule pointer (mode `160000`) instead of real files - looks totally
  normal in `git status` but the actual content is missing wherever it gets
  cloned/deployed. Check with `git ls-files -s backend` - if you see
  `160000` instead of `100644`, that's it. Fix:
  ```
  rm -rf backend/.git
  git rm -r --cached backend
  git add backend
  ```

- **Railway's "Custom Start Command" field overrides everything** - Procfile,
  Dockerfile CMD, doesn't matter, if there's something set there it wins.
  Kept getting `Could not import module "main"` errors that made no sense
  until I found this - it was left over from an earlier attempt. Settings →
  Deploy → Start Command, needs to be completely blank.

- **Railway reused a stale build** at one point even after new commits -
  same image digest across supposedly different builds. Turned out it
  caches based on the actual copied file contents, not the Dockerfile
  instructions, so a Dockerfile-only change wasn't enough to bust it.
  Touching an actual file inside backend/ forced a real rebuild.

- **uvicorn needs `--app-dir backend`, not `cd backend &&`** - some of
  Railway's build paths run the start command directly instead of through a
  shell, and `cd` isn't an actual executable in that context, so the `&&`
  chain just fails. `--app-dir` sidesteps the whole problem since it tells
  uvicorn directly where to look instead of relying on cwd.

## A few implementation notes

- No real volume on Deriv synthetics, so the order block "strength" number
  is derived from candle range/body size instead - relative measure, not
  actual order flow.
- Overlays recompute at most once a second as new candles stream in, just
  to keep CPU sane. Candle prices themselves still update every tick.
- Swing length / liquidity % are adjustable live from the UI.

## Telegram alerts

Broadcasts BOS, CHoCH, and liquidity sweep events across synthetic symbols
to whoever's subscribed. Anyone subscribes by messaging the bot `/start`,
leaves with `/stop` - handled by `backend/telegram_alerts.py`, polling every
`ALERT_POLL_INTERVAL` seconds (default 300s / 5min), one digest message per
sweep rather than a message per event (30-50 symbols individually alerting
gets unusable fast).

Env vars:
- `TELEGRAM_BOT_TOKEN` - required to enable alerts at all, get one from @BotFather
- `TELEGRAM_CHAT_ID` - optional, auto-subscribes that one chat on top of whoever's used /start
- `ALERT_SYMBOLS` - optional comma-separated Deriv symbol codes (e.g.
  `R_100,R_75,BOOM1000`) to restrict alerts to a specific watchlist instead
  of every synthetic index. Leave unset for everything.
- `ALERT_GRANULARITY` - seconds, default 300 (5m). Alerts run on a fixed
  timeframe independent of whatever's open on the dashboard.
- `ALERT_POLL_INTERVAL` - seconds between sweeps, default 300.

**Known limitations:**
- Subscriber list and dedup state (`subscribers.json`, `alert_state.json`)
  live on the container's local disk. This survives an in-process
  crash/restart but **not** a fresh deploy - Railway rebuilds the container
  from scratch each `git push`, wiping both files. So right after a deploy,
  the first sweep is silently treated as a fresh baseline (no alerts), which
  is fine, but subscribers do need to /start again after a redeploy wipes
  the file. If this needs to survive redeploys properly, that means external
  storage (a Railway volume, or a tiny hosted key-value store) instead of
  local JSON files.
- The heavy per-symbol computation runs via `asyncio.to_thread` so it
  doesn't block the event loop (was previously causing the whole app to
  become unresponsive to health checks and get restarted mid-sweep, which is
  also what caused duplicate alerts before this was fixed - each restart
  wiped in-memory dedup state and re-alerted the same still-recent events).
- This doesn't place trades, purely visual. If I ever extend it further:
  alerts on new BOS/CHoCH or liquidity sweeps, multi-symbol watchlist, or
  (bigger undertaking) wiring in actual order placement - that last one
  would need real risk controls built in first, not something to rush.
