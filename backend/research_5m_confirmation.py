"""
Research: does requiring a simple 5m "stabilization" signal at the entry
candle improve the LQ-reclaim setup, or just add noise?

Hypothesis being tested (a starting point, not a conclusion):
  At the 15m candle that triggers entry, that candle is itself made up of
  three 5m candles. Require the LAST of those three 5m candles to have a
  HIGHER LOW than the one before it - i.e. price has stopped making a
  fresh intra-bar low right at the moment of entry, instead of still
  falling straight through the level.

  This uses no future data - by the time the 15m candle closes (which is
  when entry already happens), all three of its 5m candles are complete.

This is NOT wired into the live bot. It only runs the existing, already-
validated trade simulation twice per symbol - once with this filter
applied, once without - and reports both, so you can see honestly whether
the filter would have helped, hurt, or done nothing before ever
considering putting it live.

Run locally - needs network access to Deriv.
"""
import asyncio
from pathlib import Path

import pandas as pd

from backtest_lq_reclaim import compute_atr, find_candidate_setups, SWING_LENGTH, RANGE_PERCENT
from trade_sim_lq_reclaim import (
    fetch_df, simulate_trade, summarize_trades,
    MIN_CANDLES_LQ_TO_BOS, TIMEFRAME_SECONDS,
)
import smc_engine

SYMBOLS = ["R_100", "R_75", "R_50", "R_25", "CRASH500", "BOOM500"]
HISTORY_COUNT_15M = 20000
FIVE_MIN_GRANULARITY = 300
# 3x the 15m candle count covers the same calendar span at 5m resolution
HISTORY_COUNT_5M = HISTORY_COUNT_15M * 3


def has_5m_confirmation(df5: pd.DataFrame, entry_time_15m: int) -> bool | None:
    """
    Checks the three 5m candles that make up the 15m entry bar
    (entry_time_15m, +300, +600). Returns True if the last one has a
    higher low than the one before it, False if not, None if the 5m data
    needed isn't available (edge of fetched history).
    """
    times = df5["time"].to_numpy()
    idx_mid = None
    idx_last = None
    mid_time = entry_time_15m + 300
    last_time = entry_time_15m + 600
    mid_matches = df5.index[times == mid_time]
    last_matches = df5.index[times == last_time]
    if len(mid_matches) == 0 or len(last_matches) == 0:
        return None
    mid_low = df5.loc[mid_matches[0], "low"]
    last_low = df5.loc[last_matches[0], "low"]
    return last_low > mid_low


def run_variant(df15: pd.DataFrame, df5: pd.DataFrame, symbol_label: str, require_confirmation: bool) -> pd.DataFrame:
    atr_series = compute_atr(df15)
    overlays = smc_engine.compute_all(df15, swing_length=SWING_LENGTH, range_percent=RANGE_PERCENT)
    candidates = find_candidate_setups(df15, overlays)
    time_to_idx = {t: i for i, t in enumerate(df15["time"])}

    trades = []
    seen_pairs = set()
    skipped_no_confirmation = 0
    for c in candidates:
        pair_key = (c["lq_time"], c["bos_time"])
        if pair_key in seen_pairs:
            continue

        lq_idx = time_to_idx.get(c["lq_time"])
        bos_idx = time_to_idx.get(c["bos_time"])
        if lq_idx is None or bos_idx is None:
            continue
        if bos_idx - lq_idx < MIN_CANDLES_LQ_TO_BOS:
            continue

        ret_idx = c["return_idx"]
        atr_at_entry = atr_series.iloc[ret_idx]
        if pd.isna(atr_at_entry):
            continue

        if require_confirmation:
            entry_time = int(df15["time"].iloc[ret_idx])
            confirmed = has_5m_confirmation(df5, entry_time)
            if confirmed is None:
                continue  # 5m data unavailable for this window - skip rather than guess
            if not confirmed:
                skipped_no_confirmation += 1
                continue

        trade = simulate_trade(df15, c, atr_at_entry)
        if trade is None:
            continue
        seen_pairs.add(pair_key)
        trade["symbol"] = symbol_label
        trades.append(trade)

    label = f"{symbol_label} ({'WITH' if require_confirmation else 'WITHOUT'} 5m confirmation)"
    if require_confirmation:
        print(f"  [{label}] {skipped_no_confirmation} candidate(s) rejected for lacking 5m confirmation")
    return pd.DataFrame(trades)


async def main():
    out_dir = Path(__file__).resolve().parent
    all_with = []
    all_without = []

    for symbol in SYMBOLS:
        try:
            df15 = await fetch_df(symbol, HISTORY_COUNT_15M)
            print(f"Fetching 5m data for {symbol} ...")
            import deriv_client
            candles_5m = await deriv_client.fetch_candle_history_paginated(
                symbol, FIVE_MIN_GRANULARITY, HISTORY_COUNT_5M
            )
            df5 = smc_engine.build_dataframe(candles_5m)
            print(f"Got {len(df5)} 5m candles for {symbol}: {df5['time'].iloc[0]} -> {df5['time'].iloc[-1]}")
        except Exception as exc:
            print(f"  {symbol}: fetch failed ({exc}) - skipping")
            continue

        trades_without = run_variant(df15, df5, symbol, require_confirmation=False)
        trades_with = run_variant(df15, df5, symbol, require_confirmation=True)

        summarize_trades(trades_without, f"{symbol} - WITHOUT 5m confirmation (baseline)")
        summarize_trades(trades_with, f"{symbol} - WITH 5m confirmation (filtered)")

        if not trades_without.empty:
            all_without.append(trades_without)
        if not trades_with.empty:
            all_with.append(trades_with)

    if all_without:
        combined_without = pd.concat(all_without, ignore_index=True)
        summarize_trades(combined_without, "ALL SYMBOLS - WITHOUT 5m confirmation (baseline)")
        combined_without.to_csv(out_dir / "research_5m_confirmation_baseline.csv", index=False)

    if all_with:
        combined_with = pd.concat(all_with, ignore_index=True)
        summarize_trades(combined_with, "ALL SYMBOLS - WITH 5m confirmation (filtered)")
        combined_with.to_csv(out_dir / "research_5m_confirmation_filtered.csv", index=False)

    print(
        "\nHow to read this: compare win_rate and profit_factor between the WITHOUT and WITH "
        "summaries, both per-symbol and combined. If WITH is clearly better on profit_factor "
        "without shrinking n_trades to nothing, the confirmation signal is worth pursuing further "
        "(time-split and multi-symbol validation next, same as the original strategy went through). "
        "If it's the same or worse, or n_trades collapses too far to trust, this specific rule isn't "
        "the answer - a different confirmation definition might still be worth trying, but this one "
        "wasn't it."
    )


if __name__ == "__main__":
    asyncio.run(main())
