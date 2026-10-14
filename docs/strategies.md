# Strategy definitions and evidence limits

The strategy registry now contains two rule sets. Both run on a user-selected completed candle interval (M1, M5,
M15, M30, or H1), create their own stop and target from price structure, and expose editable, versioned parameters in the
dashboard. A research result is specific to its broker feed, timeframe, date range, contract specification, and cost
assumptions. Neither strategy is presumed profitable on gold.

## ICT liquidity sweep, MSS, and FVG retest

The rule sequence takes its concepts from Michael J. Huddleston's public ICT 2022 mentorship material, including
[Episode 2: Elements of a Trade Setup](https://www.youtube.com/watch?v=tmeCWULSTHc),
[Episode 3: Internal Range Liquidity and Market Structure Shifts](https://www.youtube.com/watch?v=nQfHZ2DEJ8c),
and his [Fair Value Gap and Market Structure Shift discussion](https://www.youtube.com/watch?v=Bkt8B3kLATQ).
Those lessons teach concepts and context; they do not define one universally agreed, fully mechanical EA with these
exact numeric settings. This implementation therefore identifies itself as an ICT-concept entry model, not a
complete replacement for the discretionary higher-timeframe narrative, timing, and judgment in the mentorship.

1. **Mark swing liquidity.** A swing high or low is the highest or lowest candle among the configured number of bars
   to its left and right. It is unavailable until every right-side confirmation bar has closed. The default is two
   bars on each side.
2. **Wait for a sweep and reclaim.** A bullish candidate forms when a candle trades below a still-active confirmed
   swing low and closes back above it. A bearish candidate trades above a confirmed swing high and closes back below
   it. A candle that sweeps both sides is treated as ambiguous and ignored.
3. **Require a structure shift with imbalance.** After the sweep, a later candle must close through the latest
   opposing confirmed swing. A three-candle fair value gap must be confirmed on that candle or within the next two
   candles (configurable): for a bullish gap, the third candle's low is above the first candle's high; for a bearish gap,
   the third candle's high is below the first candle's low. This permits the displacement candle to be the middle
   candle of the gap. No body-size
   threshold is added as a substitute for ICT's qualitative displacement concept.
4. **Wait for the first completed-candle FVG return.** After the shift candle, price must revisit the gap and close
   inside it. The historical engine submits at the next bar open; live execution uses its normal post-candle quote
   and risk checks. This close-confirmed rule is deliberately testable and does not pretend to model an intrabar
   limit order fill.
5. **Place the bracket from structure.** The stop is at the sweep candle's extreme. The target is the nearest
   unswept, confirmed swing liquidity on the opposite side. A setup is skipped if that target is behind price or
   fails the configured minimum reward-to-risk filter (1.0 by default). A signal expires after one entry candle;
   a pending pattern expires after 24 bars. Swings older than 96 bars are ignored. A position has a 24-hour time exit
   by default.

Pending patterns reset across missing candles or market closures. The two-bar swing confirmation, two-bar FVG
confirmation allowance, 96-bar level age, 24-bar setup lifetime, 1.0 minimum reward-to-risk, and 24-hour
position timeout are explicit software choices exposed to the user. They are not attributed to Michael as universal
settings and should be evaluated on the selected Exness history. The implementation does not add order blocks,
Fibonacci, SMT divergence, kill zones, or a higher-timeframe directional filter; these are not silently inferred from
the words “ICT.”

## Rolling support/resistance bounce

This rule is based on the documented rolling-extrema method in Chung and Bellotti,
[“Evidence and Behaviour of Support and Resistance Levels in Financial Time Series”](https://arxiv.org/abs/2101.07410).
That study examined minute data for EUR/USD, Lloyds Banking Group shares, and Brent crude, and reported that
previous bounces were associated with higher subsequent bounce probability. It did not test this application's
stop/target trading system or XAU/USD.

1. From the preceding 240 minutes of completed closes (configurable from 60 to 1,440 minutes), with at least 16
   closed candles by default, calculate the lowest close as support and highest close as resistance. The observation
   floor extends M30 to eight hours and H1 to sixteen hours; this is an explicit adaptation, not a number from the paper.
2. Set the zone half-width, gamma, to the mean absolute close-to-close change in that preceding window. While price is
   inside a zone, keep its boundaries fixed until it exits or penetrates the far side.
3. Count a bounce only after price enters from one side and then closes back out that same side without trading
   through the far boundary. If one candle crosses the far edge, its OHLC does not reveal the intrabar path; the level
   is discarded and no bounce signal is issued.
4. Count completed crossings into and out of the exact discovered zone in the trailing close sample, excluding an
   unmatched entry/exit at a sample boundary. By default, require one earlier bounce before taking the next bounce. The strategy enters
   on the confirming close, with its stop beyond the far zone edge and target at the opposing rolling zone. A
   configurable reward-to-risk floor (1.0 by default) rejects too-close targets.

The paper computes gamma from the complete observed series. That is suitable for its statistical analysis but would
leak future information in a real-time bot. This code uses only the trailing window available before each candle.
Frozen zones remain fixed until the observed bounce/penetration resolves. A history gap resets both zones and
requires a new contiguous lookback. That causal adaptation, candle-count floor, zone persistence, and conversion of a bounce event into a stop/target trade are explicit
implementation choices, not the exact system evaluated by the paper. The New York Fed's earlier
[support/resistance study](https://www.newyorkfed.org/research/epr/00v06n2/0007osle.html) evaluated levels published by
six FX firms against intraday exchange rates; it was not a gold CFD EA backtest.

## Research and use

The dashboard and CLI compare the two systems on the selected historical interval with the same CFD cost model,
broker volume-step sizing, risk limits, and next-bar execution assumptions. Exploratory backtests default to both
strategies on M1, M5, M15, M30 and H1, with an account replay and an independent minimum-lot setup study for each pair.
Account replays default to a maximum of 10 filled trades per UTC day and a 60-minute cooldown after five losses;
the live/demo entry limit remains separately configured. Setup studies evaluate all executable signals with overlapping
positions and account guards disabled; trades above the reference risk budget are flagged and no account return is claimed.
The per-entry audit CSV
records every generated setup, including rejected setups, with its risk budget, estimated minimum-lot risk, estimated
lot size, rejection reason, and minimum capital needed to risk the broker's minimum lot at the selected stop.
At a $1,000 balance and 0.25% risk, the planned budget is $2.50; if the stop's estimated minimum-lot risk exceeds
that budget, the engine reports the setup and rejects it instead of pretending the trade fit. Structural stops and targets are not
replaced by the old generic ATR grid. Save a changed strategy configuration as a new configuration hash, then run
development and holdout validation against the intended dataset. Until the broker's gold symbol results pass the project's
explicit approval gates and demo execution is separately authorized, treat signals and backtests as research only.
