# SMC Dashboard

Live chart for Deriv's synthetic indices with Smart Money Concepts overlays
on top — swing points, BOS/CHoCH, fair value gaps, order blocks, liquidity
zones. Updates as new candles come in, no refreshing needed.

**Live:** https://web-production-e22932.up.railway.app

## Using it

Open it up, give it a couple seconds to connect, and the symbol list plus
chart should populate on their own.

- Pick a symbol from the dropdown top-left — any of the Volatility indices,
  Boom/Crash, Step, Jump, etc. These run 24/7 so there's always something
  live to look at.
- Timeframes across the top, 1m through 1d. Changing it reloads history for
  that granularity.
- Price top-right, green/red depending on the last candle's direction. The
  little dot next to it goes green once it's actually streaming.

One thing to know: a few of the symbols (Gold Basket etc.) track real
markets and follow real market hours, so outside those hours you'll get
history but no live ticks. Doesn't apply to the pure synthetics, they never
close.

There's a `?` button top-right if you forget what any of the overlays mean —
opens a quick explainer in-app.

### What the overlays are

- **Swing H/L** — the amber arrows. Local highs and lows, everything else
  is built off these.
- **BOS / CHoCH** — blue markers. BOS = price breaks a swing point in the
  direction it was already going (trend continuing). CHoCH = breaks the
  other way (possible reversal starting).
- **Fair value gaps** — teal/coral boxes. 3-candle imbalances price tends to
  come back and fill. Solid = still open, faded = already filled.
- **Order blocks** — similar boxes, flagged as likely institutional entries
  before a big move. Opacity roughly tracks strength.
- **Liquidity** — dashed amber lines, marked LQ. Clusters of equal highs/
  lows where stops probably sit. Faded + "swept" once price has run through.

Green-ish = bullish, coral = bearish, consistently across all of it.

### Swing / Liq % settings

Swing controls how many candles on either side define a pivot — lower gets
you more (noisier) structure, good for lower timeframes; higher gives fewer,
cleaner points. Liq % is how close two highs/lows need to be to count as the
same cluster. Both apply instantly.

### Legend sidebar

Checkboxes to toggle each overlay off if the chart gets too busy, plus a
live count of how many are plotted.

## Telegram alerts

Message the bot `/start` and you'll get pinged for new BOS/CHoCH breaks and
liquidity sweeps across every synthetic index, not just whatever's open on
screen. `/stop` to unsubscribe.

## Worth keeping in mind

- Order block "strength" isn't real volume — Deriv synthetics don't have
  actual traded volume, so it's derived from candle size instead. Take it
  as relative, not literal.
- This just shows structure, it doesn't tell you what to trade. What you do
  with any of it is on you.
- No connection to your actual Deriv account or funds — pure chart, no
  execution.
- Lower timeframes (1m especially) throw a lot more noise than signal.

## If something's off

- Chart not updating → check the status dot, refresh if it's not green.
- Old data, no live ticks → probably a real-market symbol outside trading
  hours, switch to a synthetic index instead.
- No overlays showing at all → try lowering Swing, a high value on a short
  history can wipe everything out.

Setup/deploy stuff is in `DEVELOPMENT.md`, not here.
