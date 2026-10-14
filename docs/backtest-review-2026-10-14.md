# Aurum 0.2.0: backtest investigation

Completed 14 October 2026. This replaces the earlier M15-only review. Both strategies were evaluated on M1, M5, M15, M30 and H1: 10 account runs, 10 independent setup studies, and 15 rule sensitivity studies (35 runs).

## Account and setup results

Each account run starts independently at $1,000 with 0.25% equity risk, initially $2.50 per entry. Maximum 10 successful entries per UTC day, one simultaneous position, and a 60-minute cooldown after five consecutive losses. The daily, weekly and drawdown guards remain active. These are independent account experiments; their balances must not be added together.

Setup studies use the broker minimum lot of 0.01, permit overlapping positions, and disable cumulative account guards. They expose executable model setups even when their minimum lot exceeds the requested risk budget. Their dollar P&L does not represent an achievable $1,000 account return. Broker volume steps, valid protective brackets, signal expiry and observed candle execution still apply.

| Model | Interval | Previous setups | New setups | Account fills | Account net USD | Study fills | Study net USD | Study PF | Over-budget study fills |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| ICT | M1 | 101 | 203 | 20 | -31.36 | 203 | -166.51 | 0.698 | 176 |
| S/R | M1 | 3299 | 4433 | 73 | -84.59 | 4430 | -5244.59 | 0.645 | 3943 |
| ICT | M5 | 98 | 217 | 9 | -6.75 | 217 | -111.69 | 0.893 | 208 |
| S/R | M5 | 6927 | 9583 | 84 | -66.19 | 9546 | -10962.74 | 0.796 | 9367 |
| ICT | M15 | 79 | 163 | 5 | -4.97 | 163 | -199.97 | 0.797 | 158 |
| S/R | M15 | 3621 | 5455 | 93 | -100.29 | 5394 | -5559.98 | 0.806 | 5135 |
| ICT | M30 | 31 | 58 | 1 | -2.26 | 58 | 60.89 | 1.161 | 57 |
| S/R | M30 | 220 | 2194 | 18 | -23.25 | 2137 | -3318.03 | 0.781 | 2117 |
| ICT | H1 | 11 | 20 | 0 | 0.00 | 20 | -226.57 | 0.245 | 20 |
| S/R | H1 | 0 | 456 | 0 | 0.00 | 443 | -703.37 | 0.826 | 443 |

The previous setup counts come from the saved pre-change candidate baseline over the same requested evaluation interval. They compare rule output, rather than previously filled trades. More setups have been recovered; this does not establish better profitability.

Every default account run with fills lost money. H1 had setups but no account fills because the broker minimum lot could not fit the risk budget. Rejected candidates remain in entry_decisions.csv with estimated minimum-lot risk, required capital and reasons. The 0.25% setting is applied to equity and costs at entry; subsequent price gaps can make realized loss exceed the intended budget.

ICT M30 was the only default setup study with positive aggregate dollar P&L: +$60.89 across 58 trades, PF 1.161. Its average cost-adjusted R was still -0.017. Of its 58 fills, 57 exceeded the reference risk budget. This is not a profitable $1,000 account result.

| ICT M30 chronological segment | Trades | Study net USD |
| --- | ---: | ---: |
| Early 60% | 37 | -26.85 |
| Middle 20% | 12 | -49.18 |
| Late 20% | 9 | 136.93 |

The positive aggregate depends on nine late-period trades; the earlier two segments lost money. These time segments and parameter comparisons are descriptive checks on data already examined. No untouched holdout or production approval is claimed.

ICT M30 confirmation variants also had positive aggregate study P&L (zero-bar confirmation +$29.21 / 15 trades, one-bar confirmation +$69.49 / 57 trades). All other tested setup variants lost money or produced no trades. The three-bar and 32-bar S/R H1 windows produced no trades; the 32-hour contiguous requirement crosses daily market closures. No variant was automatically selected.

## Actual data coverage

Requested interval: 2023-10-14 to 2026-10-14, end exclusive, UTC. Available broker history is shorter on M1 and M5. Missing history is reported and was not fabricated.

| Interval | Evaluation candles | First candle UTC | End exclusive UTC | Calendar days |
| --- | ---: | --- | --- | ---: |
| M1 | 100521 | 2026-07-01T17:19:00+00:00 | 2026-10-13T00:00:00+00:00 | 103.3 |
| M5 | 100000 | 2025-05-14T15:30:00+00:00 | 2026-10-09T21:00:00+00:00 | 513.2 |
| M15 | 70664 | 2023-10-15T22:00:00+00:00 | 2026-10-12T17:15:00+00:00 | 1092.8 |
| M30 | 35336 | 2023-10-15T22:00:00+00:00 | 2026-10-12T14:00:00+00:00 | 1092.7 |
| H1 | 17683 | 2023-10-15T22:00:00+00:00 | 2026-10-12T17:00:00+00:00 | 1092.8 |

Source: Exness-MT5Trial9 / XAUUSDm. Cost assumptions: bid OHLC; spread 0.50 price units, slippage 0.20, commission zero, long swap -$51.64 per lot/day, short swap zero, fixed margin fraction 0.01. Costs remain marked unverified overall; zero commission and fixed spread/slippage need broker confirmation. No costs were reduced to improve reported performance. Intrabar bid/ask paths cannot be reconstructed from OHLC data.

## Correctness changes

- Default CLI and dashboard backtests run both models across all five intervals; explicit single-interval investigations remain available.
- ICT permits a three-candle fair value gap to confirm shortly after market structure shift, uses the latest opposing confirmed swing, and waits for a later completed-candle retest. Pending sequences reset across missing candles.
- S/R reconstructs completed prior bounces on each exact discovered zone, freezes a pending zone until exit, and requires sufficient contiguous history. The default 16-candle minimum makes higher intervals usable.
- Orders size against opening quote equity, margin and portfolio exposure before current-candle exits. Future close or target proceeds cannot fund an earlier entry.
- Portfolio headroom can reduce lot size instead of rejecting a size that can be reduced safely. Successful fills consume the daily quota; failed orders do not.
- Finalization cancels pending entries and closes existing positions at the final observed close, avoiding replay of the final candle to fill an order retroactively.
- Entry decisions preserve minimum-lot risk, budget, volume rounding and rejection evidence. Cost-adjusted R includes fees, spread, slippage and modeled swap.
- Timestamp caching, vector gap detection and efficient exposure accounting remove repeated pandas indexing work; report charts sample display points while retaining full ledgers.

## Verification

- Offline suite: 159 passed, one explicitly excluded demo integration test. No MT5 trading orders were sent.
- Ruff passed; mypy passed across 48 source files; git diff whitespace check passed.
- The timestamp optimization preserved complete metrics and byte-identical trade/decision ledgers on three completed M1 runs (account, setup study, and strict confirmation variant).
- A flat 5,000-candle profiling case improved from 9.695 seconds to 0.829 seconds. This is a runtime comparison, not a trading return.
- Windows release automation builds the standalone launcher from the tag and runs its embedded self-test before uploading AurumLauncher.exe.

## Research and Q&A used

The execution review used the [official backtesting API documentation](https://kernc.github.io/backtesting.py/doc/backtesting/backtesting.html), [maintainer source](https://github.com/kernc/backtesting.py/blob/master/backtesting/backtesting.py), and [session execution issue](https://github.com/kernc/backtesting.py/issues/1290). Installed framework source was also inspected for finalization behavior.

ICT sequence interpretation used the [official 2022 mentorship episode 6](https://www.youtube.com/watch?v=Bkt8B3kLATQ). The two-bar confirmation allowance is an explicit implementation choice, rather than a universal ICT requirement.

The S/R investigation used the [primary support/resistance study](https://arxiv.org/html/2101.07410), including its historical bounce counting and zone behavior. The causal trailing implementation and 16-candle minimum are adaptations; this paper is not evidence of profitability on this broker feed.

## Local evidence

Detailed batch: reports/batch_20261014T162738_5c91cd4a/report.html. Portable report bundle: reports/batch_20261014T162738_5c91cd4a/batch.zip. Summary: reports/all-interval-backtest-2026-10-14.json. Baseline: reports/multitimeframe-baseline-2026-10-14.json. Parity: reports/timestamp-parity-2026-10-14.json. Generated ledgers, broker data and large reports stay local; this review is tracked with the release.
