import asyncio
import logging
import os
import time

import httpx
import pandas as pd

import deriv_client
import smc_engine

log = logging.getLogger("smc-dashboard.alerts")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
ALERTS_ENABLED = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)

ALERT_GRANULARITY = int(os.getenv("ALERT_GRANULARITY", "300"))  # 5m default
ALERT_SWING_LENGTH = int(os.getenv("ALERT_SWING_LENGTH", "10"))
ALERT_RANGE_PERCENT = float(os.getenv("ALERT_RANGE_PERCENT", "0.01"))
ALERT_POLL_INTERVAL = int(os.getenv("ALERT_POLL_INTERVAL", "300"))  # seconds between full sweeps
ALERT_HISTORY_COUNT = int(os.getenv("ALERT_HISTORY_COUNT", "300"))  # smaller than the chart's, this only needs recent bars
ALERT_STAGGER = 2.0  # seconds between checking each symbol, spreads load out over the poll window

# per-symbol memory of event times we've already alerted on, so we don't repeat
_seen_bos_choch: dict[str, set[tuple[int, str]]] = {}
_seen_liquidity_swept: dict[str, set[int]] = {}
_warmed_up: set[str] = set()


async def send_telegram_message(text: str):
    if not ALERTS_ENABLED:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            })
            if resp.status_code != 200:
                log.warning("telegram send failed: %s %s", resp.status_code, resp.text)
    except Exception:
        log.exception("telegram send raised")


def _format_symbol(symbol: str, display_name: str) -> str:
    return display_name or symbol


async def _check_symbol(symbol: str, display_name: str):
    try:
        candles = await deriv_client.fetch_candle_history(symbol, ALERT_GRANULARITY, ALERT_HISTORY_COUNT)
        if len(candles) < 20:
            return
        df = smc_engine.build_dataframe(candles)
        overlays = smc_engine.compute_all(df, swing_length=ALERT_SWING_LENGTH, range_percent=ALERT_RANGE_PERCENT)
    except Exception:
        log.exception("alert check failed for %s", symbol)
        return

    first_pass = symbol not in _warmed_up
    seen_bc = _seen_bos_choch.setdefault(symbol, set())
    seen_lq = _seen_liquidity_swept.setdefault(symbol, set())
    name = _format_symbol(symbol, display_name)

    for item in overlays["bos_choch"]:
        key = (item["time"], item["kind"])
        if key in seen_bc:
            continue
        seen_bc.add(key)
        if first_pass:
            continue
        arrow = "\U0001F7E2" if item["direction"] == "bullish" else "\U0001F534"
        await send_telegram_message(
            f"{arrow} <b>{item['kind']}</b> ({item['direction']}) on <b>{name}</b>\n"
            f"level {item['level']:.4f}"
        )

    for item in overlays["liquidity"]:
        if not item.get("swept_time"):
            continue
        key = item["swept_time"]
        if key in seen_lq:
            continue
        seen_lq.add(key)
        if first_pass:
            continue
        arrow = "\U0001F7E2" if item["direction"] == "bullish" else "\U0001F534"
        await send_telegram_message(
            f"\U0001F4A7 <b>Liquidity swept</b> ({item['direction']}) on <b>{name}</b>\n"
            f"level {item['level']:.4f}"
        )

    # cap memory growth - only keep the most recent events per symbol
    if len(seen_bc) > 500:
        _seen_bos_choch[symbol] = set(sorted(seen_bc, key=lambda k: k[0])[-300:])
    if len(seen_lq) > 500:
        _seen_liquidity_swept[symbol] = set(sorted(seen_lq)[-300:])

    _warmed_up.add(symbol)


async def run_alert_watcher():
    if not ALERTS_ENABLED:
        log.info("Telegram alerts disabled (no TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID set)")
        return

    log.info("Telegram alert watcher starting")
    await send_telegram_message("SMC dashboard alert watcher started.")

    while True:
        try:
            symbols = await deriv_client.fetch_synthetic_indices()
        except Exception:
            log.exception("alert watcher: failed to fetch symbol list, retrying next cycle")
            await asyncio.sleep(ALERT_POLL_INTERVAL)
            continue

        cycle_start = time.monotonic()
        for s in symbols:
            await _check_symbol(s["symbol"], s.get("display_name", s["symbol"]))
            await asyncio.sleep(ALERT_STAGGER)

        elapsed = time.monotonic() - cycle_start
        remaining = max(0.0, ALERT_POLL_INTERVAL - elapsed)
        await asyncio.sleep(remaining)
