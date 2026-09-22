"""
Setup detection for the sweep-then-deeper-C pattern (the six original
scenarios), built on top of smc_engine.compute_all() -- NOT a
reimplementation of swing/liquidity detection. That part is already
solved correctly by the smartmoneyconcepts library and matches the live
tool exactly; this only adds the one piece that's specific to this
pattern.

Direction convention (confirmed empirically against the real library,
not assumed): a liquidity zone with direction == "bearish" is LOW-based
(swept when price breaks downward through it) -- that's the "A -> B"
half of the pattern for the long side. "bullish" is HIGH-based, for the
mirrored short-side version later.

Simplification worth flagging: some of the six original scenarios (1, 2)
tied C's level back to an OLD prior low from before A, not just
whatever forms next after the sweep. This version doesn't check for
that yet -- C here is simply the next confirmed swing low after the
sweep, i.e. wherever the decline actually bottoms out. Simpler, more
general, and matches scenario 3/4/6 directly; scenario 1/2's
"references an old level" nuance is a refinement to test later, not
included here.
"""


def find_sweep_c_setups(swings: list[dict], liquidity: list[dict], direction: str = "bearish",
                         c_mode: str = "next_after_sweep") -> list[dict]:
    """
    swings: smc_engine.compute_all()'s "swings" list.
    liquidity: smc_engine.compute_all()'s "liquidity" list.
    direction: "bearish" for the long-side pattern (low swept, bounce up),
               "bullish" for the mirrored short-side pattern later.
    c_mode:
        "next_after_sweep" -- C is the next confirmed swing low that
            forms AFTER the sweep (the original, simpler version).
        "nearest_prior_before_a" -- C is the nearest already-confirmed
            swing low that existed BEFORE A even formed, with a level
            below the sweep price. This is the "old level the market
            remembers" version from scenarios 1 and 2 -- and the
            relevant one for a live "approaching" alert, since it
            doesn't require waiting for a brand-new low to confirm.

    Returns one dict per setup: a_time/a_level (where the liquidity
    pool formed), b_time (when it was swept), c_time/c_level (the
    entry candidate, defined per c_mode above).
    """
    swing_type = "low" if direction == "bearish" else "high"
    swing_pts = [s for s in swings if s["type"] == swing_type]

    setups = []
    for lq in liquidity:
        if lq["direction"] != direction or lq["swept_time"] is None:
            continue

        if c_mode == "next_after_sweep":
            c = next((s for s in swing_pts if s["time"] > lq["swept_time"]), None)
        elif c_mode == "nearest_prior_before_a":
            # Nearest (most recent) prior swing low, strictly before A's
            # own formation time, with a level below the sweep price.
            # sweep price = the liquidity zone's own level, since that's
            # what got broken.
            prior = [s for s in swing_pts if s["time"] < lq["time"] and s["level"] < lq["level"]]
            c = max(prior, key=lambda s: s["time"]) if prior else None
        else:
            raise ValueError(f"unknown c_mode: {c_mode}")

        if c is None:
            continue

        setups.append({
            "a_time": lq["time"],
            "a_level": lq["level"],
            "b_time": lq["swept_time"],
            "c_time": c["time"],
            "c_level": c["level"],
        })
    return setups
