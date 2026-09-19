"""
Paper-trade tracker for the LQ-Reclaim live alerts.

Usage:
  1. Keep a text file (e.g. alerts.txt) where you paste raw copied text from
     the Telegram bot - entry signals, partial-hit messages, and close
     messages, in any order, appended over time as new alerts arrive.
  2. Run: python paper_trade_tracker.py alerts.txt
  3. It parses everything, links partials/closes back to their entry by
     (symbol, entry_time), and prints a running trade table plus summary
     stats - compared directly against the backtest's benchmark numbers.

No need to reformat anything - just paste blocks of message text as-is,
copy-pasted straight from Telegram (this tolerates both literal <b>/<a>
HTML tags if present, and already-rendered plain/markdown text).
"""
import re
import sys
from pathlib import Path

import pandas as pd

# Backtest benchmark to compare live results against (first-per-level,
# the conservative/independent view from the independence audit)
BACKTEST_WIN_RATE = 0.546
BACKTEST_PROFIT_FACTOR = 2.551

# Flag any two entry signals on the same symbol within this many hours as a
# possible correlated cluster (the same underlying zone detected twice)
DUPLICATE_WINDOW_HOURS = 3

ENTRY_RE = re.compile(
    r"LQ-RECLAIM signal.*?-\s*(?P<symbol>\S+).*?"
    r"Entry:\s*([\d.]+).*?"
    r"Stop:\s*([\d.]+).*?Target \(partial\):\s*([\d.]+).*?"
    r"Breakeven.*?:\s*([\d.]+).*?"
    r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}) UTC",
    re.DOTALL,
)

CLOSE_RE = re.compile(
    r"Trade closed\s*-\s*(?P<symbol>\S+).*?"
    r"Outcome:\s*(?P<outcome>[^\n]+).*?"
    r"Return:\s*(?P<ret>-?[\d.]+)%.*?"
    r"Entry:\s*([\d.]+)\s*\S+\s*Exit:\s*([\d.]+).*?"
    r"Entered\s*(?P<entry_ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}) UTC\s*\S+\s*Closed\s*(?P<close_ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}) UTC",
    re.DOTALL,
)


def parse_alerts(text: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    entries = []
    for m in ENTRY_RE.finditer(text):
        entries.append({
            "symbol": m.group("symbol"),
            "entry_price": float(m.group(2)),
            "stop_price": float(m.group(3)),
            "target_price": float(m.group(4)),
            "breakeven_price": float(m.group(5)),
            "entry_time": pd.Timestamp(m.group("ts")),
        })
    entries_df = pd.DataFrame(entries).drop_duplicates(subset=["symbol", "entry_time"])

    closes = []
    for m in CLOSE_RE.finditer(text):
        closes.append({
            "symbol": m.group("symbol"),
            "outcome": m.group("outcome").strip(),
            "return_pct": float(m.group("ret")),
            "entry_time": pd.Timestamp(m.group("entry_ts")),
            "close_time": pd.Timestamp(m.group("close_ts")),
        })
    closes_df = pd.DataFrame(closes).drop_duplicates(subset=["symbol", "entry_time"])

    return entries_df, closes_df


def find_near_duplicates(entries_df: pd.DataFrame) -> list[str]:
    warnings = []
    for symbol, group in entries_df.groupby("symbol"):
        times = group.sort_values("entry_time")["entry_time"].tolist()
        for a, b in zip(times, times[1:]):
            gap_hours = (b - a).total_seconds() / 3600
            if gap_hours <= DUPLICATE_WINDOW_HOURS:
                warnings.append(
                    f"  {symbol}: entries at {a} and {b} are only {gap_hours:.1f}h apart - "
                    f"likely correlated, not independent signals"
                )
    return warnings


def main():
    if len(sys.argv) < 2:
        print("Usage: python paper_trade_tracker.py <alerts.txt>")
        return

    path = Path(sys.argv[1])
    if not path.exists():
        print(f"File not found: {path}")
        return

    text = path.read_text(encoding="utf-8")
    entries_df, closes_df = parse_alerts(text)

    if entries_df.empty:
        print("No entry signals found - check the file has raw pasted alert text.")
        return

    merged = entries_df.merge(closes_df, on=["symbol", "entry_time"], how="left", suffixes=("", "_close"))
    merged["status"] = merged["outcome"].apply(lambda o: "CLOSED" if pd.notna(o) else "OPEN")

    print(f"\n=== All signals ({len(merged)} total) ===")
    display_cols = ["entry_time", "symbol", "entry_price", "stop_price", "target_price",
                     "status", "outcome", "return_pct"]
    print(merged[display_cols].sort_values("entry_time").to_string(index=False))

    closed = merged[merged["status"] == "CLOSED"]
    open_trades = merged[merged["status"] == "OPEN"]

    if not closed.empty:
        n = len(closed)
        wins = closed[closed["return_pct"] > 0]
        win_rate = len(wins) / n
        gross_win = wins["return_pct"].sum()
        gross_loss = abs(closed.loc[closed["return_pct"] <= 0, "return_pct"].sum())
        profit_factor = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
        avg_return = closed["return_pct"].mean()
        total_return = closed["return_pct"].sum()

        print(f"\n=== Live results so far ({n} closed trades, {len(open_trades)} still open) ===")
        print(f"  win_rate:        {win_rate:.3f}   (backtest benchmark: {BACKTEST_WIN_RATE:.3f})")
        print(f"  profit_factor:   {profit_factor:.3f}   (backtest benchmark: {BACKTEST_PROFIT_FACTOR:.3f})")
        print(f"  avg_return_pct:  {avg_return:.4f}")
        print(f"  total_return_pct: {total_return:.3f}")
        if n < 30:
            print(f"\n  Note: only {n} closed trades so far - way too few to draw real conclusions yet. "
                  f"The backtest's own reliability bar was 30+ per parameter combo.")
    else:
        print("\nNo closed trades yet - nothing to compare against the backtest.")

    dup_warnings = find_near_duplicates(entries_df)
    if dup_warnings:
        print(f"\n=== Possible correlated signal clusters (within {DUPLICATE_WINDOW_HOURS}h on the same symbol) ===")
        for w in dup_warnings:
            print(w)


if __name__ == "__main__":
    main()
