"""
Backtest: LQ-low reclaim after a bullish BOS.

Setup being tested:
  1. A liquidity LOW forms (direction == "bearish" - a level resting below
     price, built from a swing-low cluster).
  2. After that, price rallies and prints a bullish BOS (breaks a prior
     swing high).
  3. Price pulls back down toward the liquidity low's level, getting within
     some threshold distance of it (candidate "closeness" values below).
  4. Shortly after that return, price pops back up (candidate "pop-up"
     definitions below).

This does NOT place trades or compute PnL - it just measures what actually
happens after each candidate setup, across a grid of parameter choices, so
you can see which threshold values produce a real, repeatable edge instead
of guessing.

Run this locally or as a one-off Railway job (not part of the live app).
It needs network access to Deriv, so it won't run in a sandboxed
environment without that.
"""
import asyncio
import itertools
import sys
from pathlib import Path

import pandas as pd
import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

sys.path.insert(0, str(Path(__file__).resolve().parent))  # so `import deriv_client` etc. work if run from elsewhere

import deriv_client
import smc_engine

# ---------------------------------------------------------------------------
# CONFIG - edit these to control what gets tested
# ---------------------------------------------------------------------------

SYMBOL = "R_100"          # Deriv symbol code to test against
TIMEFRAME_SECONDS = 900   # 15m candles
HISTORY_COUNT = 20000     # how far back to pull - now uses pagination, so this can go much higher than 1000
SWING_LENGTH = 10
RANGE_PERCENT = 0.01

# How close (as a fraction of price) does price need to come to the LQ low
# level to count as "returned to the level"? e.g. 0.001 = within 0.1% of price.
CLOSENESS_FRACTIONS = [0.0005, 0.001, 0.002, 0.005]

# How many candles after the "return to level" do we look for a pop-up?
LOOKAHEAD_CANDLES = [4, 8, 16]

# What counts as a "pop up"? Expressed as a fraction of price move up from
# the return-candle's low, measured as the best (highest high) reached
# within the lookahead window. Kept at 0.5%+ - anything smaller is just
# normal synthetic-index noise, not a meaningful reaction.
POPUP_FRACTIONS = [0.005, 0.01, 0.015, 0.02]

# How far after the LQ low's origin are we willing to look for the
# qualifying bullish BOS? (in candles). Keeps things from matching a BOS
# that's completely unrelated, weeks later.
MAX_CANDLES_LQ_TO_BOS = 200

# How far after the BOS are we willing to look for the retest/return to the
# LQ level? (in candles)
MAX_CANDLES_BOS_TO_RETURN = 200


def compute_baseline_rates(df: pd.DataFrame, lookaheads: list[int], popup_fractions: list[float]) -> dict:
    """
    For comparison: what fraction of ALL candles (not tied to any LQ/BOS
    setup) would "hit" each popup threshold within each lookahead window,
    just from ordinary price movement? Without this, a high hit rate on the
    actual setup is meaningless - synthetic indices move constantly, so a
    setup needs to clearly beat this baseline to be worth anything.
    """
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    n = len(df)

    baseline = {}
    for lookahead in set(lookaheads):
        if n <= lookahead:
            continue
        windows = sliding_window_view(highs[1:], lookahead)  # windows[k] = highs[k+1 : k+1+lookahead]
        forward_max = windows.max(axis=1)
        valid_lows = lows[: len(forward_max)]
        move_up_frac = (forward_max - valid_lows) / valid_lows
        for popup in set(popup_fractions):
            baseline[(lookahead, popup)] = float((move_up_frac >= popup).mean())
    return baseline


# ---------------------------------------------------------------------------
# Setup detection
# ---------------------------------------------------------------------------

def find_candidate_setups(df: pd.DataFrame, overlays: dict) -> list[dict]:
    """
    Finds every (lq_low, bullish_bos, return_candle) combination that fits
    the basic shape of the setup, independent of the closeness/pop-up
    thresholds (those get applied later, per parameter combo, so we only
    have to do this scan once).
    """
    time_to_idx = {t: i for i, t in enumerate(df["time"])}

    lq_lows = [item for item in overlays["liquidity"] if item["direction"] == "bearish"]
    bull_bos = [item for item in overlays["bos_choch"] if item["kind"] == "BOS" and item["direction"] == "bullish"]

    candidates = []
    for lq in lq_lows:
        lq_idx = time_to_idx.get(lq["time"])
        if lq_idx is None:
            continue

        # find a qualifying bullish BOS shortly after this LQ low formed
        for bos in bull_bos:
            bos_idx = time_to_idx.get(bos["time"])
            if bos_idx is None:
                continue
            gap = bos_idx - lq_idx
            if gap <= 0 or gap > MAX_CANDLES_LQ_TO_BOS:
                continue

            # find the first candle after the BOS whose low comes back down
            # near the LQ level - closeness is checked later per threshold,
            # so here we just record the distance at each candle
            end_idx = min(bos_idx + MAX_CANDLES_BOS_TO_RETURN, len(df) - 1)
            for ret_idx in range(bos_idx + 1, end_idx + 1):
                low = df["low"].iloc[ret_idx]
                dist_frac = abs(low - lq["level"]) / lq["level"]
                candidates.append({
                    "lq_time": lq["time"],
                    "lq_level": lq["level"],
                    "bos_time": bos["time"],
                    "bos_level": bos["level"],
                    "return_idx": ret_idx,
                    "return_time": int(df["time"].iloc[ret_idx]),
                    "return_low": low,
                    "dist_frac": dist_frac,
                })
    return candidates


def evaluate_outcome(df: pd.DataFrame, candidate: dict, closeness: float,
                      lookahead: int, popup_frac: float) -> dict | None:
    """
    For a candidate return-to-level event, checks whether it actually
    qualifies under this closeness threshold, and if so, measures the
    outcome under this lookahead/popup definition.
    """
    if candidate["dist_frac"] > closeness:
        return None

    ret_idx = candidate["return_idx"]
    entry_price = candidate["return_low"]
    end_idx = min(ret_idx + lookahead, len(df) - 1)
    if end_idx <= ret_idx:
        return None

    window = df.iloc[ret_idx + 1: end_idx + 1]
    if window.empty:
        return None

    best_high = window["high"].max()
    move_up_frac = (best_high - entry_price) / entry_price
    hit_popup = move_up_frac >= popup_frac

    worst_low = window["low"].min()
    drawdown_frac = (entry_price - worst_low) / entry_price

    return {
        "hit_popup": hit_popup,
        "move_up_frac": move_up_frac,
        "drawdown_frac": drawdown_frac,
    }


# ---------------------------------------------------------------------------
# Main sweep
# ---------------------------------------------------------------------------

async def main():
    print(f"Fetching up to {HISTORY_COUNT} candles for {SYMBOL} @ {TIMEFRAME_SECONDS}s (paginated) ...")
    candles = await deriv_client.fetch_candle_history_paginated(SYMBOL, TIMEFRAME_SECONDS, HISTORY_COUNT)
    df = smc_engine.build_dataframe(candles)
    print(f"Got {len(df)} candles: {df['time'].iloc[0]} -> {df['time'].iloc[-1]}")

    print("Computing overlays (swings, BOS/CHoCH, liquidity) ...")
    overlays = smc_engine.compute_all(df, swing_length=SWING_LENGTH, range_percent=RANGE_PERCENT)

    print("Scanning for candidate setups (LQ low -> bullish BOS -> return) ...")
    candidates = find_candidate_setups(df, overlays)
    print(f"Found {len(candidates)} raw (lq, bos, return-candle) combinations before thresholding.\n")

    print("Computing baseline (random-candle) hit rates for comparison ...")
    baseline_rates = compute_baseline_rates(df, LOOKAHEAD_CANDLES, POPUP_FRACTIONS)

    rows = []
    for closeness, lookahead, popup_frac in itertools.product(
        CLOSENESS_FRACTIONS, LOOKAHEAD_CANDLES, POPUP_FRACTIONS
    ):
        outcomes = []
        seen_lq_bos_pairs = set()  # only count the FIRST qualifying return per (lq, bos) pair
        for c in candidates:
            pair_key = (c["lq_time"], c["bos_time"])
            if pair_key in seen_lq_bos_pairs:
                continue
            result = evaluate_outcome(df, c, closeness, lookahead, popup_frac)
            if result is None:
                continue
            seen_lq_bos_pairs.add(pair_key)
            outcomes.append(result)

        if not outcomes:
            continue

        n = len(outcomes)
        hit_rate = sum(o["hit_popup"] for o in outcomes) / n
        avg_move_up = sum(o["move_up_frac"] for o in outcomes) / n
        avg_drawdown = sum(o["drawdown_frac"] for o in outcomes) / n
        baseline_hit_rate = baseline_rates.get((lookahead, popup_frac), float("nan"))

        rows.append({
            "closeness_pct": closeness * 100,
            "lookahead_candles": lookahead,
            "popup_pct": popup_frac * 100,
            "n_setups": n,
            "hit_rate": round(hit_rate, 3),
            "baseline_hit_rate": round(baseline_hit_rate, 3),
            "edge": round(hit_rate - baseline_hit_rate, 3),
            "avg_move_up_pct": round(avg_move_up * 100, 3),
            "avg_drawdown_pct": round(avg_drawdown * 100, 3),
        })

    if not rows:
        print("No setups matched any parameter combination - try loosening the config values.")
        return

    results = pd.DataFrame(rows)

    MIN_SETUPS = 30  # don't trust a hit rate computed from fewer samples than this
    reliable = results[results["n_setups"] >= MIN_SETUPS].sort_values(
        ["edge", "n_setups"], ascending=[False, False]
    )
    unreliable = results[results["n_setups"] < MIN_SETUPS]

    pd.set_option("display.width", 160)
    pd.set_option("display.max_rows", 100)

    if not reliable.empty:
        print(f"\n=== Reliable results (n_setups >= {MIN_SETUPS}), ranked by edge over baseline ===")
        print(reliable.to_string(index=False))
    else:
        print(f"\nNo parameter combo reached {MIN_SETUPS}+ setups - you likely need more history "
              f"(raise HISTORY_COUNT) or looser thresholds before trusting any hit rate here.")

    if not unreliable.empty:
        print(f"\n=== Below the {MIN_SETUPS}-setup trust threshold (shown for reference only) ===")
        print(unreliable.sort_values(["edge", "n_setups"], ascending=[False, False]).to_string(index=False))

    out_path = Path(__file__).resolve().parent / f"backtest_lq_reclaim_{SYMBOL}.csv"
    results.to_csv(out_path, index=False)
    print(f"\nFull results written to {out_path}")


if __name__ == "__main__":
    asyncio.run(main())