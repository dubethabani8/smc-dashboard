# SMC Dashboard — live Smart Money Concepts chart for Deriv synthetic indices

A live trading chart for Deriv's synthetic/volatility indices, with Smart
Money Concepts overlays computed and updated in real time as new candles
form: swing highs/lows, Break of Structure / Change of Character, Fair Value
Gaps, Order Blocks, and Liquidity sweeps.

**Live URL:** _add your deployed link here once you have it_

---

## Getting started

Open the dashboard in your browser. Within a couple of seconds it connects
to Deriv and starts streaming — you'll see the symbol dropdown populate and
a candlestick chart appear.

### Pick a market and timeframe

- **Symbol dropdown** (top left) — choose any Deriv synthetic index: the
  Volatility indices (10/25/50/75/100, including the 1-second variants),
  Boom/Crash, Step Index, Jump indices, and similar. These trade **24/7**,
  including weekends.
- **Timeframe buttons** — 1m, 5m, 15m, 30m, 1h, 4h, 1d. Switching timeframe
  reloads history and overlays for that granularity.
- The current price sits top-right, colored green when the latest candle is
  up, red when it's down. The status dot next to it turns green with
  "live" once streaming is active for the selected symbol.

> Some Deriv symbols track real markets (e.g. a Gold Basket) rather than
> synthetics, and follow that market's actual trading hours — if you pick
> one of those outside its market hours, you'll see historical data but no
> live updates until it reopens. The pure synthetic/volatility indices don't
> have this limitation.

### Reading the chart

| Overlay | What it means |
|---|---|
| **Swing H/L** (amber arrows) | Local swing highs and lows — the structural pivots everything else is built from. |
| **BOS / CHoCH** (blue markers) | Break of Structure = price breaks a prior swing in the direction of the trend (continuation signal). Change of Character = price breaks the opposite way (possible reversal). |
| **Fair Value Gaps** (teal/coral shaded boxes) | 3-candle imbalances the market often revisits. Solid fill = still open (unfilled); faded fill = already mitigated. |
| **Order Blocks** (teal/coral bordered boxes) | Candle ranges flagged as likely institutional positioning before a strong move. Box opacity roughly tracks the block's relative strength. |
| **Liquidity** (dashed amber lines, labeled "LQ") | Clusters of equal highs/lows where stop-losses tend to sit. Solid line = still active; faded + "LQ swept" = price has already run through it. |

Bullish structures are teal, bearish are coral, throughout.

### Adjusting sensitivity

- **Swing** (top bar) — how many candles define a swing point. Lower =
  more (noisier) structure, useful on lower timeframes. Higher = fewer,
  cleaner structural points, better suited to higher timeframes.
- **Liq %** — how close two highs/lows need to be (as a percentage) to
  count as an equal-highs/lows liquidity cluster. Lower = stricter
  (fewer, tighter clusters); higher = looser (more clusters detected).

Both take effect immediately and reload the current symbol/timeframe.

### The overlay legend (right sidebar)

Each overlay has a checkbox to toggle it on/off, and shows a live count of
how many are currently plotted on the chart. Turning overlays off is useful
when the chart gets visually busy (e.g. a lot of Fair Value Gaps stacking up
on a low timeframe) and you want to focus on just structure, or just
liquidity, etc.

---

## Reading this responsibly

A few things worth keeping in mind while using this as a trading aid:

- **Order Block strength is a proxy, not real volume.** Deriv's synthetic
  indices are algorithmically generated and have no real traded volume, so
  the "strength" score behind each Order Block is derived from candle
  range/body size as a stand-in — treat it as a relative signal, not a
  literal measure of institutional order flow.
- **This tool shows patterns, it doesn't recommend trades.** BOS, CHoCH,
  FVGs, Order Blocks, and Liquidity sweeps are structural observations —
  what other traders may act on, not a signal to buy or sell by itself.
  How (or whether) to act on any of it is your call and your risk.
- **It doesn't place trades.** This is purely a visual analysis aid running
  alongside wherever you actually execute trades — it has no connection to
  your Deriv trading account or funds.
- **Higher timeframes are generally more reliable** for structure than very
  low ones (1m especially) — lower timeframes produce more signals, but
  also more noise.

---

## If something looks wrong

- **Chart stuck / not updating**: check the status dot — if it's not green
  and saying "live", the connection may have dropped. Refreshing the page
  reconnects.
- **A symbol shows old data with no live updates**: you likely picked a
  real-market symbol (not a pure synthetic) outside its trading hours —
  switch to a Volatility/Boom/Crash/Step/Jump index for 24/7 coverage.
- **Overlay counts show 0 across the board**: try lowering the Swing value
  — a very high setting on a short price history can suppress everything.

For anything about how this is built, deployed, or modified, see
`DEVELOPMENT.md` in this repo instead of this file.
