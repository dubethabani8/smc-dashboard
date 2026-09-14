"""
Live paper-trading detector for the LQ-low-reclaim-after-bullish-BOS setup,
now with outcome tracking: once a signal fires, this keeps watching that
specific trade across future polling cycles and sends follow-up messages
when the partial target hits, and again when the trade finally closes
(breakeven stop, full stop, or time exit) - reporting the real outcome, not
just the entry.

Uses the EXACT validated parameters, imported directly from
trade_sim_lq_reclaim.py so this can never quietly drift from what was
actually backtested:
  CLOSENESS_ATR_MULT, STOP_ATR_MULT, PARTIAL_TARGET_ATR_MULT,
  PARTIAL_EXIT_FRACTION, BREAKEVEN_BUFFER_ATR_MULT, MAX_HOLD_CANDLES,
  COST_ATR_MULT_PER_LEG, MIN_CANDLES_LQ_TO_BOS

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
   BOS/CHoCH/liquidity alert engine.

Open trades are tracked using epoch timestamps, not positional dataframe
indices - each polling cycle re-fetches a rolling window, so timestamps are
the only thing guaranteed stable across cycles.

This is a SEPARATE bot/subscriber list from production alerts
(telegram_alerts.py) - different token, different state files.

This sends NOTIFICATIONS ONLY - no real or demo orders are placed.
"""
import asyncio
import io
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.patches import Rectangle

import deriv_client
import smc_engine
from backtest_lq_reclaim import compute_atr, find_candidate_setups, SWING_LENGTH, RANGE_PERCENT
from trade_sim_lq_reclaim import (
    CLOSENESS_ATR_MULT, STOP_ATR_MULT, PARTIAL_TARGET_ATR_MULT,
    PARTIAL_EXIT_FRACTION, BREAKEVEN_BUFFER_ATR_MULT, MAX_HOLD_CANDLES,
    COST_ATR_MULT_PER_LEG,
)

log = logging.getLogger("smc-dashboard.lq-reclaim")

LQ_RECLAIM_BOT_TOKEN = os.getenv("LQ_RECLAIM_BOT_TOKEN", "")
LQ_RECLAIM_ENABLED = bool(LQ_RECLAIM_BOT_TOKEN)

LQ_RECLAIM_GRANULARITY = 900  # 15m, matches everything the setup was validated on
LQ_RECLAIM_HISTORY_COUNT = int(os.getenv("LQ_RECLAIM_HISTORY_COUNT", "2000"))
LQ_RECLAIM_POLL_INTERVAL = int(os.getenv("LQ_RECLAIM_POLL_INTERVAL", "300"))
LQ_RECLAIM_STAGGER = 2.0

CHECK_LAST_N_CANDLES = 3  # buffer for slightly-late poll cycles
MIN_CANDLES_LQ_TO_BOS = SWING_LENGTH
MAX_HOLD_SECONDS = MAX_HOLD_CANDLES * LQ_RECLAIM_GRANULARITY
CANDLES_BEFORE_LQ = 15  # context shown before the LQ level in each chart snapshot

_default_symbols = "R_100,R_75,R_50,R_25,CRASH500,BOOM500"
_raw_symbol_filter = os.getenv("LQ_RECLAIM_SYMBOLS", _default_symbols).strip()
LQ_RECLAIM_SYMBOLS = [s.strip() for s in _raw_symbol_filter.split(",") if s.strip()]

DASHBOARD_URL = os.getenv("DASHBOARD_URL", "https://web-production-e22932.up.railway.app").rstrip("/")

SUBSCRIBERS_FILE = Path(__file__).resolve().parent / "lq_reclaim_subscribers.json"
STATE_FILE = Path(__file__).resolve().parent / "lq_reclaim_state.json"

_used_lq_times: dict[str, set[int]] = {}
_seen_signals: dict[str, set[tuple[int, int, int]]] = {}
_open_trades: dict[str, dict[str, dict]] = {}  # symbol -> {entry_time_str: trade_dict}
_warmed_up: set[str] = set()

_subscribers: set[str] = set()

HELP_TEXT = (
    "SMC LQ-Reclaim paper-trading alerts (EXPERIMENTAL).\n\n"
    "Each signal is followed up automatically: a message when the partial "
    "target hits, and a final message with the real outcome when the trade "
    "closes. One trade per liquidity level, ever - a level that's already "
    "fired never fires again.\n\n"
    "/start - subscribe\n/stop - unsubscribe"
)


def _format_time(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _chart_link(symbol: str, level: float, event_time: int) -> str:
    return (
        f"{DASHBOARD_URL}/?symbol={symbol}&timeframe=15m"
        f"&event=LIQUIDITY&dir=bullish&level={level}&time={event_time}"
    )


def render_trade_chart(df: pd.DataFrame, trade: dict, title: str) -> bytes:
    """
    Renders a candlestick snapshot with LQ/BOS/stop/target/breakeven lines
    and entry/partial/exit markers - pure matplotlib, no extra dependency.
    Window is trade['lq_time'] - CANDLES_BEFORE_LQ candles through the
    latest available candle in df.
    """
    start_time = trade["lq_time"] - CANDLES_BEFORE_LQ * LQ_RECLAIM_GRANULARITY
    window = df[df["time"] >= start_time]
    candles = window.to_dict("records")
    if len(candles) < 2:
        candles = df.tail(30).to_dict("records")

    times = [datetime.fromtimestamp(c["time"], tz=timezone.utc) for c in candles]
    x = mdates.date2num(times)
    width = (x[1] - x[0]) * 0.6 if len(x) > 1 else 0.01

    fig, ax = plt.subplots(figsize=(10, 5.5), dpi=130)
    fig.patch.set_facecolor("#0d1117")
    ax.set_facecolor("#0d1117")

    for xi, c in zip(x, candles):
        color = "#26a69a" if c["close"] >= c["open"] else "#ef5350"
        ax.plot([xi, xi], [c["low"], c["high"]], color=color, linewidth=0.8, zorder=2)
        lower = min(c["open"], c["close"])
        height = abs(c["close"] - c["open"]) or (c["high"] - c["low"]) * 0.01
        ax.add_patch(Rectangle((xi - width / 2, lower), width, height,
                                facecolor=color, edgecolor=color, zorder=3))

    def hline(price, color, label):
        if price is None:
            return
        ax.axhline(price, color=color, linestyle="--", linewidth=1, alpha=0.85, zorder=1)
        ax.text(x[-1], price, f" {label}", color=color, fontsize=8,
                 va="center", ha="left", fontweight="bold")

    hline(trade.get("lq_level"), "#4caf50", "LQ")
    hline(trade.get("bos_level"), "#2196f3", "BOS")
    hline(trade.get("original_stop_price") or trade.get("current_stop_price"), "#f44336", "Stop")
    hline(trade.get("target_price"), "#00e676", "Target")
    hline(trade.get("breakeven_price"), "#ff9800", "BE")

    def marker(epoch, price, symbol, color, label):
        if epoch is None or price is None:
            return
        mx = mdates.date2num(datetime.fromtimestamp(epoch, tz=timezone.utc))
        ax.scatter([mx], [price], marker=symbol, s=140, color=color, zorder=5,
                    edgecolors="white", linewidths=0.6)
        ax.annotate(label, (mx, price), textcoords="offset points", xytext=(0, 14),
                     color=color, fontsize=8, ha="center", fontweight="bold")

    marker(trade.get("entry_time"), trade.get("entry_price"), "^", "#ffeb3b", "Entry")
    marker(trade.get("partial_time"), trade.get("partial_price"), "o", "#00e676", "Partial")
    marker(trade.get("exit_time"), trade.get("exit_price"), "v", "#f44336", "Exit")

    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    fig.autofmt_xdate(rotation=25)
    ax.tick_params(colors="#c9d1d9", labelsize=8)
    for spine in ax.spines.values():
        spine.set_color("#30363d")
    ax.grid(color="#21262d", linewidth=0.5)
    ax.set_title(title, color="#c9d1d9", fontsize=11, loc="left")
    ax.margins(x=0.02)

    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    return buf.getvalue()


async def _send_photo_to_chat(chat_id: str, photo_bytes: bytes, caption: str):
    url = f"https://api.telegram.org/bot{LQ_RECLAIM_BOT_TOKEN}/sendPhoto"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                url,
                data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
                files={"photo": ("chart.png", photo_bytes, "image/png")},
            )
            if resp.status_code != 200:
                log.warning("lq-reclaim photo send failed for %s: %s %s",
                            chat_id, resp.status_code, resp.text)
    except Exception:
        log.exception("lq-reclaim photo send raised for %s", chat_id)


async def send_lq_reclaim_photo(photo_bytes: bytes, caption: str):
    if not LQ_RECLAIM_ENABLED or not _subscribers:
        return
    for chat_id in list(_subscribers):
        await _send_photo_to_chat(chat_id, photo_bytes, caption)


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
    global _used_lq_times, _seen_signals, _open_trades, _warmed_up
    if not STATE_FILE.exists():
        return
    try:
        data = json.loads(STATE_FILE.read_text())
        _used_lq_times = {sym: set(times) for sym, times in data.get("used_lq_times", {}).items()}
        _seen_signals = {
            sym: set(tuple(t) for t in sigs)
            for sym, sigs in data.get("seen_signals", {}).items()
        }
        _open_trades = data.get("open_trades", {})
        _warmed_up = set(data.get("warmed_up", []))
        n_open = sum(len(v) for v in _open_trades.values())
        log.info("restored lq-reclaim state: %d symbol(s) warmed, %d level(s) used, %d trade(s) open",
                  len(_warmed_up), sum(len(v) for v in _used_lq_times.values()), n_open)
    except Exception:
        log.exception("failed to load lq-reclaim state, starting fresh")


def _save_state():
    try:
        data = {
            "used_lq_times": {sym: sorted(list(s)) for sym, s in _used_lq_times.items()},
            "seen_signals": {sym: [list(t) for t in s] for sym, s in _seen_signals.items()},
            "open_trades": _open_trades,
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


# ---------------------------------------------------------------------------
# Open trade tracking - follow-up messages as each trade actually plays out
# ---------------------------------------------------------------------------

def _leg_return(exit_price: float, entry_price: float, atr_at_entry: float) -> float:
    cost_frac = (COST_ATR_MULT_PER_LEG * atr_at_entry) / entry_price
    return (exit_price - entry_price) / entry_price - cost_frac


async def _process_open_trades(symbol: str, df: pd.DataFrame):
    trades = _open_trades.get(symbol, {})
    if not trades:
        return

    closed_keys = []
    for key, trade in list(trades.items()):
        new_rows = df[df["time"] > trade["last_checked_time"]]
        if new_rows.empty:
            continue

        for _, row in new_rows.iterrows():
            candle_time = int(row["time"])
            low, high, close = row["low"], row["high"], row["close"]
            entry_price = trade["entry_price"]
            atr_at_entry = trade["atr_at_entry"]

            # conservative: stop checked before target within the same candle
            if low <= trade["current_stop_price"]:
                exit_price = trade["current_stop_price"]
                if not trade["partial_taken"]:
                    outcome = "stopped_full_loss"
                    total_return = _leg_return(exit_price, entry_price, atr_at_entry)
                else:
                    outcome = "partial_then_breakeven_stop"
                    partial_leg = PARTIAL_EXIT_FRACTION * _leg_return(
                        trade["partial_price"], entry_price, atr_at_entry)
                    remainder_leg = (1 - PARTIAL_EXIT_FRACTION) * _leg_return(
                        exit_price, entry_price, atr_at_entry)
                    total_return = partial_leg + remainder_leg
                await _send_close_message(symbol, trade, outcome, total_return, exit_price, candle_time, df)
                closed_keys.append(key)
                break

            if not trade["partial_taken"] and high >= trade["target_price"]:
                trade["partial_taken"] = True
                trade["partial_time"] = candle_time
                trade["partial_price"] = trade["target_price"]
                trade["current_stop_price"] = trade["breakeven_price"]
                await _send_partial_message(symbol, trade, df)

            if candle_time >= trade["max_hold_cutoff_time"]:
                exit_price = close
                if trade["partial_taken"]:
                    outcome = "partial_then_time_exit"
                    partial_leg = PARTIAL_EXIT_FRACTION * _leg_return(
                        trade["partial_price"], entry_price, atr_at_entry)
                    remainder_leg = (1 - PARTIAL_EXIT_FRACTION) * _leg_return(
                        exit_price, entry_price, atr_at_entry)
                    total_return = partial_leg + remainder_leg
                else:
                    outcome = "time_exit_full"
                    total_return = _leg_return(exit_price, entry_price, atr_at_entry)
                await _send_close_message(symbol, trade, outcome, total_return, exit_price, candle_time, df)
                closed_keys.append(key)
                break

            trade["last_checked_time"] = candle_time

        if key in trades and key not in closed_keys:
            trades[key] = trade  # persist incremental progress (partial/last_checked updates)

    for key in closed_keys:
        trades.pop(key, None)

    _open_trades[symbol] = trades
    _save_state()


async def _send_partial_message(symbol: str, trade: dict, df: pd.DataFrame):
    text = (
        f"\U0001F3AF <b>Partial target hit</b> - <b>{symbol}</b>\n"
        f"   Entry: {trade['entry_price']:.4f} \u2192 Partial: {trade['partial_price']:.4f}\n"
        f"   Stop moved to breakeven: {trade['breakeven_price']:.4f}\n"
        f"   Entered {_format_time(trade['entry_time'])}"
    )
    chart_bytes = render_trade_chart(df, trade, f"{symbol} - partial target hit")
    await send_lq_reclaim_photo(chart_bytes, text)
    log.info("lq-reclaim partial hit: %s entry=%.4f", symbol, trade["entry_price"])


async def _send_close_message(symbol: str, trade: dict, outcome: str, total_return: float,
                                exit_price: float, exit_time: int, df: pd.DataFrame):
    emoji = "\u2705" if total_return > 0 else "\u274C"
    outcome_labels = {
        "stopped_full_loss": "Full stop-loss",
        "partial_then_breakeven_stop": "Partial then breakeven stop",
        "partial_then_time_exit": "Partial then time exit (still running remainder)",
        "time_exit_full": "Time exit (no partial reached)",
    }
    label = outcome_labels.get(outcome, outcome)
    text = (
        f"{emoji} <b>Trade closed</b> - <b>{symbol}</b>\n"
        f"   Outcome: {label}\n"
        f"   Return: {total_return * 100:.3f}%\n"
        f"   Entry: {trade['entry_price']:.4f} \u2192 Exit: {exit_price:.4f}\n"
        f"   Entered {_format_time(trade['entry_time'])} \u2192 Closed {_format_time(exit_time)}"
    )
    trade_with_exit = {**trade, "exit_time": exit_time, "exit_price": exit_price}
    chart_bytes = render_trade_chart(df, trade_with_exit, f"{symbol} - {label} ({total_return * 100:.2f}%)")
    await send_lq_reclaim_photo(chart_bytes, text)
    log.info("lq-reclaim trade closed: %s outcome=%s return=%.4f%%", symbol, outcome, total_return * 100)


# ---------------------------------------------------------------------------
# New signal detection
# ---------------------------------------------------------------------------

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

    # first: advance any already-open trades using this fresh candle data
    await _process_open_trades(symbol, df)

    first_pass = symbol not in _warmed_up
    used_levels = _used_lq_times.setdefault(symbol, set())
    seen = _seen_signals.setdefault(symbol, set())
    open_trades = _open_trades.setdefault(symbol, {})

    time_to_idx = {t: i for i, t in enumerate(df["time"])}
    n = len(df)
    recent_cutoff_idx = max(0, n - CHECK_LAST_N_CANDLES)

    for c in candidates:
        ret_idx = c["return_idx"]
        if ret_idx < recent_cutoff_idx:
            continue

        sig_key = (c["lq_time"], c["bos_time"], c["return_time"])
        if sig_key in seen:
            continue
        seen.add(sig_key)

        if first_pass:
            continue

        if c["lq_time"] in used_levels:
            continue  # one-trade-per-level

        lq_idx = time_to_idx.get(c["lq_time"])
        bos_idx = time_to_idx.get(c["bos_time"])
        if lq_idx is None or bos_idx is None:
            continue
        if bos_idx - lq_idx < MIN_CANDLES_LQ_TO_BOS:
            continue

        atr_at_entry = atr_series.iloc[ret_idx]
        if pd.isna(atr_at_entry):
            continue

        closeness_abs = CLOSENESS_ATR_MULT * atr_at_entry
        if c["dist_abs"] > closeness_abs:
            continue

        entry_price = df["close"].iloc[ret_idx]
        stop_price = entry_price - STOP_ATR_MULT * atr_at_entry
        target_price = entry_price + PARTIAL_TARGET_ATR_MULT * atr_at_entry
        breakeven_price = entry_price + BREAKEVEN_BUFFER_ATR_MULT * atr_at_entry
        entry_time = c["return_time"]

        used_levels.add(c["lq_time"])

        open_trades[str(entry_time)] = {
            "entry_time": entry_time,
            "entry_price": float(entry_price),
            "lq_time": c["lq_time"],
            "lq_level": float(c["lq_level"]),
            "bos_time": c["bos_time"],
            "bos_level": float(c["bos_level"]),
            "original_stop_price": float(stop_price),
            "current_stop_price": float(stop_price),
            "target_price": float(target_price),
            "breakeven_price": float(breakeven_price),
            "atr_at_entry": float(atr_at_entry),
            "partial_taken": False,
            "partial_time": None,
            "partial_price": None,
            "last_checked_time": entry_time,
            "max_hold_cutoff_time": entry_time + MAX_HOLD_SECONDS,
        }
        _save_state()

        ts = _format_time(entry_time)
        link = _chart_link(symbol, c["lq_level"], c["lq_time"])
        text = (
            f"\U0001F7E2 <b>LQ-RECLAIM signal</b> (paper) - <b>{symbol}</b>\n"
            f"   Entry: {entry_price:.4f}\n"
            f"   Stop: {stop_price:.4f}  |  Target (partial): {target_price:.4f}\n"
            f"   Breakeven (after partial): {breakeven_price:.4f}\n"
            f"   {ts} \u00b7 <a href=\"{link}\">view LQ level on chart</a>"
        )
        chart_title = f"{symbol} - entry signal"
        chart_bytes = render_trade_chart(df, open_trades[str(entry_time)], chart_title)
        await send_lq_reclaim_photo(chart_bytes, text)
        log.info("lq-reclaim signal opened: %s entry=%.4f stop=%.4f target=%.4f",
                  symbol, entry_price, stop_price, target_price)

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

    cycle_count = 0
    while True:
        cycle_start = time.monotonic()
        error_count = 0
        for symbol in LQ_RECLAIM_SYMBOLS:
            try:
                await _check_symbol(symbol)
            except Exception:
                error_count += 1
                log.exception("lq-reclaim uncaught error checking %s", symbol)
            await asyncio.sleep(LQ_RECLAIM_STAGGER)

        cycle_count += 1
        n_open = sum(len(v) for v in _open_trades.values())
        n_used = sum(len(v) for v in _used_lq_times.values())
        log.info(
            "lq-reclaim heartbeat: cycle %d complete, %d symbol(s) checked, %d error(s), "
            "%d level(s) used to date, %d trade(s) currently open",
            cycle_count, len(LQ_RECLAIM_SYMBOLS), error_count, n_used, n_open,
        )

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
                    "You'll get a message on entry, on partial target hit, and again "
                    "when each trade closes. Send /stop any time to unsubscribe.")
            elif text.startswith("/stop"):
                if chat_id in _subscribers:
                    _subscribers.discard(chat_id)
                    _save_subscribers()
                await _send_to_chat(chat_id, "Unsubscribed. Send /start any time to resume.")
            else:
                await _send_to_chat(chat_id, HELP_TEXT)
