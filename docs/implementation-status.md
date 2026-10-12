# Implementation and observed verification

Verified locally on Windows with Python 3.14.7. A read-only MT5 check previously verified a configured demo account,
gold symbol, and terminal permissions. No broker order was submitted during that check. Account identifiers,
balances, trade history, and current safety state are kept in the local app, not version-controlled documentation.

## Phase record

| Phase | Main files delivered | Observed checks | Remaining acceptance |
| --- | --- | --- | --- |
| 1 — Foundation | `pyproject.toml`, `requirements.lock`, `.env.example`, `app/config/`, `app/domain.py`, `app/database/`, `migrations/`, `.github/workflows/ci.yml`, `scripts/setup.ps1` | Dependencies installed on Python 3.14; `pip check` clean; versioned migration/restart, SQLite integrity, secret redaction and settings tests pass; local Git initialized | CI is configured but has not run on GitHub; no remote destination supplied |
| 2 — Broker/data | `app/broker/`, `app/data/`, `scripts/ExportSessions.mq5`, `scripts/SessionExporterEA.mq5` | A configured demo account and gold contract specification were verified. History spans three years on some intervals; lower timeframes remain limited by broker/terminal history windows | Downloaded interval coverage varies by account and terminal settings. Session schedule export and gap classification remain pending; gaps are unresolved |
| 3 — Strategies | `app/strategies/`, `docs/strategies.md` | Current registry contains two versioned rules: ICT liquidity sweep → MSS → FVG retest and rolling support/resistance bounce. Deterministic rule and no-lookahead tests are added with this update. | These rules have not yet been evaluated on the configured historical dataset. Results for the retired prototypes do not qualify them |
| 4 — Research | `app/backtesting/`, `configs/research.json` | Chronological partitions, walk-forward evaluation, cost model, broker sizing, risk limits, and holdout isolation remain. Generic ATR stop/target tuning is removed because the current rules use structure-derived brackets. | Rerun the complete research workflow for the two current strategy hashes; earlier comparison results tested the retired prototypes |
| 5 — Risk/execution | `app/risk/`, `app/execution/`, `app/service.py`, `app/execution/signal_tracker.py` | AUTO is the saved default. When started, the monitor scans all registered strategies by default (users can narrow the list); only a strategy matching its own research approval and account authorization can submit orders. DEMO/LIVE identity checks, 15-minute position alerts, and broker fault fixtures are implemented. | Actual broker order lifecycle remains untested; MT5 exactly-once execution cannot be guaranteed |
| 6 — Dashboard | `app/dashboard/home.py`, `.streamlit/config.toml` | Nine pages retained; monitor status updates live, distinguishes dashboard from market monitoring, and shows selected strategies. Backtest balance/risk controls show live dollar equivalents. LIVE banner, account authorization, readable research, broker-position panel, refreshed P&L, tooltips, and scoped cleanup remain available. | Review with current persisted data; localhost only, no public multi-user authentication |
| 7 — Broker integration | `tests/test_demo_integration.py`, CLI `doctor`, `arm-demo`, and `arm-live` | Doctor verified demo identity, symbol, fresh quote, and algorithmic/external Python permissions; no order submitted; automatic orders remain blocked by persistent halt | Strategy approval, session export, complete history coverage, reconciliation/recovery checks and explicit authorization remain pending |
| 8 — Hardening/docs | `app/monitoring/`, `docs/`, `README.md`, `scripts/launcher.py` | Graceful shutdown revokes permission; standalone EXE bundles the app and passes its packaged self-test; settings and data use each Windows user's profile | Real outage/restart exercise, Telegram delivery, OS session/Task Scheduler testing and backup/recovery rehearsal |

## Final offline commands and results

- Regression tests cover AUTO/LIVE, multi-strategy monitoring, Windows cleanup safety, costs, and P&L refresh.
- Ruff checks for `app`, `tests`, migrations, and the launcher pass.
- `.venv\Scripts\python.exe -m mypy app`: **no issues in 47 source files**.
- `.venv\Scripts\python.exe -m pip check`: **no broken requirements**.
- `.venv\Scripts\python.exe -m pytest -m demo tests/test_demo_integration.py -q`: **1 skipped**; broker opt-in absent.
- `doctor` (2026-10-12): **SQLite integrity OK** and MT5 identity, quote, symbol specification, and permissions were
  verified in a read-only check; no order was submitted. Current broker status belongs in the local dashboard.
- The earlier three-year comparison tested retired prototype rules. It does not evaluate or approve the current ICT or
  support/resistance rules. Run research on the new rule hashes before any forward test.
- Full offline test suite: **119 passed, 1 deselected**; the packaged-worker lifecycle test adds **1 passed**. Ruff, MyPy
  (47 source files), dependency check, and standalone launcher build/self-test pass.

Every deterministic fixture runs against a temporary test database. The real local dashboard has no fabricated
account balances, candles, broker trades or research results. Screenshots and generated research files are local artifacts.

## Default configuration

The default is AUTO cost mode with a 0.25% per-trade risk setting and the current versioned strategy rules. Automatic
mode supplies starter spread/slippage limits and reads supported swap values from MT5. Commission is an estimate until
the user confirms broker costs; unverified assumptions cannot qualify a strategy for order authorization.

## Broker acceptance checklist

The MT5 demo account and symbol configuration, read-only connectivity, and M15/H1 plus M30/H1 history downloads are complete.
Remaining broker-dependent items are:

1. Recheck current account costs and specifications in MT5, including swap and spread assumptions.
2. Compile the read-only MQL5 helper, export fresh symbol session metadata, and classify schedule closures. Do not
   attest that real missing trading-session prices are closures.
3. Increase MT5's history limit and retry M5 if three years are needed; download other requested intervals as needed.
   M15/H1 currently cover three years, with gaps unresolved.
4. Freeze a fixed dataset/date range and timeframe, compare strategies in development, select parameters before opening holdout,
   and consume the holdout once. If all candidates fail, retain **NO STRATEGY APPROVED FOR AUTOMATION**.
5. Start signal-only monitoring through trustworthy UTC day/week reset checkpoints. Check real equity/history
   reconciliation and recovery before arming. Apply the selected report's exact parameters through the engine.
6. Obtain fresh explicit user authorization for the first broker order. Stop the service, use the audited arming command,
   then supervise execution. Compare intent, order, deal, volume, fill, SL/TP, commissions and swaps with MT5 history.
7. Rehearse disconnect/restart and persistent-halt recovery against the authorized demo account. Retain protected
   positions; verify bounded closure handling for bot positions without required SL. Record remaining exposure.

The initial research adapter supports bid-based USD linear CFDs and uses a documented calendar-day financing
approximation. It cannot reconstruct true intrabar tick order, broker-exact swaps or liquidity from OHLC. Review
`research.md` and `operations.md` before treating a passing research gate as execution evidence.
