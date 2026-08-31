import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

import deriv_client
import smc_engine

log = logging.getLogger("smc-dashboard.alerts")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ALERTS_ENABLED = bool(TELEGRAM_BOT_TOKEN)

ALERT_GRANULARITY = int(os.getenv("ALERT_GRANULARITY", "300"))  # 5m default
ALERT_SWING_LENGTH = int(os.getenv("ALERT_SWING_LENGTH", "10"))
ALERT_RANGE_PERCENT = float(os.getenv("ALERT_RANGE_PERCENT", "0.01"))
ALERT_POLL_INTERVAL = int(os.getenv("ALERT_POLL_INTERVAL", "300"))  # seconds between full symbol sweeps
ALERT_HISTORY_COUNT = int(os.getenv("ALERT_HISTORY_COUNT", "300"))
ALERT_STAGGER = 2.0  # seconds between checking each symbol, spreads load over the poll window

# comma-separated Deriv symbol codes to restrict alerts to, e.g. "R_100,R_75,BOOM1000"
# leave unset/empty to alert on every synthetic index (noisy - 30-50+ symbols)
_raw_symbol_filter = os.getenv("ALERT_SYMBOLS", "").strip()
ALERT_SYMBOL_FILTER = set(s.strip() for s in _raw_symbol_filter.split(",") if s.strip()) or None

DASHBOARD_URL = os.getenv("DASHBOARD_URL", "https://web-production-e22932.up.railway.app").rstrip("/")

_GRANULARITY_TF_LABELS = {60: "1m", 300: "5m", 900: "15m", 1800: "30m", 3600: "1h", 14400: "4h", 86400: "1d"}
ALERT_TF_LABEL = _GRANULARITY_TF_LABELS.get(ALERT_GRANULARITY, f"{ALERT_GRANULARITY}s")


def _format_time(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _chart_link(symbol: str) -> str:
    return f"{DASHBOARD_URL}/?symbol={symbol}&timeframe={ALERT_TF_LABEL}"

SUBSCRIBERS_FILE = Path(__file__).resolve().parent / "subscribers.json"
ALERT_STATE_FILE = Path(__file__).resolve().parent / "alert_state.json"

# per-symbol memory of event times already alerted on, so nothing repeats
_seen_bos_choch: dict[str, set[tuple[int, str]]] = {}
_seen_liquidity_swept: dict[str, set[int]] = {}
_warmed_up: set[str] = set()

_subscribers: set[str] = set()

HELP_TEXT = (
    "SMC Dashboard alerts.\n\n"
    "/start - subscribe to BOS, CHoCH, and liquidity sweep alerts across all Deriv synthetic indices\n"
    "/stop - unsubscribe"
)


def _load_subscribers():
    global _subscribers
    if SUBSCRIBERS_FILE.exists():
        try:
            _subscribers = set(str(c) for c in json.loads(SUBSCRIBERS_FILE.read_text()))
        except Exception:
            log.exception("failed to load subscribers file, starting empty")
            _subscribers = set()
    else:
        _subscribers = set()

    # legacy single-chat env var still works, folds straight into the subscriber list
    legacy = os.getenv("TELEGRAM_CHAT_ID", "")
    if legacy:
        _subscribers.add(str(legacy))


def _save_subscribers():
    try:
        SUBSCRIBERS_FILE.write_text(json.dumps(sorted(_subscribers)))
    except Exception:
        log.exception("failed to save subscribers file")


def _load_alert_state():
    """
    Restores the "already alerted on this" memory from disk. Without this,
    every restart forgets everything and re-alerts old events as if they
    were new - which is exactly what was happening before this existed.
    """
    global _seen_bos_choch, _seen_liquidity_swept, _warmed_up
    if not ALERT_STATE_FILE.exists():
        return
    try:
        data = json.loads(ALERT_STATE_FILE.read_text())
        _seen_bos_choch = {
            sym: set((t, k) for t, k in pairs)
            for sym, pairs in data.get("bos_choch", {}).items()
        }
        _seen_liquidity_swept = {
            sym: set(times) for sym, times in data.get("liquidity", {}).items()
        }
        _warmed_up = set(data.get("warmed_up", []))
        log.info("restored alert state for %d symbol(s)", len(_warmed_up))
    except Exception:
        log.exception("failed to load alert state, starting fresh")


def _save_alert_state():
    try:
        data = {
            "bos_choch": {sym: sorted(list(s)) for sym, s in _seen_bos_choch.items()},
            "liquidity": {sym: sorted(list(s)) for sym, s in _seen_liquidity_swept.items()},
            "warmed_up": sorted(_warmed_up),
        }
        ALERT_STATE_FILE.write_text(json.dumps(data))
    except Exception:
        log.exception("failed to save alert state")


async def _send_to_chat(chat_id: str, text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            })
            if resp.status_code != 200:
                log.warning("telegram send failed for %s: %s %s", chat_id, resp.status_code, resp.text)
    except Exception:
        log.exception("telegram send raised for %s", chat_id)


async def send_telegram_message(text: str):
    """Broadcasts to every subscribed chat."""
    if not ALERTS_ENABLED or not _subscribers:
        return
    for chat_id in list(_subscribers):
        await _send_to_chat(chat_id, text)


async def _check_symbol(symbol: str, display_name: str) -> list[str]:
    """Returns a list of formatted event lines for anything new - caller batches these into one digest."""
    try:
        candles = await deriv_client.fetch_candle_history(symbol, ALERT_GRANULARITY, ALERT_HISTORY_COUNT)
        if len(candles) < 20:
            return []
        df = smc_engine.build_dataframe(candles)
        overlays = await asyncio.to_thread(
            smc_engine.compute_all, df, swing_length=ALERT_SWING_LENGTH, range_percent=ALERT_RANGE_PERCENT
        )
    except Exception:
        log.exception("alert check failed for %s", symbol)
        return []

    first_pass = symbol not in _warmed_up
    seen_bc = _seen_bos_choch.setdefault(symbol, set())
    seen_lq = _seen_liquidity_swept.setdefault(symbol, set())
    name = display_name or symbol
    lines: list[str] = []

    for item in overlays["bos_choch"]:
        key = (item["time"], item["kind"])
        if key in seen_bc:
            continue
        seen_bc.add(key)
        if first_pass:
            continue
        arrow = "\U0001F7E2" if item["direction"] == "bullish" else "\U0001F534"
        ts = _format_time(item["time"])
        link = _chart_link(symbol)
        lines.append(
            f"{arrow} <b>{item['kind']}</b> ({item['direction']}) <b>{name}</b> @ {item['level']:.4f}\n"
            f"   {ts} \u00b7 <a href=\"{link}\">open {ALERT_TF_LABEL} chart</a>"
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
        ts = _format_time(item["swept_time"])
        link = _chart_link(symbol)
        lines.append(
            f"\U0001F4A7 Liquidity swept ({item['direction']}) <b>{name}</b> @ {item['level']:.4f}\n"
            f"   {ts} \u00b7 <a href=\"{link}\">open {ALERT_TF_LABEL} chart</a>"
        )

    # cap memory growth - only keep the most recent events per symbol
    if len(seen_bc) > 500:
        _seen_bos_choch[symbol] = set(sorted(seen_bc, key=lambda k: k[0])[-300:])
    if len(seen_lq) > 500:
        _seen_liquidity_swept[symbol] = set(sorted(seen_lq)[-300:])

    _warmed_up.add(symbol)
    _save_alert_state()
    return lines


async def run_alert_watcher():
    """Background loop: sweeps synthetic symbols on a timer, sends one digest message per sweep."""
    if not ALERTS_ENABLED:
        log.info("Telegram alerts disabled (no TELEGRAM_BOT_TOKEN set)")
        return

    _load_subscribers()
    _load_alert_state()
    log.info(
        "alert watcher starting, %d subscriber(s), symbol filter: %s",
        len(_subscribers), ALERT_SYMBOL_FILTER or "all synthetic indices",
    )

    while True:
        try:
            symbols = await deriv_client.fetch_synthetic_indices()
        except Exception:
            log.exception("alert watcher: failed to fetch symbol list, retrying next cycle")
            await asyncio.sleep(ALERT_POLL_INTERVAL)
            continue

        if ALERT_SYMBOL_FILTER:
            symbols = [s for s in symbols if s["symbol"] in ALERT_SYMBOL_FILTER]

        cycle_start = time.monotonic()
        digest_lines: list[str] = []
        for s in symbols:
            lines = await _check_symbol(s["symbol"], s.get("display_name", s["symbol"]))
            digest_lines.extend(lines)
            await asyncio.sleep(ALERT_STAGGER)

        if digest_lines:
            header = f"<b>SMC alerts</b> ({len(digest_lines)} new)\n\n"
            await send_telegram_message(header + "\n".join(digest_lines))

        elapsed = time.monotonic() - cycle_start
        remaining = max(0.0, ALERT_POLL_INTERVAL - elapsed)
        await asyncio.sleep(remaining)


async def run_command_listener():
    """Background loop: long-polls Telegram for /start and /stop messages to manage subscriptions."""
    if not ALERTS_ENABLED:
        return

    offset = None
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"

    while True:
        try:
            params = {"timeout": 25}
            if offset is not None:
                params["offset"] = offset
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(url, params=params)
                data = resp.json()
        except Exception:
            log.exception("command listener: getUpdates failed, retrying in 5s")
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
                    log.info("new subscriber (total %d)", len(_subscribers))
                await _send_to_chat(chat_id,
                    "Subscribed. You'll get alerts for new BOS, CHoCH, and liquidity "
                    "sweeps across all synthetic indices.\n\nSend /stop any time to unsubscribe.")
            elif text.startswith("/stop"):
                if chat_id in _subscribers:
                    _subscribers.discard(chat_id)
                    _save_subscribers()
                    log.info("unsubscribed (total %d)", len(_subscribers))
                await _send_to_chat(chat_id, "Unsubscribed. Send /start any time to resume.")
            else:
                await _send_to_chat(chat_id, HELP_TEXT)
