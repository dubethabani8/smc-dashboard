"""
Trade simulation for the LQ-low reclaim after bullish BOS setup.

Builds on backtest_lq_reclaim.py's setup detection and ATR normalization,
but instead of just measuring "did price move up by X afterward", this
simulates an actual trade with:

  - Entry at the return-to-level candle's low
  - A stop-loss placed below entry (STOP_ATR_MULT * ATR)
  - A partial take-profit above entry (PARTIAL_TARGET_ATR_MULT * ATR) -
    closes PARTIAL_EXIT_FRACTION of the position
  - Stop moved to breakeven (+ small buffer) on the remainder once the
    partial target is hit
  - A cost deduction per closed leg (spread/slippage approximation, in
    ATR terms) - the earlier version measured raw price movement with no
    cost at all, which flatters results
  - A max holding period, after which any still-open remainder is closed
    at the last available price (a "time exit")

Conservative assumption: when a single candle's range could satisfy both
the stop and the target (its low is at/below stop AND its high is at/above
target), the stop is assumed to trigger first. This is the standard
conservative convention when only OHLC (not tick data) is available - it
may understate results slightly, but never overstates them.

Outputs a per-trade log and aggregate stats: win rate, average return per
trade, profit factor, and a simple equity curve with max drawdown.

Run this locally - needs network access to Deriv. Reuses functions from
backtest_lq_reclaim.py, so that file must be in the same folder.
"""
import asyncio
from pathlib import Path

import pandas as pd

from backtest_lq_reclaim import (
    fetch_df, typical_atr, compute_atr, find_candidate_setups,
    SWING_LENGTH, RANGE_PERCENT, TIMEFRAME_SECONDS,
)
import smc_engine

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

SYMBOLS_TO_SIMULATE = ["R_100", "R_75", "R_50", "R_25", "CRASH500", "BOOM500"]
HISTORY_COUNT = 20000

# The validated setup parameters from the earlier ATR sweep - the combo that
# held up most consistently across the time-split and multi-symbol checks.
CLOSENESS_ATR_MULT = 1.0
POPUP_LOOKAHEAD_CANDLES = 16  # kept for reference; the trade sim itself uses MAX_HOLD_CANDLES below

# Minimum candles between LQ origin and the BOS - a liquidity level needs
# SWING_LENGTH candles just to be confirmed via swing detection. A BOS
# happening sooner than that means the level wasn't realistically knowable
# yet in live conditions, even though the batch computation "sees" it.
MIN_CANDLES_LQ_TO_BOS = SWING_LENGTH

# Trade management parameters
STOP_ATR_MULT = 1.0              # stop-loss distance below entry
PARTIAL_TARGET_ATR_MULT = 2.0    # first take-profit distance above entry
PARTIAL_EXIT_FRACTION = 0.5      # fraction of position closed at the partial target
BREAKEVEN_BUFFER_ATR_MULT = 0.05  # small buffer above entry for the breakeven stop, to help cover costs
MAX_HOLD_CANDLES = 48            # give the remainder room to run after breakeven, beyond the original 16-candle lookahead

# Cost model: round-turn cost per closed leg, expressed as a multiple of ATR.
# This approximates spread + slippage. 0.0 disables costs entirely (useful
# for comparing "gross" vs "net of costs" results side by side).
COST_ATR_MULT_PER_LEG = 0.03


# ---------------------------------------------------------------------------
# Trade simulation
# ---------------------------------------------------------------------------

def simulate_trade(df: pd.DataFrame, candidate: dict, atr_at_entry: float) -> dict | None:
    """
    Simulates one trade from a candidate return-to-level event, applying
    the closeness filter first (same as the earlier backtest).

    Entry is priced at the return candle's CLOSE, not its low - you can't
    know a candle's low until after it happens, so deciding to enter once
    that candle has closed near the LQ level, and filling around its close,
    is the realistic assumption. atr_at_entry is this candle's own rolling
    ATR value (backward-looking only), not a global dataset-wide constant.
    """
    closeness_abs = CLOSENESS_ATR_MULT * atr_at_entry
    if candidate["dist_abs"] > closeness_abs:
        return None

    ret_idx = candidate["return_idx"]
    entry_price = df["close"].iloc[ret_idx]
    stop_price = entry_price - STOP_ATR_MULT * atr_at_entry
    target_price = entry_price + PARTIAL_TARGET_ATR_MULT * atr_at_entry
    breakeven_price = entry_price + BREAKEVEN_BUFFER_ATR_MULT * atr_at_entry
    cost_frac = (COST_ATR_MULT_PER_LEG * atr_at_entry) / entry_price

    end_idx = min(ret_idx + MAX_HOLD_CANDLES, len(df) - 1)
    if end_idx <= ret_idx:
        return None

    partial_taken = False
    current_stop = stop_price
    total_return = 0.0
    outcome = "time_exit_full"  # default if nothing triggers before max hold
    partial_time = None
    partial_price = None
    exit_time = None
    exit_price = None

    for i in range(ret_idx + 1, end_idx + 1):
        low = df["low"].iloc[i]
        high = df["high"].iloc[i]

        # conservative: check stop before target within the same candle
        if low <= current_stop:
            exit_time = int(df["time"].iloc[i])
            exit_price = current_stop
            if not partial_taken:
                leg_return = (current_stop - entry_price) / entry_price - cost_frac
                total_return = leg_return
                outcome = "stopped_full_loss"
            else:
                remainder_return = (current_stop - entry_price) / entry_price - cost_frac
                total_return += (1 - PARTIAL_EXIT_FRACTION) * remainder_return
                outcome = "partial_then_breakeven_stop"
            break

        if not partial_taken and high >= target_price:
            leg_return = (target_price - entry_price) / entry_price - cost_frac
            total_return += PARTIAL_EXIT_FRACTION * leg_return
            partial_taken = True
            partial_time = int(df["time"].iloc[i])
            partial_price = target_price
            current_stop = breakeven_price
            # continue the loop - remainder is still live

        if i == end_idx and partial_taken and outcome == "time_exit_full":
            # remainder still open at max hold - close at last close, mark as time exit
            last_close = df["close"].iloc[end_idx]
            exit_time = int(df["time"].iloc[end_idx])
            exit_price = last_close
            remainder_return = (last_close - entry_price) / entry_price - cost_frac
            total_return += (1 - PARTIAL_EXIT_FRACTION) * remainder_return
            outcome = "partial_then_time_exit"

    if outcome == "time_exit_full" and not partial_taken:
        # never hit stop, target, or got a partial - close full position at last close
        last_close = df["close"].iloc[end_idx]
        exit_time = int(df["time"].iloc[end_idx])
        exit_price = last_close
        total_return = (last_close - entry_price) / entry_price - cost_frac

    return {
        "lq_time": candidate["lq_time"],
        "lq_level": candidate["lq_level"],
        "bos_time": candidate["bos_time"],
        "bos_level": candidate["bos_level"],
        "return_time": candidate["return_time"],
        "entry_time": candidate["return_time"],
        "entry_price": entry_price,
        "stop_price": stop_price,
        "target_price": target_price,
        "breakeven_price": breakeven_price,
        "partial_time": partial_time,
        "partial_price": partial_price,
        "exit_time": exit_time,
        "exit_price": exit_price,
        "outcome": outcome,
        "return_pct": round(total_return * 100, 4),
    }


def run_trade_sim(df: pd.DataFrame, symbol_label: str) -> pd.DataFrame:
    atr_series = compute_atr(df)  # rolling, backward-looking only - no future leak
    overlays = smc_engine.compute_all(df, swing_length=SWING_LENGTH, range_percent=RANGE_PERCENT)
    candidates = find_candidate_setups(df, overlays)
    time_to_idx = {t: i for i, t in enumerate(df["time"])}

    trades = []
    seen_lq_bos_pairs = set()
    skipped_unconfirmed = 0
    for c in candidates:
        pair_key = (c["lq_time"], c["bos_time"])
        if pair_key in seen_lq_bos_pairs:
            continue

        lq_idx = time_to_idx.get(c["lq_time"])
        bos_idx = time_to_idx.get(c["bos_time"])
        if lq_idx is None or bos_idx is None:
            continue
        if bos_idx - lq_idx < MIN_CANDLES_LQ_TO_BOS:
            # BOS happened before the LQ level would have been confirmable
            # in live conditions - skip, don't trade on foresight
            skipped_unconfirmed += 1
            continue

        ret_idx = c["return_idx"]
        atr_at_entry = atr_series.iloc[ret_idx]
        if pd.isna(atr_at_entry):
            continue  # still in ATR warm-up period, not enough history yet

        trade = simulate_trade(df, c, atr_at_entry)
        if trade is None:
            continue
        seen_lq_bos_pairs.add(pair_key)
        trade["symbol"] = symbol_label
        trades.append(trade)

    if skipped_unconfirmed:
        print(f"  [{symbol_label}] skipped {skipped_unconfirmed} candidate(s) where BOS preceded "
              f"LQ confirmation (< {MIN_CANDLES_LQ_TO_BOS} candles after LQ origin)")

    return pd.DataFrame(trades)


def summarize_trades(trades: pd.DataFrame, label: str):
    if trades.empty:
        print(f"\n[{label}] No trades generated.")
        return None

    n = len(trades)
    wins = trades[trades["return_pct"] > 0]
    losses = trades[trades["return_pct"] <= 0]
    win_rate = len(wins) / n
    avg_return = trades["return_pct"].mean()
    gross_win = wins["return_pct"].sum()
    gross_loss = abs(losses["return_pct"].sum())
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else float("inf")

    equity = trades["return_pct"].cumsum()
    running_max = equity.cummax()
    drawdown = equity - running_max
    max_drawdown = drawdown.min()

    print(f"\n=== [{label}] Trade simulation summary ({n} trades) ===")
    print(f"  win_rate:        {win_rate:.3f}")
    print(f"  avg_return_pct:  {avg_return:.4f}  (per trade, on notional entry price)")
    print(f"  profit_factor:   {profit_factor:.3f}")
    print(f"  total_return_pct: {equity.iloc[-1]:.3f}  (sum of all trades, additive - not compounded)")
    print(f"  max_drawdown_pct: {max_drawdown:.3f}  (peak-to-trough on the additive equity curve)")
    print("  outcome breakdown:")
    print(trades["outcome"].value_counts().to_string())

    return {
        "label": label, "n_trades": n, "win_rate": round(win_rate, 3),
        "avg_return_pct": round(avg_return, 4), "profit_factor": round(profit_factor, 3),
        "total_return_pct": round(equity.iloc[-1], 3), "max_drawdown_pct": round(max_drawdown, 3),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    out_dir = Path(__file__).resolve().parent
    all_trades = []
    summary_rows = []

    for symbol in SYMBOLS_TO_SIMULATE:
        try:
            df = await fetch_df(symbol, HISTORY_COUNT)
        except Exception as exc:
            print(f"  {symbol}: fetch failed ({exc}) - skipping")
            continue

        trades = run_trade_sim(df, symbol)
        summary = summarize_trades(trades, symbol)
        if summary:
            summary_rows.append(summary)
        if not trades.empty:
            all_trades.append(trades)

    if all_trades:
        combined = pd.concat(all_trades, ignore_index=True)
        combined.to_csv(out_dir / "trade_log_lq_reclaim.csv", index=False)
        summarize_trades(combined, "ALL SYMBOLS COMBINED")

    if summary_rows:
        summary_df = pd.DataFrame(summary_rows)
        print("\n=== Per-symbol summary ===")
        print(summary_df.to_string(index=False))
        summary_df.to_csv(out_dir / "trade_sim_summary.csv", index=False)


if __name__ == "__main__":
    asyncio.run(main())
