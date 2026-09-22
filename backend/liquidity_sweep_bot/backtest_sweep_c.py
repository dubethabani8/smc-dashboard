"""
Backtest: LQ-low swept, then a deeper low forms (C), then price pops back
up -- the pattern from the six original scenarios. Same scaffolding as
backend/backtest_lq_reclaim.py on lq-reclaim-bounce (ATR normalization,
baseline comparison, time-split check, multi-symbol check), but a
different setup-detection: instead of [BOS -> pullback near the original
LQ level], this looks for [LQ low swept -> a deeper low forms -> price
pops back up], via sweep_c_setup.find_sweep_c_setups().

Simplification (see sweep_c_setup.py): C here is just the next confirmed
swing low after the sweep, not specifically tied to an old prior level
from before A. Worth revisiting once this baseline is validated.

Entry point is C's swing-confirmation index (C's own index + SWING_LENGTH),
not C's own index -- matches the look-ahead-bias fix already made on
lq-reclaim-bounce (a328b5a): you can't know in real time that a candle is
a confirmed swing low until SWING_LENGTH candles later.

Run this locally or as a one-off job -- needs live network access to
Deriv (this sandbox can't reach it; confirmed earlier).
"""
import asyncio
import sys
from pathlib import Path

import pandas as pd
import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from smartmoneyconcepts import smc

sys.path.insert(0, str(Path(__file__).resolve().parent))

import deriv_client
from sweep_c_setup import find_sweep_c_setups

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

SYMBOL = "BOOM900"  # matches your original scenarios -- R_100 was an inherited default, never actually the right market to test
TIMEFRAME_SECONDS = 900
HISTORY_COUNT = 20000
SWING_LENGTH = 10
RANGE_PERCENT = 0.01  # matches your tool's Liq % -- liquidity clustering threshold

ATR_PERIOD = 14

POPUP_ATR_MULTIPLES = [1.0, 2.0, 3.0, 4.0]
LOOKAHEAD_CANDLES = [4, 8, 16]

MIN_SETUPS = 30

CONFIRMATION_WINDOW = 20  # candles to wait, after C is confirmed, for a close back above C

DO_FULL_SWEEP = True
DO_TIME_SPLIT_CHECK = False
DO_MULTI_SYMBOL_CHECK = False

# Fill in after looking at the full-period sweep results, same as
# backtest_lq_reclaim.py -- these are placeholders.
CANDIDATE_PARAMS_TO_TRACK = [
    (16, 3.0),
    (16, 2.0),
    (8, 2.0),
]

SYMBOLS_FOR_MULTI_CHECK = ["R_75", "R_50", "R_25", "CRASH500", "BOOM500"]
MULTI_SYMBOL_HISTORY_COUNT = 10000


# ---------------------------------------------------------------------------
# smc_engine-equivalent extraction (swings + liquidity only -- this
# backtest doesn't need BOS/CHoCH/FVG/order blocks, unlike the live app)
# ---------------------------------------------------------------------------

def build_dataframe(candles: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(candles)
    df = df.rename(columns={"epoch": "time"})
    df["time"] = df["time"].astype(int)
    df = df.drop_duplicates(subset="time").sort_values("time").reset_index(drop=True)
    for col in ("open", "high", "low", "close"):
        df[col] = df[col].astype(float)
    df["volume"] = (df["high"] - df["low"]).abs() + (df["close"] - df["open"]).abs()
    df.index = pd.to_datetime(df["time"], unit="s")
    return df


def extract_swings_and_liquidity(df: pd.DataFrame, swing_length: int, range_percent: float):
    swings_df = smc.swing_highs_lows(df, swing_length=swing_length)
    liquidity_df = smc.liquidity(df, swings_df, range_percent=range_percent)

    swings = []
    for i, row in swings_df.dropna(subset=["HighLow"]).iterrows():
        swings.append({"time": int(df["time"].iloc[i]),
                        "type": "high" if row["HighLow"] == 1 else "low",
                        "level": float(row["Level"])})

    liquidity = []
    for i, row in liquidity_df.dropna(subset=["Liquidity"]).iterrows():
        raw_swept = row.get("Swept")
        if pd.notna(raw_swept) and int(raw_swept) == 0:
            raw_swept = None
        swept_time = int(df["time"].iloc[int(raw_swept)]) if raw_swept is not None else None
        liquidity.append({"time": int(df["time"].iloc[i]),
                           "direction": "bullish" if row["Liquidity"] == 1 else "bearish",
                           "level": float(row["Level"]), "swept_time": swept_time})

    return swings, liquidity


# ---------------------------------------------------------------------------
# ATR (same as backtest_lq_reclaim.py)
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
# Setup detection + outcome evaluation
# ---------------------------------------------------------------------------

C_MODE = "nearest_prior_before_a"  # the version we're testing now, per this round's clarification


def find_candidate_setups(df: pd.DataFrame, swings: list[dict], liquidity: list[dict]) -> list[dict]:
    """Wraps find_sweep_c_setups, then finds the actual designed entry:
    a candle that wicks down to C's level and closes back above it.

    The search-start point depends on C_MODE:
      - "next_after_sweep": C is a brand-new low, so nothing can happen
        until C's own index + SWING_LENGTH (the earliest point you could
        know in real time it's a confirmed swing low).
      - "nearest_prior_before_a": C is an OLD low that was already
        confirmed well before A even formed, so there's no confirmation
        lag to wait out -- the search starts right at B (the sweep),
        since that's the earliest point this setup exists at all.

    Scans up to CONFIRMATION_WINDOW candles from the start point for the
    first one satisfying touch-then-close-above. No qualifying candle
    within the window means no trade for that setup."""
    time_to_idx = {t: i for i, t in enumerate(df["time"])}
    raw_setups = find_sweep_c_setups(swings, liquidity, direction="bearish", c_mode=C_MODE)

    candidates = []
    for s in raw_setups:
        c_idx = time_to_idx.get(s["c_time"])
        b_idx = time_to_idx.get(s["b_time"])
        if c_idx is None or b_idx is None:
            continue

        if C_MODE == "next_after_sweep":
            search_start = c_idx + SWING_LENGTH
        else:
            search_start = b_idx

        if search_start >= len(df):
            continue

        c_level = s["c_level"]
        entry_idx = None
        window_end = min(search_start + CONFIRMATION_WINDOW, len(df) - 1)
        for i in range(search_start, window_end + 1):
            if df["low"].iloc[i] <= c_level and df["close"].iloc[i] > c_level:
                entry_idx = i
                break
        if entry_idx is None:
            continue

        candidates.append({
            "a_time": s["a_time"], "a_level": s["a_level"],
            "b_time": s["b_time"],
            "c_time": s["c_time"], "c_level": c_level,
            "entry_idx": entry_idx,
            "entry_price": float(df["close"].iloc[entry_idx]),
        })
    return candidates


def evaluate_outcome_abs(df: pd.DataFrame, candidate: dict, lookahead: int, popup_abs: float) -> dict | None:
    entry_idx = candidate["entry_idx"]
    entry_price = candidate["entry_price"]
    end_idx = min(entry_idx + lookahead, len(df) - 1)
    if end_idx <= entry_idx:
        return None
    window = df.iloc[entry_idx + 1: end_idx + 1]
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

def run_sweep_atr(df: pd.DataFrame, lookahead_candles=None, popup_multiples=None) -> pd.DataFrame:
    lookahead_candles = lookahead_candles or LOOKAHEAD_CANDLES
    popup_multiples = popup_multiples or POPUP_ATR_MULTIPLES

    atr_value = typical_atr(df)
    swings, liquidity = extract_swings_and_liquidity(df, SWING_LENGTH, RANGE_PERCENT)
    candidates = find_candidate_setups(df, swings, liquidity)

    popup_abs_values = [m * atr_value for m in popup_multiples]
    baseline_rates = compute_baseline_rates_abs(df, lookahead_candles, popup_abs_values)

    rows = []
    for lookahead in lookahead_candles:
        for popup_mult in popup_multiples:
            popup_abs = popup_mult * atr_value
            outcomes = [o for c in candidates if (o := evaluate_outcome_abs(df, c, lookahead, popup_abs)) is not None]

            if not outcomes:
                continue

            n = len(outcomes)
            hit_rate = sum(o["hit_popup"] for o in outcomes) / n
            avg_move_up = sum(o["move_up_pct"] for o in outcomes) / n
            avg_drawdown = sum(o["drawdown_pct"] for o in outcomes) / n
            baseline_hit_rate = baseline_rates.get((lookahead, popup_abs), float("nan"))

            rows.append({
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
        print(f"None reached {min_setups}+ setups - need more history or a longer lookahead.")

    if not unreliable.empty:
        print(f"\n=== [{label}] Below the {min_setups}-setup trust threshold (reference only) ===")
        print(unreliable.sort_values(["edge", "n_setups"], ascending=[False, False]).to_string(index=False))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def fetch_df(symbol: str, history_count: int) -> pd.DataFrame:
    print(f"Fetching up to {history_count} candles for {symbol} @ {TIMEFRAME_SECONDS}s (paginated) ...")
    candles = await deriv_client.fetch_candle_history_paginated(symbol, TIMEFRAME_SECONDS, history_count)
    df = build_dataframe(candles)
    print(f"Got {len(df)} candles for {symbol}: {df['time'].iloc[0]} -> {df['time'].iloc[-1]}")
    return df


async def main():
    print(f"C_MODE = {C_MODE!r}")
    out_dir = Path(__file__).resolve().parent

    df_full = None
    if DO_FULL_SWEEP or DO_TIME_SPLIT_CHECK:
        df_full = await fetch_df(SYMBOL, HISTORY_COUNT)

    if DO_FULL_SWEEP:
        print(f"\n########## FULL-PERIOD SWEEP: {SYMBOL} ##########")
        results_full = run_sweep_atr(df_full)
        print_sweep_results(results_full, f"{SYMBOL} FULL PERIOD ({len(df_full)} candles)")
        results_full.to_csv(out_dir / f"backtest_sweep_c_{SYMBOL}_full.csv", index=False)

    if DO_TIME_SPLIT_CHECK:
        print(f"\n########## TIME-SPLIT CHECK: {SYMBOL} ##########")
        mid = len(df_full) // 2
        df_first = df_full.iloc[:mid].reset_index(drop=True)
        df_second = df_full.iloc[mid:].reset_index(drop=True)

        results_first = run_sweep_atr(df_first)
        results_second = run_sweep_atr(df_second)

        print_sweep_results(results_first, f"{SYMBOL} FIRST HALF ({df_first['time'].iloc[0]} -> {df_first['time'].iloc[-1]})")
        print_sweep_results(results_second, f"{SYMBOL} SECOND HALF ({df_second['time'].iloc[0]} -> {df_second['time'].iloc[-1]})")

        print(f"\n=== [{SYMBOL}] Candidate params: first half vs second half ===")
        compare_rows = []
        for lookahead, popup_mult in CANDIDATE_PARAMS_TO_TRACK:
            row1 = results_first[(results_first.lookahead_candles == lookahead) & (results_first.popup_atr_mult == popup_mult)]
            row2 = results_second[(results_second.lookahead_candles == lookahead) & (results_second.popup_atr_mult == popup_mult)]
            compare_rows.append({
                "lookahead": lookahead, "popup_atr_mult": popup_mult,
                "first_half_n": row1["n_setups"].iloc[0] if not row1.empty else 0,
                "first_half_edge": row1["edge"].iloc[0] if not row1.empty else None,
                "second_half_n": row2["n_setups"].iloc[0] if not row2.empty else 0,
                "second_half_edge": row2["edge"].iloc[0] if not row2.empty else None,
            })
        compare_df = pd.DataFrame(compare_rows)
        print(compare_df.to_string(index=False))
        compare_df.to_csv(out_dir / f"backtest_sweep_c_{SYMBOL}_split_comparison.csv", index=False)

    if DO_MULTI_SYMBOL_CHECK:
        print(f"\n########## MULTI-SYMBOL CHECK ##########")
        lookahead_list = sorted({l for l, _ in CANDIDATE_PARAMS_TO_TRACK})
        popup_list = sorted({p for _, p in CANDIDATE_PARAMS_TO_TRACK})

        multi_rows = []
        for sym in SYMBOLS_FOR_MULTI_CHECK:
            try:
                sym_df = await fetch_df(sym, MULTI_SYMBOL_HISTORY_COUNT)
                sym_results = run_sweep_atr(sym_df, lookahead_candles=lookahead_list, popup_multiples=popup_list)
            except Exception as exc:
                print(f"  {sym}: failed ({exc}) - skipping")
                continue

            for lookahead, popup_mult in CANDIDATE_PARAMS_TO_TRACK:
                row = sym_results[(sym_results.lookahead_candles == lookahead) & (sym_results.popup_atr_mult == popup_mult)] if not sym_results.empty else pd.DataFrame()
                if row.empty:
                    multi_rows.append({"symbol": sym, "lookahead": lookahead, "popup_atr_mult": popup_mult,
                                        "n_setups": 0, "hit_rate": None, "baseline_hit_rate": None, "edge": None})
                else:
                    r = row.iloc[0]
                    multi_rows.append({"symbol": sym, "lookahead": lookahead, "popup_atr_mult": popup_mult,
                                        "n_setups": r["n_setups"], "hit_rate": r["hit_rate"],
                                        "baseline_hit_rate": r["baseline_hit_rate"], "edge": r["edge"]})

        multi_df = pd.DataFrame(multi_rows)
        print(f"\n=== Candidate params tested across other symbols ===")
        if not multi_df.empty:
            print(multi_df.to_string(index=False))
            multi_df.to_csv(out_dir / "backtest_sweep_c_multi_symbol.csv", index=False)
        else:
            print("No results - all symbol fetches failed or produced no setups.")


if __name__ == "__main__":
    asyncio.run(main())
