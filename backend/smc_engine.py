"""
Runs smartmoneyconcepts over an OHLC dataframe and turns the output into
plain JSON the frontend can plot directly (epoch seconds, matches what
lightweight-charts wants).

No real volume on Deriv synthetics so the "volume" fed into the order block
calc is just derived from candle range/body size - fine for a relative
strength score, not literal order flow.
"""
import logging

import pandas as pd
from smartmoneyconcepts import smc

log = logging.getLogger("smc-dashboard.engine")


def build_dataframe(candles: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(candles)
    df = df.rename(columns={"epoch": "time"})
    df["time"] = df["time"].astype(int)
    df = df.drop_duplicates(subset="time").sort_values("time").reset_index(drop=True)
    for col in ("open", "high", "low", "close"):
        df[col] = df[col].astype(float)
    if "volume" not in df.columns:
        df["volume"] = (df["high"] - df["low"]).abs() + (df["close"] - df["open"]).abs()
    df.index = pd.to_datetime(df["time"], unit="s")
    return df


def _epoch(df: pd.DataFrame, i) -> int | None:
    if i is None or pd.isna(i):
        return None
    i = int(i)
    if i < 0 or i >= len(df):
        return None
    return int(df["time"].iloc[i])


def compute_all(df: pd.DataFrame, swing_length: int = 10, range_percent: float = 0.01) -> dict:
    swings = smc.swing_highs_lows(df, swing_length=swing_length)
    bos_choch = smc.bos_choch(df, swings, close_break=True)
    fvg = smc.fvg(df, join_consecutive=True)
    ob = smc.ob(df, swings, close_mitigation=False)
    liquidity = smc.liquidity(df, swings, range_percent=range_percent)

    out_swings = []
    for i, row in swings.dropna(subset=["HighLow"]).iterrows():
        out_swings.append({
            "time": int(df["time"].iloc[i]),
            "type": "high" if row["HighLow"] == 1 else "low",
            "level": float(row["Level"]),
        })

    out_bos_choch = []
    for i, row in bos_choch.dropna(subset=["Level"]).iterrows():
        is_bos = pd.notna(row.get("BOS"))
        val = row["BOS"] if is_bos else row["CHOCH"]
        out_bos_choch.append({
            "time": int(df["time"].iloc[i]),
            "kind": "BOS" if is_bos else "CHOCH",
            "direction": "bullish" if val == 1 else "bearish",
            "level": float(row["Level"]),
            "broken_time": _epoch(df, row.get("BrokenIndex")),
        })

    out_fvg = []
    for i, row in fvg.dropna(subset=["FVG"]).iterrows():
        out_fvg.append({
            "time": int(df["time"].iloc[i]),
            "direction": "bullish" if row["FVG"] == 1 else "bearish",
            "top": float(row["Top"]),
            "bottom": float(row["Bottom"]),
            "mitigated_time": _epoch(df, row.get("MitigatedIndex")),
        })

    out_ob = []
    for i, row in ob.dropna(subset=["OB"]).iterrows():
        out_ob.append({
            "time": int(df["time"].iloc[i]),
            "direction": "bullish" if row["OB"] == 1 else "bearish",
            "top": float(row["Top"]),
            "bottom": float(row["Bottom"]),
            "strength_pct": float(row["Percentage"]),
            "mitigated_time": _epoch(df, row.get("MitigatedIndex")),
        })

    # --- DEBUG: dump raw liquidity columns once per call, so we can see
    # exactly what smartmoneyconcepts is putting in the Swept column
    # before our own code interprets it. Remove once diagnosed.
    try:
        non_null_liq = liquidity.dropna(subset=["Liquidity"])
        log.warning(
            "LQ_DEBUG df_len=%d df_first_time=%s df_last_time=%s liquidity_rows=%d",
            len(df),
            int(df["time"].iloc[0]) if len(df) else None,
            int(df["time"].iloc[-1]) if len(df) else None,
            len(non_null_liq),
        )
        for i, row in non_null_liq.iterrows():
            raw_swept = row.get("Swept")
            raw_end = row.get("End")
            log.warning(
                "LQ_DEBUG row_idx=%s level=%.4f direction=%s raw_Swept=%r raw_End=%r "
                "is_swept_nan=%s is_swept_zero=%s computed_time=%s computed_swept_time=%s",
                i,
                float(row["Level"]),
                "bullish" if row["Liquidity"] == 1 else "bearish",
                raw_swept,
                raw_end,
                pd.isna(raw_swept),
                (raw_swept == 0) if pd.notna(raw_swept) else False,
                int(df["time"].iloc[i]),
                _epoch(df, raw_swept),
            )
    except Exception:
        log.exception("LQ_DEBUG logging failed")
    # --- END DEBUG

    out_liq = []
    for i, row in liquidity.dropna(subset=["Liquidity"]).iterrows():
        out_liq.append({
            "time": int(df["time"].iloc[i]),
            "direction": "bullish" if row["Liquidity"] == 1 else "bearish",
            "level": float(row["Level"]),
            "end_time": _epoch(df, row.get("End")),
            "swept_time": _epoch(df, row.get("Swept")),
        })

    return {
        "swings": out_swings,
        "bos_choch": out_bos_choch,
        "fvg": out_fvg,
        "order_blocks": out_ob,
        "liquidity": out_liq,
    }