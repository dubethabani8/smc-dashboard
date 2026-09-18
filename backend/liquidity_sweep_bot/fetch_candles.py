"""
Pulls historical OHLC candles from Deriv's public ticks_history API and
saves them to CSV, in the format compare_c_definitions.py expects.

ticks_history requires no auth (just an app_id) -- see
https://developers.deriv.com -- but it's WebSocket-only (wss://), and
this sandbox's network can't reach derivws.com to run it from here (I
checked: the handshake gets a 403 from the egress proxy). Run this
somewhere with normal internet access instead -- your own machine, or
as a one-off on Railway, since that service already talks to Deriv.

Usage:
    pip install websockets
    python fetch_candles.py --symbol BOOM900 --granularity 900 --count 5000 --out boom900_15m.csv

Granularity is in seconds: 60=1m, 300=5m, 900=15m, 1800=30m, 3600=1h.

Symbol codes are Deriv's internal names, not the display names on your
chart. A few common ones -- double check against your own tool or
api.get_active_symbols() before relying on these, I haven't verified
them against a live connection:
    Boom 900 Index    -> BOOM900
    Boom 1000 Index   -> BOOM1000
    Crash 900 Index   -> CRASH900
    Crash 1000 Index  -> CRASH1000
    Step Index 400    -> stpRNG   (Step indices may use a different
                                   naming scheme -- worth confirming)

A single call maxes out around 5000 candles; for a longer history, page
backwards by passing --end as an earlier epoch and stitching the CSVs
together (not implemented here yet -- one file, one pull, to start).
"""
import argparse
import asyncio
import csv
import json

import websockets

APP_ID = 1089  # Deriv's public demo app_id, fine for unauthenticated market data


async def fetch(symbol, granularity, count, end):
    uri = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"
    async with websockets.connect(uri) as ws:
        request = {
            "ticks_history": symbol,
            "adjust_start_time": 1,
            "count": count,
            "end": end,
            "granularity": granularity,
            "style": "candles",
        }
        await ws.send(json.dumps(request))
        response = json.loads(await ws.recv())
        if "error" in response:
            raise RuntimeError(response["error"]["message"])
        return response["candles"]


async def list_symbols(filter_text=None):
    """Prints every active symbol Deriv currently exposes (code + display
    name), optionally narrowed to ones containing filter_text. Use this
    once to confirm the exact code for e.g. 'Boom 900 Index' rather than
    guessing -- I have not verified the symbol codes below against a live
    connection.

    Prints a progress line at each stage on purpose -- if this silently
    produces nothing, that's not supposed to be possible; the checkpoints
    are here to show exactly which stage it actually dies at."""
    uri = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"
    print(f"Connecting to {uri} ...", flush=True)
    try:
        async with websockets.connect(uri) as ws:
            print("Connected. Sending active_symbols request...", flush=True)
            await ws.send(json.dumps({"active_symbols": "brief"}))
            raw = await ws.recv()
            print(f"Got a response ({len(raw)} bytes). Parsing...", flush=True)
            response = json.loads(raw)
            if "error" in response:
                print(f"Deriv returned an error: {response['error']}")
                return
            symbols = response.get("active_symbols", [])
            print(f"Deriv returned {len(symbols)} active symbols total.", flush=True)
            matched = 0
            for s in symbols:
                if filter_text and filter_text.lower() not in s["display_name"].lower():
                    continue
                print(f"{s['symbol']:15s} {s['display_name']}")
                matched += 1
            label = f"matched filter '{filter_text}'" if filter_text else "total"
            print(f"({matched} {label})")
    except Exception as e:
        print(f"FAILED at some stage above: {type(e).__name__}: {e}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", help="Deriv symbol code, e.g. BOOM900")
    parser.add_argument("--granularity", type=int, default=900, help="Seconds per candle")
    parser.add_argument("--count", type=int, default=5000, help="Number of candles (~5000 max per call)")
    parser.add_argument("--end", default="latest", help="Epoch to end at, or 'latest'")
    parser.add_argument("--out", help="Output CSV path")
    parser.add_argument("--list-symbols", metavar="FILTER", nargs="?", const="",
                         help="Instead of fetching, list active symbol codes (optionally filtered, e.g. --list-symbols Boom)")
    args = parser.parse_args()

    if args.list_symbols is not None:
        asyncio.run(list_symbols(args.list_symbols or None))
        return

    if not args.symbol or not args.out:
        parser.error("--symbol and --out are required unless using --list-symbols")

    candles = asyncio.run(fetch(args.symbol, args.granularity, args.count, args.end))

    with open(args.out, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["epoch", "open", "high", "low", "close"])
        for c in candles:
            writer.writerow([c["epoch"], c["open"], c["high"], c["low"], c["close"]])

    print(f"Wrote {len(candles)} candles to {args.out}")


if __name__ == "__main__":
    main()
