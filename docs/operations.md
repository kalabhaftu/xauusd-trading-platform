# Operations and troubleshooting

## Safe startup and supervision

Configure a matching MT5 DEMO or LIVE account and run doctor first. Start the monitor with automatic orders off to observe broker/account
state and establish trustworthy period resets. Verify downloaded history, actual account equity, source, timestamps,
and broker symbol contract specifications. The service scans all registered strategies by default; narrow its list on
the Strategies page if needed. Auto execute is the selected mode, not proof the monitor is running. Only a strategy with
matching research approval and account authorization can submit orders. Keep the MT5 terminal logged in and available.

Daily reset is midnight UTC; weekly reset is Monday midnight UTC. Loss uses equity changes adjusted for deposits
and withdrawals, so floating P&L, commissions, and swaps affect limits. Drawdown uses a cash-flow-adjusted equity
high-water mark. Initial startup mid-period leaves that period unverified. The service must observe both required
boundaries continuously, with checkpoints no more than 15 seconds apart. A restart across a missed boundary blocks
entries until a trustworthy later checkpoint; it does not invent midnight equity from balance alone.

Starter values are 0.25% planned risk per trade, 5% daily loss, 3% weekly loss, 10% drawdown, one open position,
three entries per session (UTC day for continuous strategies), and five consecutive losses. Users can change risk
percentages, loss limits, and open-position count in Getting Started; the app allows settings up to 100% instead of
enforcing the former 0.5% per-trade ceiling. Planned stops can be exceeded by gaps and fast-market fills.
Sizing reserves 10% of free margin and budgets adverse execution, commission, and a financing allowance.
These are modeled risk limits: gaps and broker failures can create losses larger than a stop-based budget.

Halt blocks new entries persistently. Protected positions remain open. Bot-owned positions missing
SL prompt at most three automatic closure attempts, persisted across restarts. Rejected preflights count toward this
limit. An uncertain submission blocks further attempts until reconciliation. Remaining exposure is recorded for
operator recovery; protected positions are retained. A failed or uncertain closure remains audited and
blocked; the system cannot promise closure during outages. `reset-halt` requires connectivity, fresh quotes, reconciled
state, verified baselines, and recovered risk limits. A consecutive-loss halt additionally requires a 60-minute cooldown;
explicit reset records an acknowledged loss-streak checkpoint. Reset never rearms entries.

CLI and dashboard start/stop controls are independent of the browser. Closing the dashboard does not terminate
the service. Stop prevents new entries and allows graceful shutdown. For emergency manual intervention use the verified
MT5 terminal; reconcile afterwards. Do not delete intents or halt state to force the bot to trade.

## First broker order

Implementation alone is not authorization to send the first order. Review successful read-only checks,
evidence-backed approval, costs, identity, and risk state. Select the intended account environment and obtain explicit
user authorization. Use `arm-demo` for demo or `arm-live` for a real account, then supervise the first service
execution. The GUI labels LIVE as real-money and provides an explicit authorization action. Compare deal, order,
volume, actual fill, SL/TP, commissions, and history against MT5. Record outcomes; never fabricate successful checks.

## Deployment

Use Windows Task Scheduler for unattended monitoring with the full `.venv\Scripts\python.exe` path,
arguments `-m app.main demo`, and the project as “Start in”. Configure one task instance and a restart delay.
MT5 generally needs a usable Windows user session; verify that chosen Task Scheduler/session settings actually
allow terminal connectivity. A machine sleep, reboot, or logout can interrupt the terminal and engine. Never bypass
the account mutex to work around this. Back up SQLite with its online backup facility, not a lone copy during WAL writes.

Bind Streamlit to 127.0.0.1. This milestone has no public deployment authentication, encrypted database, or multi-user
authorization. Protect the Windows account, `.env`, SQLite, reports, logs, and backups. Telegram is optional; daily
summaries and connection/execution/risk events use a persisted outbox, with capped retries and no secret disclosure.

Retention settings are recorded for future maintenance; automatic deletion of audit/research records is intentionally
not performed. Rotating JSON logs cap routine log files. SQLite candle history grows with explicitly requested downloads.

## Troubleshooting

- **MT5 missing:** install the official Windows terminal, specify its executable path, and install the Windows MT5 wheel.
- **Identity/permission mismatch:** correct the configured login/server and environment; verify actual account trade mode and external API permissions. A server name alone does not prove demo or live status.
- **No history:** increase “Max bars in chart”, load broker charts, retry download, and inspect coverage. Broker limitations cannot be repaired with synthetic prices.
- **No strategy approval:** inspect sample size, out-of-sample metrics, costs, gaps, folds, and concentration. Failure is a valid result.
- **Risk baselines missing:** keep monitoring through the UTC day/week boundary; startup balance is not prior midnight equity.
- **Session metadata stale:** re-run the read-only exporter and verify server offset. Broker holiday changes require updated evidence.
- **Unknown order result:** inspect MT5 positions and history. Preserve intent records and wait for reconciliation; never blind-resend.
- **DNS/install errors:** verify DNS and HTTPS access to pypi.org/files.pythonhosted.org. Do not disable TLS checks. Dependency resolution here needed an IPv4 workaround for unreliable local DNS.
- **Dashboard empty:** start the monitor or download data. Empty indicators are honest unavailable-state reporting.
