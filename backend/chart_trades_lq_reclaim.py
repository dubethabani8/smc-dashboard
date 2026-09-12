"""
Generates standalone HTML chart snippets for a sample of trades from
trade_log_lq_reclaim.csv, so you can visually inspect what these setups
actually look like on the chart - LQ level, BOS, entry, stop, target, and
exit all marked.

Samples a handful of trades per outcome category (not all of them - that's
usually redundant once you've eyeballed a representative few) and per
symbol, then re-fetches just enough surrounding candle history for each one
to render a focused chart.

Run this AFTER trade_sim_lq_reclaim.py has produced trade_log_lq_reclaim.csv
in the same folder. Needs network access to Deriv (to re-fetch candles).
"""
import asyncio
import json
from pathlib import Path

import numpy as np
import pandas as pd

from backtest_lq_reclaim import fetch_df, TIMEFRAME_SECONDS

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

TRADE_LOG_PATH = Path(__file__).resolve().parent / "trade_log_lq_reclaim.csv"
OUTPUT_DIR = Path(__file__).resolve().parent / "trade_charts"

SAMPLES_PER_OUTCOME_PER_SYMBOL = 2  # keep small - this is for eyeballing, not exhaustive review
SYMBOLS_TO_CHART = None  # None = all symbols in the trade log; or set e.g. ["R_100", "CRASH500"]

CANDLES_BEFORE_LQ = 20   # context before the setup starts
CANDLES_AFTER_EXIT = 15  # context after the trade closes


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------

def sample_trades(trades: pd.DataFrame) -> pd.DataFrame:
    symbols = SYMBOLS_TO_CHART or sorted(trades["symbol"].unique())
    picked = []
    for symbol in symbols:
        sym_trades = trades[trades["symbol"] == symbol]
        for outcome, group in sym_trades.groupby("outcome"):
            picked.append(group.head(SAMPLES_PER_OUTCOME_PER_SYMBOL))
    if not picked:
        return pd.DataFrame()
    return pd.concat(picked, ignore_index=True)


# ---------------------------------------------------------------------------
# HTML chart generation
# ---------------------------------------------------------------------------

CHART_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>{title}</title>
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<style>
  body {{ background: #0d1117; color: #c9d1d9; font-family: -apple-system, Segoe UI, sans-serif; margin: 0; padding: 16px; }}
  h2 {{ font-size: 16px; margin: 0 0 4px 0; }}
  .meta {{ font-size: 13px; color: #8b949e; margin-bottom: 12px; }}
  .meta b {{ color: #c9d1d9; }}
  #chart {{ width: 100%; height: 480px; }}
  .legend {{ display: flex; gap: 16px; font-size: 12px; margin-top: 8px; flex-wrap: wrap; }}
  .legend span {{ display: inline-flex; align-items: center; gap: 4px; }}
  .swatch {{ width: 10px; height: 10px; display: inline-block; border-radius: 2px; }}
</style>
</head>
<body>
<h2>{title}</h2>
<div class="meta">
  <b>Outcome:</b> {outcome} &nbsp;|&nbsp;
  <b>Return:</b> {return_pct}% &nbsp;|&nbsp;
  <b>Entry:</b> {entry_price:.4f} &nbsp;|&nbsp;
  <b>Stop:</b> {stop_price:.4f} &nbsp;|&nbsp;
  <b>Target:</b> {target_price:.4f}
</div>
<div id="chart"></div>
<div class="legend">
  <span><span class="swatch" style="background:#4caf50"></span> LQ level</span>
  <span><span class="swatch" style="background:#2196f3"></span> BOS level</span>
  <span><span class="swatch" style="background:#ffeb3b"></span> Entry</span>
  <span><span class="swatch" style="background:#f44336"></span> Stop</span>
  <span><span class="swatch" style="background:#00e676"></span> Target</span>
  <span><span class="swatch" style="background:#ff9800"></span> Breakeven</span>
</div>

<script>
const candles = {candles_json};
const trade = {trade_json};

const chart = LightweightCharts.createChart(document.getElementById('chart'), {{
  layout: {{ background: {{ color: '#0d1117' }}, textColor: '#c9d1d9' }},
  grid: {{ vertLines: {{ color: '#21262d' }}, horzLines: {{ color: '#21262d' }} }},
  timeScale: {{ timeVisible: true, secondsVisible: false }},
}});

const candleSeries = chart.addCandlestickSeries({{
  upColor: '#26a69a', downColor: '#ef5350',
  borderUpColor: '#26a69a', borderDownColor: '#ef5350',
  wickUpColor: '#26a69a', wickDownColor: '#ef5350',
}});
candleSeries.setData(candles);

function priceLine(price, color, title) {{
  candleSeries.createPriceLine({{
    price: price, color: color, lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed,
    axisLabelVisible: true, title: title,
  }});
}}

priceLine(trade.lq_level, '#4caf50', 'LQ');
priceLine(trade.bos_level, '#2196f3', 'BOS');
priceLine(trade.stop_price, '#f44336', 'Stop');
priceLine(trade.target_price, '#00e676', 'Target');
priceLine(trade.breakeven_price, '#ff9800', 'BE');

const markers = [
  {{ time: trade.entry_time, position: 'belowBar', color: '#ffeb3b', shape: 'arrowUp', text: 'Entry' }},
];
if (trade.partial_time) {{
  markers.push({{ time: trade.partial_time, position: 'aboveBar', color: '#00e676', shape: 'circle', text: 'Partial' }});
}}
if (trade.exit_time) {{
  markers.push({{ time: trade.exit_time, position: 'aboveBar', color: '#f44336', shape: 'arrowDown', text: 'Exit' }});
}}
candleSeries.setMarkers(markers);

chart.timeScale().fitContent();
</script>
</body>
</html>
"""


def build_chart_html(candles: list[dict], trade: dict) -> str:
    title = f"{trade['symbol']} - {trade['outcome']} ({trade['return_pct']}%)"
    return CHART_TEMPLATE.format(
        title=title,
        outcome=trade["outcome"],
        return_pct=trade["return_pct"],
        entry_price=trade["entry_price"],
        stop_price=trade["stop_price"],
        target_price=trade["target_price"],
        candles_json=json.dumps(candles),
        trade_json=json.dumps({
            "lq_level": trade["lq_level"],
            "bos_level": trade["bos_level"],
            "stop_price": trade["stop_price"],
            "target_price": trade["target_price"],
            "breakeven_price": trade["breakeven_price"],
            "entry_time": trade["entry_time"],
            "partial_time": trade["partial_time"] if pd.notna(trade["partial_time"]) else None,
            "exit_time": trade["exit_time"] if pd.notna(trade["exit_time"]) else None,
        }),
    )


def candles_to_lwc_format(df: pd.DataFrame) -> list[dict]:
    return [
        {"time": int(row.time), "open": row.open, "high": row.high, "low": row.low, "close": row.close}
        for row in df.itertuples()
    ]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    if not TRADE_LOG_PATH.exists():
        print(f"Trade log not found at {TRADE_LOG_PATH} - run trade_sim_lq_reclaim.py first.")
        return

    trades = pd.read_csv(TRADE_LOG_PATH)
    sampled = sample_trades(trades)
    if sampled.empty:
        print("No trades to sample - check trade_log_lq_reclaim.csv has rows.")
        return

    print(f"Sampled {len(sampled)} trades across {sampled['symbol'].nunique()} symbol(s) "
          f"and {sampled['outcome'].nunique()} outcome type(s).")

    OUTPUT_DIR.mkdir(exist_ok=True)

    # cache one fetched dataframe per symbol so we don't re-fetch per trade
    df_cache: dict[str, pd.DataFrame] = {}
    index_entries = []

    for i, trade in sampled.iterrows():
        symbol = trade["symbol"]
        if symbol not in df_cache:
            # a moderate window is enough for charting purposes - doesn't need
            # the full 20000-candle history the backtest used
            df_cache[symbol] = await fetch_df(symbol, 20000)
        df = df_cache[symbol]

        time_values = df["time"].to_numpy()
        lq_positions = np.flatnonzero(time_values == int(trade["lq_time"]))
        exit_time = trade["exit_time"] if pd.notna(trade["exit_time"]) else trade["entry_time"]
        exit_positions = np.flatnonzero(time_values == int(exit_time))
        if lq_positions.size == 0 or exit_positions.size == 0:
            print(f"  skipping trade {i}: couldn't locate lq_time/exit_time in fetched candles "
                  f"(may be outside the re-fetched window)")
            continue

        lq_idx = int(lq_positions[0])
        exit_idx = int(exit_positions[0])
        start = max(0, lq_idx - CANDLES_BEFORE_LQ)
        end = min(len(df) - 1, exit_idx + CANDLES_AFTER_EXIT)
        window = df.iloc[start:end + 1]

        candles = candles_to_lwc_format(window)
        html = build_chart_html(candles, trade.to_dict())

        filename = f"{symbol}_{trade['outcome']}_{int(trade['entry_time'])}.html"
        out_path = OUTPUT_DIR / filename
        out_path.write_text(html, encoding="utf-8")
        index_entries.append((filename, symbol, trade["outcome"], trade["return_pct"]))
        print(f"  wrote {filename}")

    if index_entries:
        index_html = ["<html><body style='background:#0d1117;color:#c9d1d9;"
                       "font-family:sans-serif;padding:16px'><h2>Trade chart samples</h2><ul>"]
        for filename, symbol, outcome, return_pct in index_entries:
            index_html.append(
                f"<li><a style='color:#58a6ff' href='{filename}'>{symbol} - {outcome} "
                f"({return_pct}%)</a></li>"
            )
        index_html.append("</ul></body></html>")
        (OUTPUT_DIR / "index.html").write_text("\n".join(index_html), encoding="utf-8")
        print(f"\nDone. Open {OUTPUT_DIR / 'index.html'} to browse all samples.")


if __name__ == "__main__":
    asyncio.run(main())
