import asyncio
import json
import logging
import os
import time
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import deriv_client
import smc_engine
import telegram_alerts

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("smc-dashboard")

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.on_event("startup")
async def _start_alert_watcher():
    asyncio.create_task(telegram_alerts.run_alert_watcher())
    asyncio.create_task(telegram_alerts.run_command_listener())

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

GRANULARITIES = {
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "4h": 14400, "1d": 86400,
}

HISTORY_COUNT = int(os.getenv("HISTORY_COUNT", "1000"))
RECOMPUTE_MIN_INTERVAL = 1.0  # seconds, avoid hammering CPU if candles burst in


@app.get("/api/symbols")
async def get_symbols():
    try:
        return await deriv_client.fetch_synthetic_indices()
    except Exception as exc:
        log.exception("failed to fetch symbols")
        return {"error": str(exc)}


@app.get("/api/timeframes")
async def get_timeframes():
    return list(GRANULARITIES.keys())


@app.websocket("/ws/chart")
async def ws_chart(ws: WebSocket):
    await ws.accept()
    stream_task: asyncio.Task | None = None
    try:
        while True:
            init = await ws.receive_json()
            symbol = init.get("symbol")
            timeframe = init.get("timeframe", "5m")
            swing_length = int(init.get("swing_length", 10))
            range_percent = float(init.get("range_percent", 0.01))
            around_time = init.get("around_time")  # optional: jump to a specific historical point
            granularity = GRANULARITIES.get(timeframe)

            if not symbol or not granularity:
                await ws.send_json({"type": "error", "message": "symbol and a valid timeframe are required"})
                continue

            if stream_task:
                stream_task.cancel()

            stream_task = asyncio.create_task(
                _run_symbol_stream(ws, symbol, granularity, swing_length, range_percent, around_time)
            )
    except WebSocketDisconnect:
        pass
    finally:
        if stream_task:
            stream_task.cancel()


async def _run_symbol_stream(ws: WebSocket, symbol: str, granularity: int, swing_length: int,
                              range_percent: float, around_time: int | None = None):
    try:
        # An anchor inside what the "most recent HISTORY_COUNT candles" window already
        # covers doesn't need special handling - the frontend can find and center on it
        # itself. Only a genuinely old anchor (e.g. right-click on a candle from weeks
        # back on a higher timeframe, then drop to a lower one) needs a real historical
        # fetch, and that fetch must NOT then have live ticks streamed into it: the
        # streaming loop below only knows how to extend a dataframe that already ends
        # at "now" - blindly attaching live ticks to an old, disjoint window would just
        # corrupt it with a time gap in the middle rather than anything useful.
        now = time.time()
        live_edge_window = (HISTORY_COUNT // 2) * granularity
        is_historical = around_time is not None and (now - around_time) > live_edge_window
        end_time = min(int(around_time) + live_edge_window, int(now)) if is_historical else None

        candles = await deriv_client.fetch_candle_history(symbol, granularity, HISTORY_COUNT, end_time=end_time)
        df = smc_engine.build_dataframe(candles)
        overlays = await asyncio.to_thread(
            smc_engine.compute_all, df, swing_length=swing_length, range_percent=range_percent
        )

        await ws.send_json({
            "type": "history",
            "symbol": symbol,
            "candles": candles,
            "overlays": overlays,
            "live": not is_historical,
        })

        if is_historical:
            return  # frozen snapshot - frontend shows a "Live" button to resume streaming

        last_sent = 0.0
        async for candle in deriv_client.stream_candles(symbol, granularity):
            row = {"time": candle["epoch"], "open": candle["open"], "high": candle["high"],
                   "low": candle["low"], "close": candle["close"],
                   "volume": abs(candle["high"] - candle["low"]) + abs(candle["close"] - candle["open"])}
            if len(df) and df["time"].iloc[-1] == row["time"]:
                df.iloc[-1, df.columns.get_indexer(["open", "high", "low", "close", "volume"])] = \
                    [row["open"], row["high"], row["low"], row["close"], row["volume"]]
            else:
                df = pd.concat([df, pd.DataFrame([row], index=[pd.to_datetime(row["time"], unit="s")])])
                df = df.tail(HISTORY_COUNT)

            await ws.send_json({"type": "candle", "candle": {
                "time": row["time"], "open": row["open"], "high": row["high"],
                "low": row["low"], "close": row["close"],
            }})

            loop_time = asyncio.get_event_loop().time()
            if loop_time - last_sent >= RECOMPUTE_MIN_INTERVAL:
                overlays = await asyncio.to_thread(
                    smc_engine.compute_all, df, swing_length=swing_length, range_percent=range_percent
                )
                await ws.send_json({"type": "overlays", "overlays": overlays})
                last_sent = loop_time
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.exception("stream failed for %s", symbol)
        try:
            await ws.send_json({"type": "error", "message": str(exc)})
        except Exception:
            pass


app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
