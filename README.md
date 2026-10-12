# Aurum — XAU/USD Research and Trading Platform

A local Windows application for MT5 gold history, versioned strategies, reproducible research,
centralized risk controls, audited MT5 demo/live execution, and a guided Streamlit dashboard.

**Auto execute is the default mode for the selected DEMO or LIVE environment. Automatic orders stay blocked until the account,
strategy, costs, risk checks, and your explicit authorization pass. LIVE is clearly marked as real-money trading
throughout the GUI. No trading profitability is implied.**
The mock broker and synthetic datasets are exclusively software test fixtures; the application never falls back to them.

## Windows users

Get `AurumLauncher.exe` from the project's release and double-click it. You do not need the source code, Python,
`.env`, or a virtual environment. You do need the MetaTrader 5 desktop terminal and your own broker login. Follow the
short [getting-started guide](docs/getting-started.md) to connect the account, run a historical simulation, and start
the market monitor. The executable keeps local settings, reports, and history in `%LOCALAPPDATA%\Aurum`.

The EXE is built from this repository with `scripts/build-launcher.ps1`; the build includes the dashboard, Python
runtime, strategies, database migrations, and MT5 connector. Users should download a release build, not run the build
script.

## Developers

Use 64-bit Python 3.14 and run `scripts/setup.ps1`. This installs the locked environment for working from source.
Developer settings belong in a local `.env`; end users configure accounts inside the app. MT5 credentials stay in
the terminal. Automatic cost estimates support exploratory backtests, while automation approval requires confirmed
broker costs.

### Developer CLI

```powershell
.\.venv\Scripts\python.exe -m app.main doctor
.\.venv\Scripts\python.exe -m app.main download-data --timeframes M15 H1
.\.venv\Scripts\python.exe -m app.main compare-strategies
.\.venv\Scripts\python.exe -m streamlit run app/dashboard/home.py
.\.venv\Scripts\python.exe -m app.main demo
.\.venv\Scripts\python.exe -m app.main status
.\.venv\Scripts\python.exe -m app.main halt
.\.venv\Scripts\python.exe -m app.main stop
```

`doctor` checks SQLite, installed packages, explicit account identity, permissions, symbol specifications, quotes,
and historical access. It never submits orders. An unavailable broker is reported explicitly.

`demo` starts the separate live-market monitor, which checks all registered strategies by default and persists broker
snapshots and UTC risk checkpoints. Opening Streamlit alone does not start monitoring; use **Start strategy monitor**
in Overview or the launcher. Automatic order permission is separate from AUTO mode and from monitor startup. The
account mutex prevents competing engine instances.
Stopping the service blocks new automatic orders and leaves protective broker orders active. A persistent halt survives restart.

**Auto execute** is the default selection for the configured account environment. It is an operating mode, not an
authorization: orders stay blocked until the account-specific authorization succeeds. **Signal only** sends new
qualifying setups to Telegram (when configured) and tracks simulated next-candle outcomes with costs, stop/target rules,
win rate, and P&L. Those simulated results are clearly separated from broker fills. Auto execute sends orders to the
verified account only after explicit authorization; DEMO uses virtual funds, while LIVE uses real money and can incur
real losses. Both environments use the same strategies, journal, risk checks, margin checks, quote filters, protective
stops, reconciliation, and account identity validation.

To obtain broker session hours, compile and run `scripts/ExportSessions.mq5` in MetaEditor/MT5. Copy the produced
`MQL5/Files/xau_sessions.json` into the project `data` folder configured by `XAU_SESSION_FILE`. Refresh daily and
after broker offset/schedule changes. Alternatively attach the read-only `scripts/SessionExporterEA.mq5` helper to a
chart and copy its refreshed export. Stale or missing schedules block automation. Neither helper submits orders.

## Research and authorization

Research defaults to the last three years available in the local database, with M15 entries.
Supported entry intervals are M1, M5, M15, and M30. Download an interval, then run the comparison for each
timeframe to assess sensitivity; the current rule sets do not add a separate higher-timeframe trend filter. History
coverage depends on the account, broker, symbol, and MT5 terminal limits; use the dashboard's coverage table before
choosing a date range. Downloaded bars remain broker candles and their gaps remain visible in Data Quality.
Screening defaults are in `configs/research.json`; supply another validated JSON file with `--policy PATH`.
Each experiment records assumptions, specifications, dataset hashes, chronological partitions, and reports.
Download limitations and unresolved gaps are visible; missing prices are never manufactured.

The **Backtesting Lab** keeps the current strategy rules and risk/cost assumptions as editable defaults. Starting
capital defaults to the latest verified account balance and can be edited for each simulation. A coverage table shows
the actual date range and candle count per stored interval. Adjust
strategy-specific structural rules, entry timeframe, risk per trade, daily/weekly loss stops, drawdown, entries, spread,
commission, slippage, swaps, simulated margin, position count, and starting capital for each experiment. Strategy
comparisons keep each strategy's structure-derived stop and target; they do not apply a generic ATR/target grid. Every saved run records its exact settings;
its report folder contains the complete trade ledger with size in broker lots, fills, costs, net P&L, and R multiple.
The dashboard shows a readable comparison, performance summary, and trades table, with CSV/HTML downloads. Worker
diagnostics go to the local `logs/research-console.log`.

```powershell
# Development comparison leaves the final holdout untouched.
.\.venv\Scripts\python.exe -m app.main compare-strategies --start 2023-10-11 --end 2026-10-12
# Final evaluation is an explicit, one-time holdout claim after development selection.
.\.venv\Scripts\python.exe -m app.main validate-strategy --strategy ict_sweep_mss_fvg --consume-holdout
```

Use the **same fixed date range and timeframe** for development and final evaluation. Run `compare-strategies`
separately for M1, M5, M15, and M30 when those data are available. The development comparison leaves the final 20%
untouched. `validate-strategy --consume-holdout` is the explicit one-time evaluation after selecting parameters.
The exploratory full-range `backtest` can be repeated to compare user-selected settings, but its full-sample results
are not approval evidence. Only the explicit validation workflow claims the untouched holdout once.

Session closures cannot be inferred from missing bars. Inspect gap evidence and broker schedule history, then use
`confirm-coverage --dataset-hash HASH --evidence PATH` only when the exact dataset's gaps are confirmed legitimate
closures. This is an auditable user attestation, not an automated completeness claim. Record unresolved history
limitations honestly; do not attest actual missing trading-session prices as closures.

No qualifying strategy means **NO STRATEGY APPROVED FOR AUTOMATION**. An exploratory run is never an approval.
Passing research does not authorize execution. See [research assumptions](docs/research.md).
Apply the selected report's exact parameters through the Strategies page while the monitor is stopped; the engine
must apply that queued configuration before it is stopped for arming. Validation does not silently activate parameters.

After actual account integration, approved research, verified period baselines, and explicit authorization for the
account you intend to use, stop the engine and run the matching command locally:

```powershell
# DEMO account: replace LOGIN with the exact configured demo login.
.\.venv\Scripts\python.exe -m app.main arm-demo --confirm-account LOGIN --confirm-first-order
# LIVE account: set XAU_MODE=LIVE and the exact real login/server first. This authorizes real-money orders.
.\.venv\Scripts\python.exe -m app.main arm-live --confirm-account LOGIN --confirm-first-live-order
.\.venv\Scripts\python.exe -m app.main demo
```

Arming submits no order itself. It requires a verified flat account of the configured environment, fresh quote/session metadata, matching
strategy/specification/cost evidence, and passing risk state. Strategy or risk changes block automatic orders until reviewed.

In the dashboard **Settings → Manage saved data**, you can remove saved backtest results, selected market-candle
timeframes or date ranges, or old local records for the currently verified demo/live account. These controls do not
change broker-side history, and preserve open positions, unresolved orders, risk baselines, and safety halts. A cleared
backtest keeps the one-time holdout-used record so the same final test cannot be repeatedly tuned.
This repository's creation does **not** authorize the first broker order.

## Verify

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check app tests migrations
.\.venv\Scripts\python.exe -m mypy app
```

The default suite needs no broker credentials and never sends MT5 orders. Separately opt in to read-only integration:

```powershell
$env:XAU_RUN_DEMO_CHECKS = '1'
.\.venv\Scripts\python.exe -m pytest -m demo tests/test_demo_integration.py
```

Reports, the local database, and logs remain on the user's computer. No GitHub publication is configured.

See [operations](docs/operations.md), [architecture](docs/architecture.md), and
[implementation/verification status](docs/implementation-status.md) for acceptance checks and remaining limitations.
