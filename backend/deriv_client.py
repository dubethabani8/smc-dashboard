"""
Wraps Deriv's websocket API - just market data, no auth needed beyond the
app_id header. Old ws.derivws.com/websockets/v3 endpoint is dead, this is
the new one: wss://api.derivws.com/trading/v1/options/ws/public
"""
import asyncio
import itertools
import json
import os

import websockets

DERIV_APP_ID = os.getenv("DERIV_APP_ID", "")  # your app_id string from developers.deriv.com
DERIV_WS_URL = "wss://api.derivws.com/trading/v1/options/ws/public"

_req_id_counter = itertools.count(1)


def _connect():
    headers = {"Deriv-App-ID": DERIV_APP_ID} if DERIV_APP_ID else {}
    # websockets.connect() returns a Connect object that is itself usable as
    # an async context manager - do NOT await it here, just return it as-is
    # and let callers do `async with _connect() as ws:`.
    # (websockets 12.x names the header kwarg `extra_headers`; renamed to
    # `additional_headers` in 13+, but requirements.txt pins <13.)
    return websockets.connect(
        DERIV_WS_URL, extra_headers=headers, ping_interval=20, ping_timeout=10
    )


async def _with_retry(coro_fn, *args, retries: int = 3, base_delay: float = 1.0):
    """
    Run a one-shot async call, retrying on transient network errors
    (DNS blips, connection timeouts, dropped sockets) with short backoff.
    Does NOT retry on Deriv-returned application errors (e.g. "market
    closed", bad symbol) - those are real, not transient, so they raise
    immediately.
    """
    last_exc = None
    for attempt in range(retries):
        try:
            return await coro_fn(*args)
        except (OSError, asyncio.TimeoutError, websockets.InvalidHandshake,
                websockets.ConnectionClosed) as exc:
            last_exc = exc
            if attempt < retries - 1:
                await asyncio.sleep(base_delay * (attempt + 1))
    raise last_exc


async def fetch_synthetic_indices() -> list[dict]:
    """Return the list of Deriv synthetic/volatility index symbols. Retries on transient network errors."""
    return await _with_retry(_fetch_synthetic_indices)


async def _fetch_synthetic_indices() -> list[dict]:
    async with _connect() as ws:
        await ws.send(json.dumps({"active_symbols": "brief"}))
        raw = await ws.recv()
        data = json.loads(raw)
        if data.get("error"):
            raise RuntimeError(data["error"]["message"])
        if data.get("errors"):  # new API's REST-style error shape can also show up here
            raise RuntimeError("; ".join(e.get("message", str(e)) for e in data["errors"]))

        symbols = data.get("active_symbols", [])
        synth = [
            {
                "symbol": s["underlying_symbol"],
                "display_name": s.get("underlying_symbol_name", s["underlying_symbol"]),
                "market": s.get("market"),
                "submarket": (s.get("submarket") or "").replace("_", " ").title(),
            }
            for s in symbols
            if s.get("market") == "synthetic_index"
        ]
        synth.sort(key=lambda s: (s["submarket"], s["display_name"]))
        return synth


async def fetch_candle_history(symbol: str, granularity: int, count: int = 1000) -> list[dict]:
    """One-shot fetch of the most recent `count` candles. Retries on transient network errors."""
    return await _with_retry(_fetch_candle_history, symbol, granularity, count)


async def _fetch_candle_history(symbol: str, granularity: int, count: int) -> list[dict]:
    async with _connect() as ws:
        req = {
            "ticks_history": symbol,
            "adjust_start_time": 1,
            "count": count,
            "end": "latest",
            "start": 1,
            "style": "candles",
            "granularity": granularity,
            "req_id": next(_req_id_counter),
        }
        await ws.send(json.dumps(req))
        raw = await ws.recv()
        data = json.loads(raw)
        if data.get("error"):
            raise RuntimeError(data["error"]["message"])
        if data.get("errors"):
            raise RuntimeError("; ".join(e.get("message", str(e)) for e in data["errors"]))
        return data.get("candles", [])

async def fetch_candle_history_paginated(symbol: str, granularity: int, total_count: int) -> list[dict]:
    """
    Fetches more candles than a single Deriv request supports, by paging
    backward in time and stitching the results together.

    Deriv's ticks_history endpoint caps a single response at ~1000 candles
    regardless of the requested `count` (confirmed empirically - asking for
    5000 still returns 1000). This repeatedly asks for the next chunk
    ending just before the earliest candle already fetched, until either
    `total_count` is reached or Deriv runs out of history for this symbol.

    Only used by the backtest script - the live app's fetch_candle_history
    is untouched and still single-request (fine there, since it only needs
    a rolling recent window, not deep history).
    """
    all_candles: dict[int, dict] = {}
    end: int | str = "latest"
    per_request = 1000

    while len(all_candles) < total_count:
        batch = await _with_retry(_fetch_candle_history_before, symbol, granularity, per_request, end)
        if not batch:
            break

        before = len(all_candles)
        for c in batch:
            all_candles[c["epoch"]] = c
        gained = len(all_candles) - before

        earliest = min(c["epoch"] for c in batch)
        next_end = earliest - granularity
        if gained == 0 or (end != "latest" and next_end >= end):
            # no new candles came back - hit the start of available history
            # for this symbol, stop instead of looping forever
            break
        end = next_end

        if len(batch) < per_request:
            # short page - also a signal we've reached the start of history
            break

        await asyncio.sleep(0.3)  # be polite to Deriv's API between pagination calls

    result = sorted(all_candles.values(), key=lambda c: c["epoch"])
    return result[-total_count:] if len(result) > total_count else result


async def _fetch_candle_history_before(symbol: str, granularity: int, count: int, end) -> list[dict]:
    async with _connect() as ws:
        req = {
            "ticks_history": symbol,
            "adjust_start_time": 1,
            "count": count,
            "end": end,
            "start": 1,
            "style": "candles",
            "granularity": granularity,
            "req_id": next(_req_id_counter),
        }
        await ws.send(json.dumps(req))
        raw = await ws.recv()
        data = json.loads(raw)
        if data.get("error"):
            raise RuntimeError(data["error"]["message"])
        if data.get("errors"):
            raise RuntimeError("; ".join(e.get("message", str(e)) for e in data["errors"]))
        return data.get("candles", [])

async def stream_candles(symbol: str, granularity: int):
    """
    Async generator yielding a candle dict every time Deriv pushes an OHLC update
    for the subscribed symbol/granularity. Reconnects on drop.
    """
    while True:
        try:
            async with _connect() as ws:
                req = {
                    "ticks_history": symbol,
                    "adjust_start_time": 1,
                    "count": 1,
                    "end": "latest",
                    "start": 1,
                    "style": "candles",
                    "granularity": granularity,
                    "subscribe": 1,
                    "req_id": next(_req_id_counter),
                }
                await ws.send(json.dumps(req))
                async for raw in ws:
                    data = json.loads(raw)
                    if data.get("error"):
                        raise RuntimeError(data["error"]["message"])
                    msg_type = data.get("msg_type")
                    if msg_type == "ohlc":
                        ohlc = data["ohlc"]
                        yield {
                            "epoch": int(ohlc["open_time"]),
                            "open": float(ohlc["open"]),
                            "high": float(ohlc["high"]),
                            "low": float(ohlc["low"]),
                            "close": float(ohlc["close"]),
                        }
                    # first response to a subscribe call is msg_type "candles" (initial burst) - ignore, we already have history
        except (websockets.ConnectionClosed, OSError) as exc:
            await asyncio.sleep(2)  # brief backoff, then reconnect and resubscribe
            continue
