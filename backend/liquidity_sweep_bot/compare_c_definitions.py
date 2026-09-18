"""
Compares two rules for defining "C" -- the deeper bounce level after a
liquidity sweep -- against historical candle data.

  Rule "inclusive": nearest prior swing low below B's sweep price. Any
                     swing low qualifies, tagged or not.
  Rule "strict":     same, but the swing low must also belong to a
                     tagged liquidity cluster (grouped with at least one
                     other nearby swing low within --liq-pct of it).

This is a first-pass reimplementation of the swing/liquidity detection,
built to approximate the Swing H/L and Liquidity overlays on your own
tool (N-candle fractal swings, percentage-based clustering) -- not a
byte-for-byte port of your actual code, since I couldn't reach your repo
(GitHub rate-limited the unauthenticated API call). Swap in your real
detection functions in place of find_swing_lows/cluster_liquidity and
everything downstream stays the same.

Entry trigger implemented here: price must wick down to the C level and
then CLOSE back above it within --lookahead candles to count as a
confirmed bounce (this is the "one candle of confirmation" rule from
our last round -- not a blind touch-entry).

Stop-loss and TP1/TP2 sizing aren't decided yet, so this script does NOT
simulate a full trade outcome or R-multiple. It only measures: how often
does each rule produce a candidate, and how often does price actually
bounce there vs. blow through vs. never even reach it. That's the
question this round is about -- outcome sizing is the next step.

Usage:
    python compare_c_definitions.py --csv candles.csv --swing 10 --liq-pct 1.0

CSV must have columns: epoch,open,high,low,close
"""
import argparse
import csv
from dataclasses import dataclass


@dataclass
class Candle:
    epoch: int
    open: float
    high: float
    low: float
    close: float


def load_candles(path):
    candles = []
    with open(path) as f:
        for row in csv.DictReader(f):
            candles.append(Candle(
                epoch=int(float(row["epoch"])),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
            ))
    candles.sort(key=lambda c: c.epoch)
    return candles


def find_swing_lows(candles, swing):
    """Index i is a swing low if its low is the minimum among the `swing`
    candles on either side of it -- a simple N-candle fractal, matching
    the 'Swing' sensitivity parameter your tool exposes."""
    lows = []
    for i in range(swing, len(candles) - swing):
        window = candles[i - swing:i + swing + 1]
        if candles[i].low == min(c.low for c in window):
            lows.append(i)
    return lows


def cluster_liquidity(candles, swing_low_indices, liq_pct):
    """Groups swing lows within liq_pct percent of each other into
    clusters. A cluster with 2+ members counts as a tagged 'LQ' zone;
    a lone swing low with nothing nearby is not tagged."""
    points = sorted(swing_low_indices, key=lambda i: candles[i].low)
    if not points:
        return set()
    clusters, current = [], [points[0]]
    for i in points[1:]:
        ref_price = candles[current[0]].low
        if abs(candles[i].low - ref_price) / ref_price * 100 <= liq_pct:
            current.append(i)
        else:
            clusters.append(current)
            current = [i]
    clusters.append(current)
    tagged = set()
    for c in clusters:
        if len(c) >= 2:
            tagged.update(c)
    return tagged


def find_sweeps(candles, swing_low_indices):
    """For each swing low, finds the first later candle whose low wicks
    below it -- that's a B (sweep) event. Returns (a_idx, b_idx) pairs."""
    sweeps = []
    for a in swing_low_indices:
        level = candles[a].low
        for b in range(a + 1, len(candles)):
            if candles[b].low < level:
                sweeps.append((a, b))
                break
    return sweeps


def nearest_c_candidate(candles, swing_low_indices, before_idx, below_price, tagged=None):
    """Nearest (most recent) prior swing low, strictly before before_idx,
    that sits below below_price. If tagged is given, only swing lows in
    that set qualify."""
    best = None
    for i in swing_low_indices:
        if i >= before_idx:
            continue
        if candles[i].low >= below_price:
            continue
        if tagged is not None and i not in tagged:
            continue
        if best is None or i > best:
            best = i
    return best


def check_bounce(candles, from_idx, c_level, lookahead):
    """Looks forward from from_idx: does price wick down to c_level and
    then close back above it within `lookahead` candles?
    Returns True (confirmed bounce), False (touched, never confirmed),
    or None (never even reached the level)."""
    touched = False
    for i in range(from_idx, min(from_idx + lookahead, len(candles))):
        if candles[i].low <= c_level:
            touched = True
            if candles[i].close > c_level:
                return True
    return False if touched else None


def run_comparison(candles, swing, liq_pct, lookahead):
    swing_lows = find_swing_lows(candles, swing)
    tagged = cluster_liquidity(candles, swing_lows, liq_pct)
    sweeps = find_sweeps(candles, swing_lows)

    results = {
        "inclusive": {"candidates": 0, "bounced": 0, "touched_not_confirmed": 0, "never_reached": 0},
        "strict": {"candidates": 0, "bounced": 0, "touched_not_confirmed": 0, "never_reached": 0},
    }

    for a_idx, b_idx in sweeps:
        b_price = candles[b_idx].low
        c_incl = nearest_c_candidate(candles, swing_lows, a_idx, b_price)
        c_strict = nearest_c_candidate(candles, swing_lows, a_idx, b_price, tagged=tagged)

        for label, c_idx in (("inclusive", c_incl), ("strict", c_strict)):
            if c_idx is None:
                continue
            r = results[label]
            r["candidates"] += 1
            outcome = check_bounce(candles, b_idx, candles[c_idx].low, lookahead)
            if outcome is True:
                r["bounced"] += 1
            elif outcome is False:
                r["touched_not_confirmed"] += 1
            else:
                r["never_reached"] += 1

    return swing_lows, sweeps, results


def print_report(candles, swing_lows, sweeps, results):
    print(f"Candles: {len(candles)}  |  Swing lows: {len(swing_lows)}  |  Sweep (B) events: {len(sweeps)}")
    print()
    for label in ("inclusive", "strict"):
        r = results[label]
        print(f"-- {label} --")
        print(f"  C-candidates found:      {r['candidates']}")
        print(f"  Bounced (confirmed):     {r['bounced']}")
        print(f"  Touched, not confirmed:  {r['touched_not_confirmed']}")
        print(f"  Never reached:           {r['never_reached']}")
        if r["candidates"]:
            print(f"  Bounce rate:             {r['bounced'] / r['candidates'] * 100:.1f}%")
        print()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", required=True, help="Path to candle CSV (epoch,open,high,low,close)")
    parser.add_argument("--swing", type=int, default=10, help="Swing sensitivity, matches your tool's Swing param")
    parser.add_argument("--liq-pct", type=float, default=1.0, help="Clustering %%, matches your tool's Liq %% param")
    parser.add_argument("--lookahead", type=int, default=40, help="Candles to watch forward for a bounce")
    args = parser.parse_args()

    candles = load_candles(args.csv)
    swing_lows, sweeps, results = run_comparison(candles, args.swing, args.liq_pct, args.lookahead)
    print_report(candles, swing_lows, sweeps, results)


if __name__ == "__main__":
    main()
