# Research assumptions and limits

All application historical research uses actual downloaded broker OHLC data. Synthetic fixtures are stored only by
tests in temporary databases. A broker history failure is an error, never an invitation to generate prices. The
current local broker dataset has three years of M15/H1 candles, but M5 has only 100,000 recent bars; every result must
report the selected interval's actual available dates. Coverage of other intervals depends on broker and terminal limits.

Current strategy definitions and evidence limits are documented in [strategies.md](strategies.md). Both rules use
completed entry-timeframe candles and causally available price structure. Swing pivots are not available until their
right-side confirmation bars close. Support/resistance width uses only trailing history, not the complete sample.
All internal timestamps are UTC.

The initial executable research adapter supports **confirmed bid-based, USD-settled linear gold CFDs** only.
The adapter records MT5's symbol chart mode as bid or last; research rejects last/unknown price bases.
Other currencies or contract payoff functions are rejected rather than valued at invented FX rates.

Each Backtesting.py integer unit is a broker volume step; economic OHLC prices are scaled by contract size times
volume step. This preserves CFD P&L and permits fractional lots without interpreting lot counts as ounces. Quantities
are rounded down and recalculated at actual modeled entry prices. Entry signals execute at the next bar open.

Historical replay applies the same default portfolio guardrails used by demo settings: 5% daily loss from UTC day
equity, 3% weekly loss from Monday 00:00 UTC, 10% peak drawdown, five consecutive net losses, and three requested
entries per named session (or UTC day for continuous strategies). One simultaneous position is the default; historical
simulations allow up to ten when the combined open-risk cap is respected. The demo default remains one. The lab
allows these thresholds, per-trade risk, starting capital, cost assumptions, strategy parameters, and timeframes to
be changed per research run. Each run persists the exact settings, metrics, trades and equity curve in its report
folder; trade ledgers include simulated lot size, prices, commission, financing, net P&L, and R. Full-sample
exploratory runs can be repeated for comparison but are not approval evidence.

The strategies define stops and targets from sweep extremes, opposing swing liquidity, or the other support/resistance
zone. Generic stop/target grid search is disabled because it would replace those structural brackets and misrepresent
the rule being tested. Users can edit versioned strategy parameters and run a new development comparison.

Spread is a configured fixed absolute price assumption. Long entries pay spread; short exit triggers are shifted to
bid-equivalent levels, and short exits pay spread. Adverse slippage applies to entries and exits. Commission is charged
per lot on both sides. Stops precede targets in ambiguous candles; adverse stop gaps fill at the worse opening price.
The extension relies on private Backtesting.py broker methods, hence version 0.6.5 is pinned and regression-tested.
The R denominator is the proposed stop distance times filled contract quantity; its numerator includes modeled costs.

Funding is a documented **calendar-day approximation** using signed per-lot daily inputs. It is deducted from equity
and added to trade P&L. Broker rollover times, triple swaps, historical financing-rate changes, dynamic leverage,
historical currency conversion, tick queueing, latency, requotes, and liquidity are not reproduced exactly. Use explicit
conservative inputs and stress scenarios. OHLC cannot establish true tick-by-tick fills. Real demo results remain distinct.

The reference “buy_hold” experiment is a protected, risk-sized long held toward the end of the evaluation, not a
fully invested ETF benchmark. The EMA crossover baseline uses the same guarded CFD framework. Both are reference
comparisons, not evidence of strategy superiority by themselves.

Default development splits use elapsed calendar time, 60/20/20. The configured strategy rules are evaluated on
training, validation, and 12-month/3-month rolling forward windows. A candidate needs at least three forward folds
and 60% positive folds. Rank by median forward expectancy in R and then validation drawdown. Report folds, market
regime performance, and extraordinary-trade concentration. Do not choose on win rate.

Final approval requires validation screening plus positive holdout expectancy, PF >1.3, drawdown <10%, 200 distinct
validation+holdout trades, positive 1.5x/2x cost stresses, confirmed gap coverage, and largest winning trade under 20%
of gross gains. Metrics with insufficient samples or zero denominators are null; annualized return requires a year and
Sharpe/Sortino require 60 daily observations. Return ratios use daily calendar observations and zero risk-free rate.

Approval is tied to strategy parameters, broker/source, symbol specifications, costs, and dataset evidence. The final
holdout is atomically claimed before examination. Software cannot enforce honest research outside this application:
relabeling subsets or manually inspecting holdout exports compromises the evidence and must not be called untouched.

The monitor offers Signal-only and Auto execute modes, with Auto execute selected by default for the configured
DEMO or LIVE environment. Signal-only sends qualifying setups and maintains a
separate simulated forward ledger using the next broker candle, configured costs, risk-sized virtual volume, and
stop-first OHLC outcomes. Its win rate and P&L are estimates, not broker fills. Auto execute requires a fresh audited
environment-specific authorization and logs actual broker orders and deal cashflows separately. LIVE orders use real
funds; GUI warnings identify the real-money environment, and the MT5 account type must match XAU_MODE exactly.
