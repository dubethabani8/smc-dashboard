"""
Independence audit for trade_log_lq_reclaim.csv.

The chart-sample review surfaced multiple trades sharing the exact same
lq_level (to 4+ decimal places) - meaning they're different BOS breakouts
retesting the SAME underlying liquidity level, not fully independent
events. If a support zone tends to hold (or fail) for structural reasons,
repeat visits to it are correlated, not fresh draws - and counting each as
an equally-weighted independent trade in an aggregate stat can inflate the
apparent edge.

This script:
  1. Groups trades by (symbol, lq_time) - the precise identifier for "same
     underlying liquidity level", not just matching price (which could in
     principle collide by coincidence, though in practice an exact price
     match to several decimals is effectively certain to be the same
     record).
  2. Reports the distribution of group sizes - how many trades are genuinely
     one-off vs. how many levels got tested multiple times.
  3. Recomputes the full performance summary three ways: ALL trades (as
     before), FIRST-per-level only (the conservative, fully-independent
     view - what you'd get if the live system only ever takes the first
     signal off a level), and a bootstrap-style RANDOM-per-level check
     (repeated sampling of one trade per group) to see how much the
     estimate wobbles.
  4. Runs a same-sign correlation check: do trades sharing a level agree
     (both win or both lose) more often than chance would predict from the
     overall win rate? That's a direct statistical test for non-independence,
     not just an assumption.
  5. Reports the time gap between repeat trades on the same level, to see
     whether they're near-simultaneous retests or well-separated in time.

Run this after trade_sim_lq_reclaim.py has produced trade_log_lq_reclaim.csv.
No network access needed - this only reads the existing CSV.
"""
import itertools
from pathlib import Path

import numpy as np
import pandas as pd

TRADE_LOG_PATH = Path(__file__).resolve().parent / "trade_log_lq_reclaim.csv"
RANDOM_SEED = 42
N_RANDOM_SAMPLES = 200  # how many random one-per-group draws to run for the bootstrap check


def summarize(trades: pd.DataFrame, label: str) -> dict | None:
    if trades.empty:
        print(f"\n[{label}] No trades.")
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
    max_drawdown = (equity - running_max).min()

    print(f"\n=== [{label}] ({n} trades) ===")
    print(f"  win_rate:         {win_rate:.3f}")
    print(f"  avg_return_pct:   {avg_return:.4f}")
    print(f"  profit_factor:    {profit_factor:.3f}")
    print(f"  total_return_pct: {equity.iloc[-1]:.3f}")
    print(f"  max_drawdown_pct: {max_drawdown:.3f}")

    return {
        "label": label, "n_trades": n, "win_rate": round(win_rate, 3),
        "avg_return_pct": round(avg_return, 4), "profit_factor": round(profit_factor, 3),
        "total_return_pct": round(equity.iloc[-1], 3), "max_drawdown_pct": round(max_drawdown, 3),
    }


def main():
    if not TRADE_LOG_PATH.exists():
        print(f"Trade log not found at {TRADE_LOG_PATH} - run trade_sim_lq_reclaim.py first.")
        return

    trades = pd.read_csv(TRADE_LOG_PATH)
    trades["group_key"] = trades["symbol"] + "|" + trades["lq_time"].astype(str)
    trades = trades.sort_values(["group_key", "entry_time"]).reset_index(drop=True)

    # ------------------------------------------------------------------
    # 1. Group size distribution
    # ------------------------------------------------------------------
    group_sizes = trades.groupby("group_key").size()
    n_groups = len(group_sizes)
    n_trades = len(trades)

    print("########## GROUP SIZE DISTRIBUTION ##########")
    print(f"Total trades: {n_trades}")
    print(f"Unique (symbol, lq_time) levels: {n_groups}")
    print(f"Average trades per level: {n_trades / n_groups:.2f}")
    print("\nDistribution (n trades on a level -> how many levels had exactly that many):")
    size_dist = group_sizes.value_counts().sort_index()
    for size, count in size_dist.items():
        pct_of_trades = (size * count) / n_trades * 100
        print(f"  {size} trade(s) on the level: {count} level(s)  "
              f"({size * count} total trades, {pct_of_trades:.1f}% of all trades)")

    repeated_levels = group_sizes[group_sizes > 1]
    pct_trades_on_repeated = trades[trades["group_key"].isin(repeated_levels.index)].shape[0] / n_trades * 100
    print(f"\n{len(repeated_levels)} of {n_groups} levels were tested more than once, "
          f"accounting for {pct_trades_on_repeated:.1f}% of all trades in the dataset.")

    # ------------------------------------------------------------------
    # 2. Three versions of the performance summary
    # ------------------------------------------------------------------
    print("\n########## PERFORMANCE: ALL TRADES vs FIRST-PER-LEVEL ##########")
    summarize(trades, "ALL TRADES (original, includes repeats)")

    first_per_level = trades.groupby("group_key").first().reset_index()
    summarize(first_per_level, "FIRST TRADE PER LEVEL ONLY (conservative, independent)")

    # bootstrap: randomly pick one trade per group, repeated many times, to
    # see how much the estimate varies depending on WHICH repeat gets kept
    rng = np.random.default_rng(RANDOM_SEED)
    bootstrap_results = []
    groups = trades.groupby("group_key")
    for _ in range(N_RANDOM_SAMPLES):
        sampled_rows = groups.apply(lambda g: g.sample(1, random_state=rng.integers(0, 1_000_000)))
        sampled = sampled_rows.reset_index(drop=True)
        n = len(sampled)
        wins = (sampled["return_pct"] > 0).sum()
        win_rate = wins / n
        gross_win = sampled.loc[sampled["return_pct"] > 0, "return_pct"].sum()
        gross_loss = abs(sampled.loc[sampled["return_pct"] <= 0, "return_pct"].sum())
        pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
        bootstrap_results.append({"win_rate": win_rate, "profit_factor": pf})

    bs_df = pd.DataFrame(bootstrap_results)
    print(f"\n=== RANDOM-ONE-PER-LEVEL BOOTSTRAP ({N_RANDOM_SAMPLES} resamples) ===")
    print(f"  win_rate:      mean={bs_df['win_rate'].mean():.3f}  "
          f"std={bs_df['win_rate'].std():.3f}  "
          f"[{bs_df['win_rate'].quantile(0.1):.3f}, {bs_df['win_rate'].quantile(0.9):.3f}] (10th-90th pct)")
    finite_pf = bs_df.loc[np.isfinite(bs_df["profit_factor"]), "profit_factor"]
    if not finite_pf.empty:
        print(f"  profit_factor: mean={finite_pf.mean():.3f}  "
              f"std={finite_pf.std():.3f}  "
              f"[{finite_pf.quantile(0.1):.3f}, {finite_pf.quantile(0.9):.3f}] (10th-90th pct)")

    # ------------------------------------------------------------------
    # 3. Same-sign correlation check within repeated levels
    # ------------------------------------------------------------------
    print("\n########## SAME-SIGN CORRELATION CHECK ##########")
    overall_win_rate = (trades["return_pct"] > 0).mean()
    expected_agreement = overall_win_rate ** 2 + (1 - overall_win_rate) ** 2

    agree_count = 0
    pair_count = 0
    for _, group in groups:
        if len(group) < 2:
            continue
        signs = (group["return_pct"] > 0).tolist()
        for a, b in itertools.combinations(signs, 2):
            pair_count += 1
            if a == b:
                agree_count += 1

    if pair_count > 0:
        actual_agreement = agree_count / pair_count
        print(f"  Pairs of trades sharing a level: {pair_count}")
        print(f"  Actual same-outcome agreement rate:   {actual_agreement:.3f}")
        print(f"  Expected agreement if independent:    {expected_agreement:.3f} "
              f"(from overall win rate {overall_win_rate:.3f})")
        diff = actual_agreement - expected_agreement
        if diff > 0.05:
            print(f"  -> Agreement is {diff:.3f} HIGHER than chance would predict. "
                  f"This is evidence trades on the same level are correlated, not independent.")
        elif diff < -0.05:
            print(f"  -> Agreement is {diff:.3f} LOWER than chance - unusual, worth a closer look.")
        else:
            print(f"  -> Agreement is close to what independence would predict "
                  f"(difference: {diff:+.3f}) - repeat trades don't look strongly correlated.")
    else:
        print("  No levels had more than one trade - nothing to test.")

    # ------------------------------------------------------------------
    # 4. Time gap between repeat trades on the same level
    # ------------------------------------------------------------------
    print("\n########## TIME GAP BETWEEN REPEAT TRADES ON THE SAME LEVEL ##########")
    gaps_days = []
    for _, group in groups:
        if len(group) < 2:
            continue
        times = sorted(group["entry_time"].tolist())
        for a, b in zip(times, times[1:]):
            gaps_days.append((b - a) / 86400)

    if gaps_days:
        gaps = pd.Series(gaps_days)
        print(f"  Gaps between consecutive repeat trades on the same level (days):")
        print(f"    min={gaps.min():.2f}  median={gaps.median():.2f}  "
              f"mean={gaps.mean():.2f}  max={gaps.max():.2f}")
        close_together = (gaps < 1).mean() * 100
        print(f"  {close_together:.1f}% of repeat visits happened within 1 day of the prior one "
              f"on the same level.")
    else:
        print("  No repeat trades to measure gaps for.")

    print(
        "\nHow to read all of this: if FIRST-PER-LEVEL performance stays reasonably close to ALL-TRADES "
        "performance, and the bootstrap range doesn't include very different (e.g. much lower or "
        "negative) profit factors, the edge is probably real and not just repeat-counting the same "
        "few levels. If FIRST-PER-LEVEL is meaningfully worse, or the bootstrap range is wide and "
        "sometimes crosses profit_factor < 1, treat the ALL-TRADES numbers as optimistic and lean on "
        "the more conservative estimate before risking anything in paper trading."
    )


if __name__ == "__main__":
    main()
