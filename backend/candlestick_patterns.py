"""
Standard candlestick pattern detection over an OHLC dataframe - single,
two, and three-candle formations that are conventionally read as bullish
or bearish. Pure pandas/numpy, no external TA library. Output shape
matches the rest of smc_engine.py: a list of plain dicts, epoch seconds,
ready to serialize straight over the websocket.

Shape-only patterns from the "hammer family" (small body, one long wick)
mean OPPOSITE things depending on what came before them - a hammer-shaped
candle is a bullish Hammer after a downtrend but a bearish Hanging Man
after an uptrend, even though the candle itself is pixel-identical.
Same pairing for Inverted Hammer (bullish) vs Shooting Star (bearish).
Getting that backwards would be a real, visible bug on a chart people
read directionally, so trend context comes from the same swing structure
smc.swing_highs_lows() already produces for the BOS/CHoCH engine, not a
guess from candle shape alone. When there isn't enough swing history yet
to call a bias, these four patterns are simply not flagged rather than
guessed at.

Doji is deliberately not flagged on its own - it's pure indecision, not
a bullish or bearish signal by itself, and this module only surfaces
patterns with a clear directional read. It still matters as a component
of Morning/Evening Star below.

Morning/Evening Star classically also want a "gap" either side of the
middle candle. Deriv synthetic indices trade continuously (no real
session open/close), so true price gaps essentially never happen here -
requiring one would make these two patterns almost never fire. Dropped
that requirement and rely on the standard body-position test instead
(candle 3 closing back past the midpoint of candle 1's body).
"""
import numpy as np
import pandas as pd

# --- tunable thresholds, all relative (to a candle's own range or to ATR),
# never absolute price - keeps this working the same on a $0.01 synthetic
# tick size and a $1000-wide index alike. Change these first if the chart
# feels too noisy or too quiet. ---
HAMMER_WICK_MULT = 2.5        # long wick must be >= 2.5x the body
HAMMER_SHORT_WICK_MAX = 0.35  # the *other* wick must be <= 0.35x the body
HAMMER_BODY_MAX_PCT = 0.3     # body must be <= 30% of the candle's full range
MARUBOZU_BODY_MIN_PCT = 0.92  # body >= 92% of range - wicks basically absent
STAR_BODY_MAX_PCT = 0.2       # candle 2 in a star pattern: genuinely small body, not just smallish
ENGULF_MARGIN = 1.3           # engulfing body must clear the prior body by 30% - a bare-minimum
                               # engulf is too easy to satisfy on tiny consecutive candles
THREE_SOLDIERS_WICK_MAX = 0.25 # trailing wick on each soldier/crow <= 25% of its own body
ATR_PERIOD = 14
MIN_ATR_MULT = 0.5            # floor #1: body >= 50% of recent ATR
BODY_QUANTILE_LOOKBACK = 20   # floor #2: body must also be a genuinely large candle *for this
BODY_QUANTILE = 0.75          # stretch of the chart* (>= 75th percentile of the last 20 bodies).
                               # ATR alone isn't enough on a steady, low-volatility grind (Boom/Crash-
                               # style indices) where every candle clears a fixed ATR fraction equally -
                               # this floor only lets through candles that stand out from their own
                               # recent neighbors, not just from the whole dataset's average noise.
PATTERN_COOLDOWN = 8          # candles: don't re-flag the *same pattern type* again this soon after
                               # the last one - stops a repeating micro-pattern (e.g. engulfing on
                               # every small pullback in a grind) from turning into a solid wall


def _atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period, min_periods=1).mean()


def _trend_bias(df: pd.DataFrame, swings: pd.DataFrame) -> pd.Series:
    """
    +1 while price is making higher highs / higher lows (uptrend context),
    -1 while making lower lows / lower highs (downtrend context), 0 where
    there isn't enough swing history yet to say. Forward-filled so every
    candle between two swing points inherits the last confirmed regime.

    Lightweight on purpose - it answers "was the approach into this candle
    bullish or bearish", not a full structure/BOS classifier (that's what
    the rest of smc_engine.py is for). Two consecutive same-type swings
    disagreeing on direction is enough to flip it.
    """
    bias = pd.Series(0, index=df.index, dtype=int)
    last_high = last_low = None
    for i, row in swings.dropna(subset=["HighLow"]).iterrows():
        level = row["Level"]
        if row["HighLow"] == 1:  # swing high
            if last_high is not None:
                bias.iloc[i:] = 1 if level >= last_high else -1
            last_high = level
        else:  # swing low
            if last_low is not None:
                bias.iloc[i:] = -1 if level <= last_low else 1
            last_low = level
    return bias


def detect_all(df: pd.DataFrame, swings: pd.DataFrame) -> list[dict]:
    n = len(df)
    if n == 0:
        return []

    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    body = (c - o).abs()
    rng = (h - l).replace(0, np.nan)  # avoid div-by-zero on a flat/zero-range candle
    upper_wick = h - np.maximum(o, c)
    lower_wick = np.minimum(o, c) - l
    bullish, bearish = c > o, c < o
    atr = _atr(df)
    bias = _trend_bias(df, swings)
    body_floor = body.rolling(BODY_QUANTILE_LOOKBACK, min_periods=5).quantile(BODY_QUANTILE)
    min_body = np.maximum(atr * MIN_ATR_MULT, body_floor.fillna(0))

    out: list[dict] = []

    def emit(mask: pd.Series, pattern: str, direction: str, code: str, min_index: int = 0):
        for i in np.flatnonzero(mask.fillna(False).to_numpy()):
            if i < min_index:
                continue
            out.append({
                "_i": int(i),
                "time": int(df["time"].iloc[i]),
                "pattern": pattern,
                "direction": direction,
                "code": code,
            })

    # ---------- single-candle: hammer family (context decides the name) ----------
    hammer_shape = (
        (lower_wick >= HAMMER_WICK_MULT * body) & (upper_wick <= HAMMER_SHORT_WICK_MAX * body)
        & (body <= HAMMER_BODY_MAX_PCT * rng) & (body >= min_body)
    )
    emit(hammer_shape & (bias == -1), "hammer", "bullish", "HAMMER")
    emit(hammer_shape & (bias == 1), "hanging_man", "bearish", "HANG")

    star_shape = (
        (upper_wick >= HAMMER_WICK_MULT * body) & (lower_wick <= HAMMER_SHORT_WICK_MAX * body)
        & (body <= HAMMER_BODY_MAX_PCT * rng) & (body >= min_body)
    )
    emit(star_shape & (bias == -1), "inverted_hammer", "bullish", "INVH")
    emit(star_shape & (bias == 1), "shooting_star", "bearish", "STAR")

    # ---------- single-candle: marubozu (near-zero wicks, full conviction) ----------
    marubozu = (body >= MARUBOZU_BODY_MIN_PCT * rng) & (body >= min_body)
    emit(marubozu & bullish, "bullish_marubozu", "bullish", "MARU")
    emit(marubozu & bearish, "bearish_marubozu", "bearish", "MARU")

    # ---------- two-candle: engulfing ----------
    po, pc = o.shift(1), c.shift(1)
    prev_bullish, prev_bearish = pc > po, pc < po
    engulf_bull = prev_bearish.fillna(False) & bullish & (o <= pc) & (c >= po + ENGULF_MARGIN*(po - pc))
    engulf_bear = prev_bullish.fillna(False) & bearish & (o >= pc) & (c <= po - ENGULF_MARGIN*(pc - po))
    emit(engulf_bull & (body >= min_body), "bullish_engulfing", "bullish", "ENGULF", min_index=1)
    emit(engulf_bear & (body >= min_body), "bearish_engulfing", "bearish", "ENGULF", min_index=1)

    # ---------- two-candle: piercing line / dark cloud cover ----------
    prev_mid = (po + pc) / 2
    piercing = prev_bearish.fillna(False) & bullish & (o < l.shift(1)) & (c > prev_mid) & (c < po)
    dark_cloud = prev_bullish.fillna(False) & bearish & (o > h.shift(1)) & (c < prev_mid) & (c > po)
    emit(piercing & (body >= min_body), "piercing_line", "bullish", "PIERCE", min_index=1)
    emit(dark_cloud & (body >= min_body), "dark_cloud_cover", "bearish", "DARKCLD", min_index=1)

    # ---------- three-candle: morning / evening star ----------
    b1_bear = bearish.shift(2).fillna(False)
    b1_bull = bullish.shift(2).fillna(False)
    star_small = (body.shift(1) <= STAR_BODY_MAX_PCT * rng.shift(1)).fillna(False)
    c1_mid = (o.shift(2) + c.shift(2)) / 2
    morning_star = b1_bear & star_small & bullish & (c > c1_mid)
    evening_star = b1_bull & star_small & bearish & (c < c1_mid)
    emit(morning_star & (body >= min_body), "morning_star", "bullish", "MSTAR", min_index=2)
    emit(evening_star & (body >= min_body), "evening_star", "bearish", "ESTAR", min_index=2)

    # ---------- three-candle: three white soldiers / three black crows ----------
    three_up = (
        bullish & bullish.shift(1).fillna(False) & bullish.shift(2).fillna(False)
        & (c > c.shift(1)) & (c.shift(1) > c.shift(2))
        & (o > o.shift(1)) & (o < c.shift(1))
        & (o.shift(1) > o.shift(2)) & (o.shift(1) < c.shift(2))
        & (upper_wick <= THREE_SOLDIERS_WICK_MAX * body)
    )
    three_down = (
        bearish & bearish.shift(1).fillna(False) & bearish.shift(2).fillna(False)
        & (c < c.shift(1)) & (c.shift(1) < c.shift(2))
        & (o < o.shift(1)) & (o > c.shift(1))
        & (o.shift(1) < o.shift(2)) & (o.shift(1) > c.shift(2))
        & (lower_wick <= THREE_SOLDIERS_WICK_MAX * body)
    )
    emit(three_up & (body >= min_body), "three_white_soldiers", "bullish", "3WS", min_index=2)
    emit(three_down & (body >= min_body), "three_black_crows", "bearish", "3BC", min_index=2)

    # A candle can legitimately satisfy more than one shape at once (e.g. also
    # be a hammer AND engulf the prior candle). Showing every match stacks
    # unreadable text on the chart, so keep only the most specific one per
    # candle - three-candle patterns first, then two-candle, then one-candle.
    _rank = {
        "morning_star": 0, "evening_star": 0, "three_white_soldiers": 0, "three_black_crows": 0,
        "bullish_engulfing": 1, "bearish_engulfing": 1, "piercing_line": 1, "dark_cloud_cover": 1,
        "hammer": 2, "hanging_man": 2, "inverted_hammer": 2, "shooting_star": 2,
        "bullish_marubozu": 3, "bearish_marubozu": 3,
    }
    best: dict[int, dict] = {}
    for p in out:
        cur = best.get(p["time"])
        if cur is None or _rank[p["pattern"]] < _rank[cur["pattern"]]:
            best[p["time"]] = p
    deduped = sorted(best.values(), key=lambda p: p["_i"])

    # Cooldown pass: a pattern type repeating every candle or two (e.g. engulfing
    # through a choppy micro-grind) is noise, not a series of distinct signals.
    last_seen: dict[str, int] = {}
    out = []
    for p in deduped:
        last = last_seen.get(p["pattern"])
        if last is not None and p["_i"] - last < PATTERN_COOLDOWN:
            continue
        last_seen[p["pattern"]] = p["_i"]
        del p["_i"]
        out.append(p)
    return out
