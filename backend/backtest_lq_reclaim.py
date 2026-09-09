"""
Backtest: LQ-low reclaim after a bullish BOS - ATR-normalized version.

Same setup as before:
  1. A liquidity LOW forms (direction == "bearish").
  2. Price rallies and prints a bullish BOS.
  3. Price pulls back toward the LQ low's level, within some threshold
     distance of it.
  4. Shortly after, price pops back up by some threshold amount.

The difference from the earlier fixed-percentage version: closeness and
pop-up thresholds are now expressed as MULTIPLES OF ATR (average true
range) instead of a fixed % of price. A "2x ATR" move is an equally hard
target on a calm instrument (R_25) as on a volatile one (R_100) - a fixed
percentage is not, which is why the earlier multi-symbol check made
low-volatility symbols look like the pattern "failed" there when really the
targets were just mismatched to that instrument's typical movement.

Three checks (toggle with DO_* flags):
  1. Full-period sweep on SYMBOL.
  2. Time-split check - same sweep on first vs second half of SYMBOL's
     history, to catch a pattern that only worked in one stretch of time.
  3. Multi-symbol check - the tracked candidate ATR-multiple combos tested
     across other symbols, now on equal footing volatility-wise.

Run this locally or as a one-off job - needs live network access to Deriv.
"""
import asyncio
import itertools
import sys
from pathlib import Path

import pandas as pd
import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

sys.path.insert(0, str(Path(__file__).resolve().parent))

import deriv_client
import smc_engine

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

SYMBOL = "R_100"
TIMEFRAME_SECONDS = 900
HISTORY_COUNT = 20000
SWING_LENGTH = 10
RANGE_PERCENT = 0.01

ATR_PERIOD = 14  # standard ATR lookback, in candles

# Closeness/popup are now multiples of the symbol's own typical ATR, not a
# fixed % of price. e.g. closeness=1.0 means "within one average candle's
# worth of range" of the LQ level - same relative difficulty on any symbol.
CLOSENESS_ATR_MULTIPLES = [0.5, 1.0, 1.5, 2.0]
LOOKAHEAD_CANDLES = [4, 8, 16]
POPUP_ATR_MULTIPLES = [1.0, 2.0, 3.0, 4.0]

MAX_CANDLES_LQ_TO_BOS = 200
MAX_CANDLES_BOS_TO_RETURN = 200

MIN_SETUPS = 30

DO_FULL_SWEEP = True
DO_TIME_SPLIT_CHECK = True
DO_MULTI_SYMBOL_CHECK = True

# Fill in after looking at the full-period sweep results - format:
# (closeness_atr_mult, lookahead, popup_atr_mult). These are placeholders -
# replace with whatever tops the full sweep's edge column.
CANDIDATE_PARAMS_TO_TRACK = [
    (1.0, 16, 3.0),
    (1.5, 16, 3.0),
    (1.0, 16, 2.0),
]

SYMBOLS_FOR_MULTI_CHECK = ["R_75", "R_50", "R_25", "CRASH500", "BOOM500"]
MULTI_SYMBOL_HISTORY_COUNT = 10000


# ---------------------------------------------------------------------------
# ATR
# ---------------------------------------------------------------------------

def compute_atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low).abs(),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(window=period, min_periods=period).mean()


def typical_atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> float:
    """Single representative ATR value for this dataframe (median of the ATR
    series), used to convert ATR-multiple thresholds into absolute price
    distances for this symbol/period."""
    atr = compute_atr(df, period).dropna()
    if atr.empty:
        raise ValueError("not enough candles to compute ATR - need at least ATR_PERIOD+1")
    return float(atr.median())


# ---------------------------------------------------------------------------
# Baseline (random-candle) comparison, in absolute price terms
# ---------------------------------------------------------------------------

def compute_baseline_rates_abs(df: pd.DataFrame, lookaheads: list[int], popup_abs_values: list[float]) -> dict:
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    n = len(df)

    baseline = {}
    for lookahead in set(lookaheads):
        if n <= lookahead:
            continue
        windows = sliding_window_view(highs[1:], lookahead)
        forward_max = windows.max(axis=1)
        valid_lows = lows[: len(forward_max)]
        move_up_abs = forward_max - valid_lows
        for popup_abs in set(popup_abs_values):
            baseline[(lookahead, popup_abs)] = float((move_up_abs >= popup_abs).mean())
    return baseline


# ---------------------------------------------------------------------------
# Setup detection (distances kept in absolute price units now)
# ---------------------------------------------------------------------------

def find_candidate_setups(df: pd.DataFrame, overlays: dict) -> list[dict]:
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
                dist_abs = abs(low - lq["level"])  # absolute price distance now, not a fraction
                candidates.append({
                    "lq_time": lq["time"],
                    "lq_level": lq["level"],
                    "bos_time": bos["time"],
                    "bos_level": bos["level"],
                    "return_idx": ret_idx,
                    "return_time": int(df["time"].iloc[ret_idx]),
                    "return_low": low,
                    "dist_abs": dist_abs,
                })
    return candidates


def evaluate_outcome_abs(df: pd.DataFrame, candidate: dict, closeness_abs: float,
                          lookahead: int, popup_abs: float) -> dict | None:
    if candidate["dist_abs"] > closeness_abs:
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
    move_up_abs = best_high - entry_price
    hit_popup = move_up_abs >= popup_abs

    worst_low = window["low"].min()
    drawdown_abs = entry_price - worst_low

    return {
        "hit_popup": hit_popup,
        "move_up_pct": move_up_abs / entry_price * 100,
        "drawdown_pct": drawdown_abs / entry_price * 100,
    }


# ---------------------------------------------------------------------------
# Reusable sweep
# ---------------------------------------------------------------------------

def run_sweep_atr(df: pd.DataFrame, closeness_multiples=None, lookahead_candles=None,
                   popup_multiples=None) -> pd.DataFrame:
    """
    Runs the (closeness, lookahead, popup) grid search, with closeness and
    popup expressed as multiples of this dataframe's own typical ATR.
    Returns a results table including the resolved ATR value used, so it's
    clear what absolute price distance each multiple corresponds to for
    this particular symbol/period.
    """
    closeness_multiples = closeness_multiples or CLOSENESS_ATR_MULTIPLES
    lookahead_candles = lookahead_candles or LOOKAHEAD_CANDLES
    popup_multiples = popup_multiples or POPUP_ATR_MULTIPLES

    atr_value = typical_atr(df)

    overlays = smc_engine.compute_all(df, swing_length=SWING_LENGTH, range_percent=RANGE_PERCENT)
    candidates = find_candidate_setups(df, overlays)

    popup_abs_values = [m * atr_value for m in popup_multiples]
    baseline_rates = compute_baseline_rates_abs(df, lookahead_candles, popup_abs_values)

    rows = []
    for closeness_mult, lookahead, popup_mult in itertools.product(
        closeness_multiples, lookahead_candles, popup_multiples
    ):
        closeness_abs = closeness_mult * atr_value
        popup_abs = popup_mult * atr_value

        outcomes = []
        seen_lq_bos_pairs = set()
        for c in candidates:
            pair_key = (c["lq_time"], c["bos_time"])
            if pair_key in seen_lq_bos_pairs:
                continue
            result = evaluate_outcome_abs(df, c, closeness_abs, lookahead, popup_abs)
            if result is None:
                continue
            seen_lq_bos_pairs.add(pair_key)
            outcomes.append(result)

        if not outcomes:
            continue

        n = len(outcomes)
        hit_rate = sum(o["hit_popup"] for o in outcomes) / n
        avg_move_up = sum(o["move_up_pct"] for o in outcomes) / n
        avg_drawdown = sum(o["drawdown_pct"] for o in outcomes) / n
        baseline_hit_rate = baseline_rates.get((lookahead, popup_abs), float("nan"))

        rows.append({
            "closeness_atr_mult": closeness_mult,
            "lookahead_candles": lookahead,
            "popup_atr_mult": popup_mult,
            "atr_value": round(atr_value, 6),
            "n_setups": n,
            "hit_rate": round(hit_rate, 3),
            "baseline_hit_rate": round(baseline_hit_rate, 3),
            "edge": round(hit_rate - baseline_hit_rate, 3),
            "avg_move_up_pct": round(avg_move_up, 3),
            "avg_drawdown_pct": round(avg_drawdown, 3),
        })

    return pd.DataFrame(rows)


def print_sweep_results(results: pd.DataFrame, label: str, min_setups: int = MIN_SETUPS):
    pd.set_option("display.width", 170)
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

    if DO_FULL_SWEEP:
        print(f"\n########## FULL-PERIOD SWEEP (ATR-normalized): {SYMBOL} ##########")
        results_full = run_sweep_atr(df_full)
        print_sweep_results(results_full, f"{SYMBOL} FULL PERIOD ({len(df_full)} candles)")
        results_full.to_csv(out_dir / f"backtest_lq_reclaim_atr_{SYMBOL}_full.csv", index=False)

    if DO_TIME_SPLIT_CHECK:
        print(f"\n########## TIME-SPLIT CHECK (ATR-normalized): {SYMBOL} ##########")
        mid = len(df_full) // 2
        df_first = df_full.iloc[:mid].reset_index(drop=True)
        df_second = df_full.iloc[mid:].reset_index(drop=True)

        results_first = run_sweep_atr(df_first)
        results_second = run_sweep_atr(df_second)

        print_sweep_results(
            results_first,
            f"{SYMBOL} FIRST HALF ({df_first['time'].iloc[0]} -> {df_first['time'].iloc[-1]})",
        )
        print_sweep_results(
            results_second,
            f"{SYMBOL} SECOND HALF ({df_second['time'].iloc[0]} -> {df_second['time'].iloc[-1]})",
        )

        print(f"\n=== [{SYMBOL}] Candidate params (ATR multiples): first half vs second half ===")
        compare_rows = []
        for closeness_mult, lookahead, popup_mult in CANDIDATE_PARAMS_TO_TRACK:
            row1 = results_first[
                (results_first.closeness_atr_mult == closeness_mult)
                & (results_first.lookahead_candles == lookahead)
                & (results_first.popup_atr_mult == popup_mult)
            ]
            row2 = results_second[
                (results_second.closeness_atr_mult == closeness_mult)
                & (results_second.lookahead_candles == lookahead)
                & (results_second.popup_atr_mult == popup_mult)
            ]
            compare_rows.append({
                "closeness_atr_mult": closeness_mult,
                "lookahead": lookahead,
                "popup_atr_mult": popup_mult,
                "first_half_n": row1["n_setups"].iloc[0] if not row1.empty else 0,
                "first_half_edge": row1["edge"].iloc[0] if not row1.empty else None,
                "second_half_n": row2["n_setups"].iloc[0] if not row2.empty else 0,
                "second_half_edge": row2["edge"].iloc[0] if not row2.empty else None,
            })
        compare_df = pd.DataFrame(compare_rows)
        print(compare_df.to_string(index=False))
        compare_df.to_csv(out_dir / f"backtest_lq_reclaim_atr_{SYMBOL}_split_comparison.csv", index=False)

    if DO_MULTI_SYMBOL_CHECK:
        print(f"\n########## MULTI-SYMBOL CHECK (ATR-normalized) ##########")
        closeness_list = sorted({c for c, _, _ in CANDIDATE_PARAMS_TO_TRACK})
        lookahead_list = sorted({l for _, l, _ in CANDIDATE_PARAMS_TO_TRACK})
        popup_list = sorted({p for _, _, p in CANDIDATE_PARAMS_TO_TRACK})

        multi_rows = []
        for sym in SYMBOLS_FOR_MULTI_CHECK:
            try:
                sym_df = await fetch_df(sym, MULTI_SYMBOL_HISTORY_COUNT)
                sym_results = run_sweep_atr(
                    sym_df,
                    closeness_multiples=closeness_list,
                    lookahead_candles=lookahead_list,
                    popup_multiples=popup_list,
                )
            except Exception as exc:
                print(f"  {sym}: failed ({exc}) - skipping")
                continue

            for closeness_mult, lookahead, popup_mult in CANDIDATE_PARAMS_TO_TRACK:
                row = sym_results[
                    (sym_results.closeness_atr_mult == closeness_mult)
                    & (sym_results.lookahead_candles == lookahead)
                    & (sym_results.popup_atr_mult == popup_mult)
                ] if not sym_results.empty else pd.DataFrame()
                if row.empty:
                    multi_rows.append({
                        "symbol": sym, "closeness_atr_mult": closeness_mult,
                        "lookahead": lookahead, "popup_atr_mult": popup_mult,
                        "n_setups": 0, "hit_rate": None, "baseline_hit_rate": None, "edge": None,
                    })
                else:
                    r = row.iloc[0]
                    multi_rows.append({
                        "symbol": sym, "closeness_atr_mult": closeness_mult,
                        "lookahead": lookahead, "popup_atr_mult": popup_mult,
                        "n_setups": r["n_setups"], "hit_rate": r["hit_rate"],
                        "baseline_hit_rate": r["baseline_hit_rate"], "edge": r["edge"],
                    })

        multi_df = pd.DataFrame(multi_rows)
        print(f"\n=== Candidate params (ATR-normalized) tested across other symbols ===")
        if not multi_df.empty:
            print(multi_df.to_string(index=False))
            multi_df.to_csv(out_dir / "backtest_lq_reclaim_atr_multi_symbol.csv", index=False)
        else:
            print("No results - all symbol fetches failed or produced no setups.")


if __name__ == "__main__":
    asyncio.run(main())
