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
happens after each candidate setup, across a grid of parameter choices,
compared against a random-candle baseline, so you can see which threshold
values produce a real, repeatable edge instead of guessing.

Three checks run in sequence (toggle with the DO_* flags below):
  1. Full-period sweep on SYMBOL - the original grid search.
  2. Time-split check - same sweep run separately on the first and second
     half of history, to see if the top parameter zone holds up out of
     sample rather than being a fluke of one stretch of price action.
  3. Multi-symbol check - the specific candidate parameter combos (edit
     CANDIDATE_PARAMS_TO_TRACK below, based on what looked best from step 1)
     tested across several other symbols, to see if the edge generalizes or
     was specific to one instrument.

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

SYMBOL = "R_100"          # Deriv symbol code for the full-period and time-split checks
TIMEFRAME_SECONDS = 900   # 15m candles
HISTORY_COUNT = 20000     # how far back to pull - uses pagination, so this can go well past 1000
SWING_LENGTH = 10
RANGE_PERCENT = 0.01

CLOSENESS_FRACTIONS = [0.0005, 0.001, 0.002, 0.005]
LOOKAHEAD_CANDLES = [4, 8, 16]
POPUP_FRACTIONS = [0.005, 0.01, 0.015, 0.02]

MAX_CANDLES_LQ_TO_BOS = 200
MAX_CANDLES_BOS_TO_RETURN = 200

MIN_SETUPS = 30  # don't trust a hit rate computed from fewer samples than this

DO_FULL_SWEEP = True
DO_TIME_SPLIT_CHECK = True
DO_MULTI_SYMBOL_CHECK = True

# Fill these in after looking at the full-period sweep results - the combos
# that showed the strongest edge there. Format: (closeness_frac, lookahead, popup_frac)
CANDIDATE_PARAMS_TO_TRACK = [
    (0.002, 16, 0.02),
    (0.005, 16, 0.015),
    (0.002, 16, 0.015),
]

# Symbols to check the candidate params against. Keep this list modest -
# each one needs a full paginated fetch.
SYMBOLS_FOR_MULTI_CHECK = ["R_75", "R_50", "R_25", "CRASH500", "BOOM500"]
MULTI_SYMBOL_HISTORY_COUNT = 10000  # smaller than the main HISTORY_COUNT to keep total runtime reasonable


# ---------------------------------------------------------------------------
# Baseline (random-candle) comparison
# ---------------------------------------------------------------------------

def compute_baseline_rates(df: pd.DataFrame, lookaheads: list[int], popup_fractions: list[float]) -> dict:
    """
    What fraction of ALL candles (not tied to any LQ/BOS setup) would "hit"
    each popup threshold within each lookahead window, from ordinary price
    movement alone? A setup needs to clearly beat this to mean anything.
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

        for bos in bull_bos:
            bos_idx = time_to_idx.get(bos["time"])
            if bos_idx is None:
                continue
            gap = bos_idx - lq_idx
            if gap <= 0 or gap > MAX_CANDLES_LQ_TO_BOS:
                continue

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
    Checks whether a candidate return-to-level event qualifies under this
    closeness threshold, and if so, measures the outcome under this
    lookahead/popup definition.
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
# Reusable sweep - runs the full grid search over one dataframe
# ---------------------------------------------------------------------------

def run_sweep(df: pd.DataFrame, closeness_fractions=None, lookahead_candles=None,
              popup_fractions=None) -> pd.DataFrame:
    """
    Runs the full (closeness, lookahead, popup) grid search against one
    dataframe and returns a results table with hit_rate, baseline_hit_rate,
    and edge columns. Uses the module-level CONFIG grids unless overridden -
    override is used by the multi-symbol check, which only needs a handful
    of specific combos, not the full grid.
    """
    closeness_fractions = closeness_fractions or CLOSENESS_FRACTIONS
    lookahead_candles = lookahead_candles or LOOKAHEAD_CANDLES
    popup_fractions = popup_fractions or POPUP_FRACTIONS

    overlays = smc_engine.compute_all(df, swing_length=SWING_LENGTH, range_percent=RANGE_PERCENT)
    candidates = find_candidate_setups(df, overlays)
    baseline_rates = compute_baseline_rates(df, lookahead_candles, popup_fractions)

    rows = []
    for closeness, lookahead, popup_frac in itertools.product(
        closeness_fractions, lookahead_candles, popup_fractions
    ):
        outcomes = []
        seen_lq_bos_pairs = set()
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
            "closeness_pct": round(closeness * 100, 4),
            "lookahead_candles": lookahead,
            "popup_pct": round(popup_frac * 100, 4),
            "n_setups": n,
            "hit_rate": round(hit_rate, 3),
            "baseline_hit_rate": round(baseline_hit_rate, 3),
            "edge": round(hit_rate - baseline_hit_rate, 3),
            "avg_move_up_pct": round(avg_move_up * 100, 3),
            "avg_drawdown_pct": round(avg_drawdown * 100, 3),
        })

    return pd.DataFrame(rows)


def print_sweep_results(results: pd.DataFrame, label: str, min_setups: int = MIN_SETUPS):
    pd.set_option("display.width", 160)
    pd.set_option("display.max_rows", 100)

    if results.empty:
        print(f"\n[{label}] No setups matched any parameter combination.")
        return

    reliable = results[results["n_setups"] >= min_setups].sort_values(
        ["edge", "n_setups"], ascending=[False, False]
    )
    unreliable = results[results["n_setups"] < min_setups]

    print(f"\n=== [{label}] Reliable results (n_setups >= {min_setups}), ranked by edge over baseline ===")
    if not reliable.empty:
        print(reliable.to_string(index=False))
    else:
        print(f"None reached {min_setups}+ setups - need more history or looser thresholds here.")

    if not unreliable.empty:
        print(f"\n=== [{label}] Below the {min_setups}-setup trust threshold (reference only) ===")
        print(unreliable.sort_values(["edge", "n_setups"], ascending=[False, False]).to_string(index=False))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def fetch_df(symbol: str, history_count: int) -> pd.DataFrame:
    print(f"Fetching up to {history_count} candles for {symbol} @ {TIMEFRAME_SECONDS}s (paginated) ...")
    candles = await deriv_client.fetch_candle_history_paginated(symbol, TIMEFRAME_SECONDS, history_count)
    df = smc_engine.build_dataframe(candles)
    print(f"Got {len(df)} candles for {symbol}: {df['time'].iloc[0]} -> {df['time'].iloc[-1]}")
    return df


async def main():
    out_dir = Path(__file__).resolve().parent

    df_full = None
    if DO_FULL_SWEEP or DO_TIME_SPLIT_CHECK:
        df_full = await fetch_df(SYMBOL, HISTORY_COUNT)

    # --- 1. Full-period sweep -------------------------------------------------
    if DO_FULL_SWEEP:
        print(f"\n########## FULL-PERIOD SWEEP: {SYMBOL} ##########")
        results_full = run_sweep(df_full)
        print_sweep_results(results_full, f"{SYMBOL} FULL PERIOD ({len(df_full)} candles)")
        results_full.to_csv(out_dir / f"backtest_lq_reclaim_{SYMBOL}_full.csv", index=False)

    # --- 2. Time-split check ---------------------------------------------------
    if DO_TIME_SPLIT_CHECK:
        print(f"\n########## TIME-SPLIT CHECK: {SYMBOL} ##########")
        mid = len(df_full) // 2
        df_first = df_full.iloc[:mid].reset_index(drop=True)
        df_second = df_full.iloc[mid:].reset_index(drop=True)

        results_first = run_sweep(df_first)
        results_second = run_sweep(df_second)

        print_sweep_results(
            results_first,
            f"{SYMBOL} FIRST HALF ({df_first['time'].iloc[0]} -> {df_first['time'].iloc[-1]})",
        )
        print_sweep_results(
            results_second,
            f"{SYMBOL} SECOND HALF ({df_second['time'].iloc[0]} -> {df_second['time'].iloc[-1]})",
        )

        # side-by-side comparison for the tracked candidate combos specifically -
        # this is the number that actually tells you if the edge is real
        print(f"\n=== [{SYMBOL}] Candidate params: first half vs second half ===")
        compare_rows = []
        for closeness, lookahead, popup in CANDIDATE_PARAMS_TO_TRACK:
            row1 = results_first[
                (results_first.closeness_pct.round(4) == round(closeness * 100, 4))
                & (results_first.lookahead_candles == lookahead)
                & (results_first.popup_pct.round(4) == round(popup * 100, 4))
            ]
            row2 = results_second[
                (results_second.closeness_pct.round(4) == round(closeness * 100, 4))
                & (results_second.lookahead_candles == lookahead)
                & (results_second.popup_pct.round(4) == round(popup * 100, 4))
            ]
            compare_rows.append({
                "closeness_pct": round(closeness * 100, 4),
                "lookahead": lookahead,
                "popup_pct": round(popup * 100, 4),
                "first_half_n": row1["n_setups"].iloc[0] if not row1.empty else 0,
                "first_half_edge": row1["edge"].iloc[0] if not row1.empty else None,
                "second_half_n": row2["n_setups"].iloc[0] if not row2.empty else 0,
                "second_half_edge": row2["edge"].iloc[0] if not row2.empty else None,
            })
        compare_df = pd.DataFrame(compare_rows)
        print(compare_df.to_string(index=False))
        compare_df.to_csv(out_dir / f"backtest_lq_reclaim_{SYMBOL}_split_comparison.csv", index=False)
        print(
            "\nRead this as: if first_half_edge and second_half_edge are both clearly positive and "
            "similar in size, the pattern likely generalizes across time. If one half is strongly "
            "positive and the other is near zero or negative, the full-period result was probably "
            "driven by one stretch of price action, not a repeatable edge."
        )

    # --- 3. Multi-symbol check --------------------------------------------------
    if DO_MULTI_SYMBOL_CHECK:
        print(f"\n########## MULTI-SYMBOL CHECK ##########")
        closeness_list = sorted({c for c, _, _ in CANDIDATE_PARAMS_TO_TRACK})
        lookahead_list = sorted({l for _, l, _ in CANDIDATE_PARAMS_TO_TRACK})
        popup_list = sorted({p for _, _, p in CANDIDATE_PARAMS_TO_TRACK})

        multi_rows = []
        for sym in SYMBOLS_FOR_MULTI_CHECK:
            try:
                sym_df = await fetch_df(sym, MULTI_SYMBOL_HISTORY_COUNT)
                sym_results = run_sweep(
                    sym_df,
                    closeness_fractions=closeness_list,
                    lookahead_candles=lookahead_list,
                    popup_fractions=popup_list,
                )
            except Exception as exc:
                print(f"  {sym}: failed ({exc}) - skipping")
                continue

            for closeness, lookahead, popup in CANDIDATE_PARAMS_TO_TRACK:
                row = sym_results[
                    (sym_results.closeness_pct.round(4) == round(closeness * 100, 4))
                    & (sym_results.lookahead_candles == lookahead)
                    & (sym_results.popup_pct.round(4) == round(popup * 100, 4))
                ] if not sym_results.empty else pd.DataFrame()
                if row.empty:
                    multi_rows.append({
                        "symbol": sym, "closeness_pct": round(closeness * 100, 4),
                        "lookahead": lookahead, "popup_pct": round(popup * 100, 4),
                        "n_setups": 0, "hit_rate": None, "baseline_hit_rate": None, "edge": None,
                    })
                else:
                    r = row.iloc[0]
                    multi_rows.append({
                        "symbol": sym, "closeness_pct": round(closeness * 100, 4),
                        "lookahead": lookahead, "popup_pct": round(popup * 100, 4),
                        "n_setups": r["n_setups"], "hit_rate": r["hit_rate"],
                        "baseline_hit_rate": r["baseline_hit_rate"], "edge": r["edge"],
                    })

        multi_df = pd.DataFrame(multi_rows)
        print(f"\n=== Candidate params tested across other symbols ===")
        if not multi_df.empty:
            print(multi_df.to_string(index=False))
            multi_df.to_csv(out_dir / "backtest_lq_reclaim_multi_symbol.csv", index=False)
            print(
                "\nRead this as: look for edge staying clearly positive (and n_setups reasonably "
                "sized) across MOST symbols, for the same parameter combo. A combo that only works "
                "on one or two symbols is probably fitted to that instrument's specific behavior, "
                "not a general pattern."
            )
        else:
            print("No results - all symbol fetches failed or produced no setups.")


if __name__ == "__main__":
    asyncio.run(main())