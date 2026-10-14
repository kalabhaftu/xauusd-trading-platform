# Changelog

## 0.2.0 — 2026-10-14

- Run ICT and support/resistance backtests on M1, M5, M15, M30 and H1 by default, with batch comparisons, coverage, entry audits and a portable report bundle.
- Separate realistic $1,000 / 0.25% account runs from independent minimum-lot setup studies; expose setups that exceed the account budget without treating study P&L as account returns.
- Count successful entries toward the default backtest limit of 10 per UTC day; support cooldown after consecutive losses.
- Correct opening-price equity and margin sizing, portfolio headroom, expiry, final-candle finalization and transaction-cost risk accounting.
- Fix ICT swing selection and delayed FVG confirmation; reconstruct historical S/R bounces and require sufficient contiguous candles across intervals.
- Speed up timestamp access, data gap checks, exposure accounting and report rendering.
- Verify release lint, types and offline tests on GitHub before building and self-testing the Windows executable.

Verification: 159 offline tests passed; one demo integration test excluded. Ruff and mypy passed. Full research campaign: 35 runs. See [the investigation report](docs/backtest-review-2026-10-14.md) for measured results and cost assumptions.

Research limitations: most tested results remain negative after costs. The positive ICT M30 setup study is too small and exceeds the requested account risk on 57 of 58 entries. M1 has about 103 days of history and M5 about 513 days; neither has three years. No model is automatically approved for live trading.

## 0.1.0 — 2026-10-13

Initial published Aurum release with Windows launcher packaging.
