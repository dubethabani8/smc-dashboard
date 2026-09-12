"""
Live paper-trading detector for the LQ-low-reclaim-after-bullish-BOS setup.

Uses the EXACT validated parameters from the backtest/trade-sim work:
  - CLOSENESS_ATR_MULT = 1.0   (how close price must return to the LQ level)
  - STOP_ATR_MULT = 1.0        (stop-loss distance below entry)
  - PARTIAL_TARGET_ATR_MULT = 2.0   (first take-profit distance above entry)
  - PARTIAL_EXIT_FRACTION = 0.5     (fraction closed at the partial target)
  - BREAKEVEN_BUFFER_ATR_MULT = 0.05
  - MIN_CANDLES_LQ_TO_BOS = SWING_LENGTH (a level needs this many candles to
    even be confirmable - a BOS sooner than that would be trading on
    foresight, same look-ahead fix as the trade simulator)

Two policies enforced here that the independence audit made necessary
before going live at all:

1. ONE TRADE PER LEVEL, EVER. Once a liquidity level (identified by its
   exact origin timestamp, per symbol) has produced one alert, it is
   permanently excluded from producing another - persisted to disk so a
   restart doesn't forget and re-fire. The audit found trades sharing a
   level agree on outcome ~64% of the time vs. ~50% expected by chance -
   repeat visits are correlated risk, not fresh independent signals.

2. Only fully-closed candles are ever evaluated - the most recent candle
   is always dropped before any computation, same rule as the existing
   BOS/CHoCH/liquidity alert engine, so nothing is flagged on a candle
   that could still change.

This is a SEPARATE bot/subscriber list from the production alerts
(telegram_alerts.py) - different token, different state files - so this
still-experimental strategy never mixes with live production alerts.

This sends NOTIFICATIONS ONLY. It does not place any real or demo trades -
paper-tracking the outcome of each alert is a manual (or future) step.
"""
import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pandas as pd

import deriv_client
import smc_engine
from backtest_lq_reclaim import (
    compute_atr, find_candidate_setups, SWING_LENGTH, RANGE_PERCENT,
)

log = logging.getLogger("smc-dashboard.lq-reclaim")

LQ_RECLAIM_BOT_TOKEN = os.getenv("LQ_RECLAIM_BOT_TOKEN", "")
LQ_RECLAIM_ENABLED = bool(LQ_RECLAIM_BOT_TOKEN)

LQ_RECLAIM_GRANULARITY = 900  # 15m, matches everything the setup was validated on
LQ_RECLAIM_HISTORY_COUNT = int(os.getenv("LQ_RECLAIM_HISTORY_COUNT", "2000"))
LQ_RECLAIM_POLL_INTERVAL = int(os.getenv("LQ_RECLAIM_POLL_INTERVAL", "300"))
LQ_RECLAIM_STAGGER = 2.0

# Only check the most recently closed candle(s) as a possible "return"
# event each cycle - a small buffer in case a poll cycle runs slightly
# late and more than one new candle closed since the last check.
CHECK_LAST_N_CANDLES = 3

# Validated setup parameters - keep these in sync with trade_sim_lq_reclaim.py
CLOSENESS_ATR_MULT = 1.0
STOP_ATR_MULT = 1.0
PARTIAL_TARGET_ATR_MULT = 2.0
PARTIAL_EXIT_FRACTION = 0.5
BREAKEVEN_BUFFER_ATR_MULT = 0.05
MIN_CANDLES_LQ_TO_BOS = SWING_LENGTH

# Start narrow: only the symbols this setup was actually validated on.
# Override with a comma-separated LQ_RECLAIM_SYMBOLS env var once you want
# to expand to untested instruments.
_default_symbols = "R_100,R_75,R_50,R_25,CRASH500,BOOM500"
_raw_symbol_filter = os.getenv("LQ_RECLAIM_SYMBOLS", _default_symbols).strip()
LQ_RECLAIM_SYMBOLS = [s.strip() for s in _raw_symbol_filter.split(",") if s.strip()]

DASHBOARD_URL = os.getenv("DASHBOARD_URL", "https://web-production-e22932.up.railway.app").rstrip("/")

SUBSCRIBERS_FILE = Path(__file__).resolve().parent / "lq_reclaim_subscribers.json"
STATE_FILE = Path(__file__).resolve().parent / "lq_reclaim_state.json"

# per-symbol: set of lq_time values that have already produced one alert -
# the permanent one-per-level exclusion list
_used_lq_times: dict[str, set[int]] = {}
# per-symbol: (lq_time, bos_time, return_time) tuples already alerted on -
# ordinary dedup so the same exact detection doesn't repeat across polls
_seen_signals: dict[str, set[tuple[int, int, int]]] = {}
_warmed_up: set[str] = set()

_subscribers: set[str] = set()

HELP_TEXT = (
    "SMC LQ-Reclaim paper-trading alerts (EXPERIMENTAL).\n\n"
    "Each alert marks one paper trade: entry, stop, and partial target are "
    "included. Once a liquidity level has fired one alert it will never "
    "fire again, even on a different breakout - this is a deliberate "
    "one-trade-per-level rule based on backtest correlation findings.\n\n"
    "/start - subscribe\n/stop - unsubscribe"
)


def _format_time(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _chart_link(symbol: str, level: float, event_time: int) -> str:
    # reuses the existing LIQUIDITY deep-link convention so the dashboard
    # highlights the LQ level itself, with zero frontend changes needed
    return (
        f"{DASHBOARD_URL}/?symbol={symbol}&timeframe=15m"
        f"&event=LIQUIDITY&dir=bullish&level={level}&time={event_time}"
    )


def _load_subscribers():
    global _subscribers
    if SUBSCRIBERS_FILE.exists():
        try:
            _subscribers = set(str(c) for c in json.loads(SUBSCRIBERS_FILE.read_text()))
        except Exception:
            log.exception("failed to load lq-reclaim subscribers, starting empty")
            _subscribers = set()
    else:
        _subscribers = set()


def _save_subscribers():
    try:
        SUBSCRIBERS_FILE.write_text(json.dumps(sorted(_subscribers)))
    except Exception:
        log.exception("failed to save lq-reclaim subscribers")


def _load_state():
    global _used_lq_times, _seen_signals, _warmed_up
    if not STATE_FILE.exists():
        return
    try:
        data = json.loads(STATE_FILE.read_text())
        _used_lq_times = {sym: set(times) for sym, times in data.get("used_lq_times", {}).items()}
        _seen_signals = {
            sym: set(tuple(t) for t in sigs)
            for sym, sigs in data.get("seen_signals", {}).items()
        }
        _warmed_up = set(data.get("warmed_up", []))
        log.info("restored lq-reclaim state: %d symbol(s) warmed, %d level(s) used",
                  len(_warmed_up), sum(len(v) for v in _used_lq_times.values()))
    except Exception:
        log.exception("failed to load lq-reclaim state, starting fresh")


def _save_state():
    try:
        data = {
            "used_lq_times": {sym: sorted(list(s)) for sym, s in _used_lq_times.items()},
            "seen_signals": {sym: [list(t) for t in s] for sym, s in _seen_signals.items()},
            "warmed_up": sorted(_warmed_up),
        }
        STATE_FILE.write_text(json.dumps(data))
    except Exception:
        log.exception("failed to save lq-reclaim state")


async def _send_to_chat(chat_id: str, text: str):
    url = f"https://api.telegram.org/bot{LQ_RECLAIM_BOT_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json={
                "chat_id": chat_id, "text": text, "parse_mode": "HTML",
                "disable_web_page_preview": True,
            })
            if resp.status_code != 200:
                log.warning("lq-reclaim telegram send failed for %s: %s %s",
                            chat_id, resp.status_code, resp.text)
    except Exception:
        log.exception("lq-reclaim telegram send raised for %s", chat_id)


async def send_lq_reclaim_message(text: str):
    if not LQ_RECLAIM_ENABLED or not _subscribers:
        return
    for chat_id in list(_subscribers):
        await _send_to_chat(chat_id, text)


async def _check_symbol(symbol: str):
    try:
        candles = await deriv_client.fetch_candle_history(
            symbol, LQ_RECLAIM_GRANULARITY, LQ_RECLAIM_HISTORY_COUNT + 1
        )
        if len(candles) < 50:
            return
        candles = candles[:-1]  # drop the still-forming candle, closed candles only
        df = smc_engine.build_dataframe(candles)
        atr_series = compute_atr(df)
        overlays = await asyncio.to_thread(
            smc_engine.compute_all, df, swing_length=SWING_LENGTH, range_percent=RANGE_PERCENT
        )
        candidates = await asyncio.to_thread(find_candidate_setups, df, overlays)
    except Exception:
        log.exception("lq-reclaim check failed for %s", symbol)
        return

    first_pass = symbol not in _warmed_up
    used_levels = _used_lq_times.setdefault(symbol, set())
    seen = _seen_signals.setdefault(symbol, set())

    time_to_idx = {t: i for i, t in enumerate(df["time"])}
    n = len(df)
    recent_cutoff_idx = max(0, n - CHECK_LAST_N_CANDLES)

    for c in candidates:
        ret_idx = c["return_idx"]
        if ret_idx < recent_cutoff_idx:
            continue  # not a recent event this cycle cares about

        sig_key = (c["lq_time"], c["bos_time"], c["return_time"])
        if sig_key in seen:
            continue
        seen.add(sig_key)

        if first_pass:
            continue  # don't alert on history that existed before we started watching

        if c["lq_time"] in used_levels:
            continue  # one-trade-per-level: this level already produced an alert

        lq_idx = time_to_idx.get(c["lq_time"])
        bos_idx = time_to_idx.get(c["bos_time"])
        if lq_idx is None or bos_idx is None:
            continue
        if bos_idx - lq_idx < MIN_CANDLES_LQ_TO_BOS:
            continue  # BOS happened before the level would have been confirmable

        atr_at_entry = atr_series.iloc[ret_idx]
        if pd.isna(atr_at_entry):
            continue  # still in ATR warm-up

        closeness_abs = CLOSENESS_ATR_MULT * atr_at_entry
        if c["dist_abs"] > closeness_abs:
            continue

        entry_price = df["close"].iloc[ret_idx]
        stop_price = entry_price - STOP_ATR_MULT * atr_at_entry
        target_price = entry_price + PARTIAL_TARGET_ATR_MULT * atr_at_entry
        breakeven_price = entry_price + BREAKEVEN_BUFFER_ATR_MULT * atr_at_entry

        # mark this level used NOW, before sending - a send failure shouldn't
        # let the same level be retried and potentially double-fire
        used_levels.add(c["lq_time"])
        _save_state()

        ts = _format_time(c["return_time"])
        link = _chart_link(symbol, c["lq_level"], c["lq_time"])
        text = (
            f"\U0001F7E2 <b>LQ-RECLAIM signal</b> (paper) - <b>{symbol}</b>\n"
            f"   Entry: {entry_price:.4f}\n"
            f"   Stop: {stop_price:.4f}  |  Target (partial): {target_price:.4f}\n"
            f"   Breakeven (after partial): {breakeven_price:.4f}\n"
            f"   {ts} \u00b7 <a href=\"{link}\">view LQ level on chart</a>"
        )
        await send_lq_reclaim_message(text)
        log.info("lq-reclaim alert sent: %s entry=%.4f stop=%.4f target=%.4f",
                  symbol, entry_price, stop_price, target_price)

    # cap memory growth, same pattern as the production alert engine
    if len(used_levels) > 500:
        _used_lq_times[symbol] = set(sorted(used_levels)[-300:])
    if len(seen) > 1000:
        _seen_signals[symbol] = set(sorted(seen, key=lambda k: k[2])[-500:])

    _warmed_up.add(symbol)
    _save_state()


async def run_lq_reclaim_watcher():
    if not LQ_RECLAIM_ENABLED:
        log.info("LQ-reclaim alerts disabled (no LQ_RECLAIM_BOT_TOKEN set)")
        return

    _load_subscribers()
    _load_state()
    log.info("lq-reclaim watcher starting, %d subscriber(s), symbols: %s",
              len(_subscribers), LQ_RECLAIM_SYMBOLS)

    while True:
        cycle_start = time.monotonic()
        for symbol in LQ_RECLAIM_SYMBOLS:
            await _check_symbol(symbol)
            await asyncio.sleep(LQ_RECLAIM_STAGGER)

        elapsed = time.monotonic() - cycle_start
        remaining = max(0.0, LQ_RECLAIM_POLL_INTERVAL - elapsed)
        await asyncio.sleep(remaining)


async def run_lq_reclaim_command_listener():
    if not LQ_RECLAIM_ENABLED:
        return

    offset = None
    url = f"https://api.telegram.org/bot{LQ_RECLAIM_BOT_TOKEN}/getUpdates"

    while True:
        try:
            params = {"timeout": 25}
            if offset is not None:
                params["offset"] = offset
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(url, params=params)
                data = resp.json()
        except Exception:
            log.exception("lq-reclaim command listener failed, retrying in 5s")
            await asyncio.sleep(5)
            continue

        for update in data.get("result", []):
            offset = update["update_id"] + 1
            message = update.get("message") or update.get("edited_message")
            if not message:
                continue
            chat_id = str(message["chat"]["id"])
            text = (message.get("text") or "").strip().lower()

            if text.startswith("/start"):
                if chat_id not in _subscribers:
                    _subscribers.add(chat_id)
                    _save_subscribers()
                await _send_to_chat(chat_id,
                    "Subscribed to LQ-Reclaim paper-trading alerts (experimental).\n\n"
                    "Send /stop any time to unsubscribe.")
            elif text.startswith("/stop"):
                if chat_id in _subscribers:
                    _subscribers.discard(chat_id)
                    _save_subscribers()
                await _send_to_chat(chat_id, "Unsubscribed. Send /start any time to resume.")
            else:
                await _send_to_chat(chat_id, HELP_TEXT)
