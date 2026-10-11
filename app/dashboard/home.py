"""Run: python -m streamlit run app/dashboard/home.py"""
# ruff: noqa: E402 -- Streamlit's direct-file entrypoint needs the project path before local imports.
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

# Streamlit executes this file directly; make the project importable from its absolute path.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config.settings import Settings
from app.database.storage import Store
from app.domain import utcnow
from app.execution.journal import trade_journal
from app.indicators.core import atr, bollinger, ema, rsi
from app.main import configured
from app.monitoring.health import service_health
from app.strategies.base import Parameters
from app.strategies.registry import REGISTRY, build

st.set_page_config(page_title="Aurum • XAU Research", page_icon="◈", layout="wide")
st.markdown("""<style>.block-container{padding-top:2rem;max-width:1600px}
div[data-testid="stMetric"]{background:#19212d;padding:18px;border-radius:8px}
h1{letter-spacing:-.04em} .stCaption{color:#9aabba}</style>""", unsafe_allow_html=True)
settings = Settings()
store = Store(settings.database_url)
store.migrate()
settings = configured(store)
account = store.get("active_account", {})
account_key = f"{account.get('server')}:{account.get('login')}" if account else None
symbol = store.get("active_symbol", settings.symbol)
source = account.get("server", settings.account_server)
health = service_health(store)


def local_time(value):
    return datetime.fromisoformat(value).astimezone(ZoneInfo("Africa/Nairobi")).strftime("%Y-%m-%d %H:%M:%S EAT") \
        if value else "unavailable"

pages = ["Overview", "Market Analysis", "Strategies", "Backtesting Lab", "Trade Journal", "Risk Center",
         "System Logs", "Settings"]
st.sidebar.title("◈ AURUM")
st.sidebar.caption("XAU/USD Research & Demo")
page = st.sidebar.radio("Workspace", pages, label_visibility="collapsed")
if settings.mode == "DEMO":
    st.sidebar.success("DEMO • live execution disabled")
else:
    st.sidebar.warning(f"{settings.mode} • broker execution unavailable")
st.sidebar.caption(f"Engine: {health['state']}")
st.sidebar.caption(f"Last refresh: {local_time(health.get('data_refresh'))}")
if st.sidebar.button("Refresh persisted state"):
    st.rerun()
st.title(page)


def payload_frame(table, limit=1000):
    records = store.list(table, limit, account_key if table in ("broker_deals", "position_snapshots",
                         "equity_snapshots", "risk_decisions", "activity", "order_intents") else None)
    return pd.DataFrame([{**r["payload"], "recorded_at": r["timestamp"]} for r in records])


def display_records(table, limit=1000):
    frame = payload_frame(table, limit)
    if frame.empty:
        st.info("No persisted records available.")
    else:
        st.dataframe(frame, width="stretch", hide_index=True)
        st.download_button("Export CSV", frame.to_csv(index=False), f"{table}.csv", "text/csv", key=table)


def equity_chart():
    frame = payload_frame("equity_snapshots")
    if frame.empty:
        st.info("Broker equity snapshots appear after the demo monitoring service connects.")
        return
    frame = frame.sort_values("timestamp")
    fig = go.Figure(go.Scatter(x=frame.timestamp, y=frame.equity, name="Broker demo equity"))
    fig.update_layout(template="plotly_dark", height=340)
    st.plotly_chart(fig, width="stretch")


def candles_chart():
    timeframe = st.selectbox("Timeframe", ["M1", "M5", "M15", "M30", "H1", "H4", "D1"], index=2)
    if not symbol or not source:
        st.info("Configure and download broker history to display market charts.")
        return
    frame = store.load_candles(source, symbol, timeframe).tail(800)
    if frame.empty:
        st.info("No persisted candles for this timeframe.")
        return
    figure = make_subplots(rows=3, cols=1, shared_xaxes=True, row_heights=[.65, .17, .18], vertical_spacing=.03)
    figure.add_trace(go.Candlestick(x=frame.index, open=frame.open, high=frame.high, low=frame.low,
                                   close=frame.close, name=symbol), row=1, col=1)
    for period, color in ((50, "#45b8a0"), (200, "#d4b272")):
        figure.add_trace(go.Scatter(x=frame.index, y=ema(frame.close, period), name=f"EMA{period}",
                                   line={"color": color}), row=1, col=1)
    bands = bollinger(frame.close)
    for name in ("upper", "lower"):
        figure.add_trace(go.Scatter(x=frame.index, y=bands[name], name=f"Bollinger {name}",
                                   line={"width": 1, "color": "#64748b"}), row=1, col=1)
    figure.add_trace(go.Scatter(x=frame.index, y=atr(frame), name="ATR14"), row=2, col=1)
    figure.add_trace(go.Scatter(x=frame.index, y=rsi(frame.close), name="RSI14"), row=3, col=1)
    signals = store.list("signals", 200, account_key)
    if signals:
        values = [r["payload"] for r in signals if r["payload"].get("symbol") == symbol]
        figure.add_trace(go.Scatter(x=[v["timestamp"] for v in values], y=[v["entry"] for v in values],
                                   mode="markers", marker={"symbol": "diamond", "size": 9}, name="Signals"), row=1, col=1)
    deals = payload_frame("broker_deals")
    if not deals.empty and "price" in deals and "time" in deals:
        figure.add_trace(go.Scatter(x=pd.to_datetime(deals.time, unit="s", utc=True), y=deals.price,
                                   mode="markers", name="Broker entries/exits", marker={"symbol": "x"}), row=1, col=1)
    figure.update_layout(template="plotly_dark", height=700, xaxis_rangeslider_visible=False)
    st.plotly_chart(figure, width="stretch")
    quote = store.get("latest_quote")
    st.caption(f"Broker source: {source} • last stored candle: {frame.index[-1]} • basis: broker OHLC")
    if quote:
        age = (utcnow() - datetime.fromisoformat(quote['timestamp'])).total_seconds()
        st.caption(f"Spread: {quote['ask'] - quote['bid']:.4f} • quote: {local_time(quote['timestamp'])} • "
                   f"age: {age:.1f}s ({'fresh' if 0 <= age <= settings.quote_max_age_seconds else 'stale'})")
    windows = []
    breakout = Parameters(**store.get("strategy_parameters:session_breakout", {}))
    for name, tz, hour in (("London", "Europe/London", breakout.london_open_hour),
                           ("New York", "America/New_York", breakout.new_york_open_hour)):
        now = utcnow().astimezone(ZoneInfo(tz))
        opening = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if opening <= now < opening + pd.Timedelta(minutes=breakout.entry_window_minutes):
            windows.append(name)
    st.caption(f"Breakout entry windows: {', '.join(windows) or 'outside configured London/New York local windows'}")


def launch_service():
    if health["state"] not in ("STOPPED", "STALE", "FAILED"):
        st.warning("A service is already running or reconnecting.")
        return
    Path("logs").mkdir(exist_ok=True)
    with open("logs/service-console.log", "a", encoding="utf-8") as log:
        subprocess.Popen([sys.executable, "-m", "app.main", "demo"], cwd=ROOT,
                         stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    st.success("Service launch requested. Refresh to inspect startup verification.")


if page == "Overview":
    st.caption("Persisted broker demo state • values are never replaced with simulated balances")
    if not account:
        st.info("No verified account snapshot. Run doctor after configuring the MT5 demo account.")
    columns = st.columns(4)
    for column, label, key in zip(columns, ["Balance", "Equity", "Free margin", "Floating P&L"],
                                  ["balance", "equity", "free_margin", "floating"]):
        value = account.get(key) if key != "floating" else account.get("equity", 0) - account.get("balance", 0)
        column.metric(label, f"{value:,.2f} {account.get('currency', '')}" if account and value is not None else "—")
    risk = store.get(f"risk:{account_key}", {})
    left, right = st.columns(2)
    left.metric("Daily P&L (UTC)", f"{risk.get('daily_pnl', 0):,.2f}" if risk else "—")
    right.metric("Weekly P&L (UTC)", f"{risk.get('weekly_pnl', 0):,.2f}" if risk else "—")
    st.caption(f"Active strategy: {health.get('strategy', settings.active_strategy)} • "
               f"Entries armed: {store.get(f'armed:{account_key}', {}).get('active', False)}")
    exposure = store.get(f"exposure:{account_key}", {})
    st.caption(f"Current broker stop risk: {exposure.get('stop_risk_amount', 'unavailable')} "
               f"{account.get('currency', '')} • snapshot: {account.get('timestamp', 'unavailable')}")
    start_control, stop_control = st.columns(2)
    if start_control.button("Start demo monitoring service"):
        launch_service()
    if stop_control.button("Stop service gracefully"):
        store.enqueue("stop")
        st.success("Stop queued; protective broker orders remain active.")
    equity_chart()
    state = store.get(f"reconciliation:{account_key}", {})
    st.subheader("Open positions")
    st.dataframe(pd.DataFrame(state.get("positions", [])), hide_index=True)
    st.subheader("Latest signal")
    latest = store.get("latest_signal")
    if latest:
        st.json(latest)
    else:
        st.caption("No persisted strategy signal.")
    st.subheader("Recent activity")
    display_records("activity", 30)
elif page == "Market Analysis":
    candles_chart()
elif page == "Strategies":
    name = st.selectbox("Strategy", list(REGISTRY))
    existing = store.get(f"strategy_parameters:{name}", {})
    strategy = build(name, existing)
    st.write(strategy.description)
    approval = store.get(f"approval:{strategy.config_hash}", {})
    st.caption(f"Validation: {'APPROVED' if approval.get('approved') else 'NOT APPROVED'} • {strategy.config_hash[:16]}")
    parameters = st.text_area("Parameters (validated JSON)", json.dumps(strategy.parameters.model_dump(), indent=2),
                              height=350)
    if st.button("Save parameters and select strategy"):
        try:
            validated = Parameters(**json.loads(parameters))
            build(name, validated.model_dump())
            store.enqueue("select-strategy", {"name": name, "parameters": validated.model_dump()})
            st.success("Validated configuration queued. Applying it disarms entries and requires matching approval.")
        except Exception as exc:
            st.error(str(exc))
    st.subheader("Research comparisons")
    display_records("backtest_results", 30)
elif page == "Backtesting Lab":
    st.caption("Historical simulation • research runs in an independent worker process")
    name = st.selectbox("Strategy", list(REGISTRY))
    operation = st.selectbox("Operation", ["backtest", "compare-strategies", "validate-strategy"])
    timeframe = st.selectbox("Entry timeframe", ["M5", "M15", "M30"], index=1)
    trend_timeframe = st.selectbox("Trend timeframe", ["H1", "H4"])
    first, second = st.columns(2)
    start = first.date_input("Start", value=(utcnow() - pd.Timedelta(days=365 * 3)).date())
    end = second.date_input("End (exclusive)", value=utcnow().date())
    capital = st.number_input("Initial simulated capital (USD)", min_value=100.0, value=10000.0, step=100.0)
    spread = st.number_input("Modeled spread in gold price units", min_value=0.0, value=settings.max_spread or 0.0)
    st.caption("Commission, slippage, financing and contract specifications come from validated local configuration.")
    if st.button("Run research job"):
        if not source or not symbol or start >= end:
            st.error("Broker source, symbol, and a valid date range are required.")
        else:
            Path("logs").mkdir(exist_ok=True)
            command = [sys.executable, "-m", "app.main", operation, "--strategy", name, "--source", source,
                       "--symbol", symbol, "--start", start.isoformat(), "--end", end.isoformat(),
                       "--capital", str(capital), "--spread", str(spread)]
            command.extend(["--timeframe", timeframe, "--trend-timeframe", trend_timeframe])
            with open("logs/research-console.log", "a", encoding="utf-8") as log:
                process = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=log,
                                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            st.success(f"Research worker started (PID {process.pid}); refresh for persisted results.")
    st.caption("Final holdout consumption requires the explicit CLI --consume-holdout action.")
    st.subheader("Dataset quality")
    display_records("data_quality", 10)
    st.subheader("Results and reports")
    results = store.list("backtest_results", 30)
    if not results:
        st.info("No research results available.")
    for record in results:
        with st.expander(record["timestamp"]):
            st.json(record["payload"])
            report = record["payload"].get("report") or record["payload"].get("holdout_report") \
                or record["payload"].get("summary_report")
            if report:
                folder = Path(report).resolve()
                if folder.is_relative_to(settings.report_dir.resolve()):
                    equity_path = folder / "equity.csv"
                    if equity_path.is_file() and st.checkbox("Show equity and drawdown", key=f"chart_{record['id']}"):
                        equity_data = pd.read_csv(equity_path, index_col=0, parse_dates=True)
                        summary_path = folder / "summary.json"
                        summary = json.loads(summary_path.read_text(encoding="utf-8"))
                        initial = summary["metrics"]["initial_capital"]
                        equity = equity_data.Equity
                        figure = make_subplots(rows=2, cols=1, shared_xaxes=True,
                                               subplot_titles=["Historical equity (USD)", "Drawdown (%)"])
                        figure.add_trace(go.Scatter(x=equity.index, y=equity, name="Equity"), row=1, col=1)
                        figure.add_trace(go.Scatter(x=equity.index,
                                                   y=100 * (1 - equity / equity.cummax().clip(lower=initial)),
                                                   name="Drawdown %", fill="tozeroy"), row=2, col=1)
                        figure.update_layout(template="plotly_dark", height=500)
                        st.plotly_chart(figure, width="stretch")
                    for filename in ("report.html", "trades.csv", "equity.csv", "summary.json",
                                     "comparisons.csv", "sensitivity.csv"):
                        path = folder / filename
                        if path.is_file():
                            st.download_button(filename, path.read_bytes(), filename, key=f"{record['id']}_{filename}")
elif page == "Trade Journal":
    st.caption("Actual broker deals, followed by order intents and their approval/rejection audit trail")
    journal = pd.DataFrame(trade_journal(store, account_key, settings.magic)) if account_key else pd.DataFrame()
    if not journal.empty:
        strategy_filter = st.multiselect("Strategies", sorted(journal.strategy.unique()),
                                         default=sorted(journal.strategy.unique()))
        direction_filter = st.multiselect("Directions", ["LONG", "SHORT"], default=["LONG", "SHORT"])
        status_filter = st.multiselect("Position status", sorted(journal.status.unique()),
                                       default=sorted(journal.status.unique()))
        dates = pd.to_datetime(journal.entry_time, utc=True).dt.date
        first, second = st.columns(2)
        after = first.date_input("Entries from (UTC)", dates.min())
        before = second.date_input("Entries through (UTC)", dates.max())
        journal = journal.loc[journal.strategy.isin(strategy_filter) & journal.direction.isin(direction_filter)
                              & journal.status.isin(status_filter) & (dates >= after) & (dates <= before)]
        st.dataframe(journal, width="stretch", hide_index=True)
        st.download_button("Export trade journal", journal.to_csv(index=False), "trade-journal.csv", "text/csv")
    else:
        st.info("No bot-owned broker position lifecycles available.")
    st.subheader("Broker deal records")
    display_records("broker_deals")
    st.subheader("Order lifecycle")
    display_records("order_intents")
    st.subheader("Risk decisions")
    display_records("risk_decisions")
elif page == "Risk Center":
    exposure = store.get(f"exposure:{account_key}", {})
    risk = store.get(f"risk:{account_key}", {})
    halt = store.get(f"halt:{account_key}", {})
    armed = store.get(f"armed:{account_key}", {}).get("active", False)
    st.caption(f"Entries: {'ARMED' if armed else 'DISARMED'} • daily/weekly resets use UTC")
    if halt.get("active"):
        st.error(f"Persistent halt: {halt.get('reason', 'Safety verification required')}")
    if not risk or not exposure:
        st.info("No verified risk or exposure snapshot. Start disarmed monitoring after configuring the demo account.")
    columns = st.columns(3)
    for column, label, key in zip(columns, ["Daily loss", "Weekly loss", "Equity drawdown"],
                                  ["daily_loss", "weekly_loss", "drawdown"]):
        column.metric(label, f"{risk[key]:.2%}" if key in risk else "—")
    left, right = st.columns(2)
    amount = exposure.get("stop_risk_amount")
    left.metric("Open position stop risk", f"{amount:,.2f} {account.get('currency', '')}" if amount is not None else "—")
    right.metric("Consecutive losses", str(risk["consecutive_losses"]) if "consecutive_losses" in risk else "—")
    st.caption(f"Period baselines: {'verified' if risk.get('baselines_verified') else 'unverified — entries blocked'}")
    st.subheader("Configured safeguards")
    limits = [
        ("Risk per trade / hard maximum", f"{settings.risk_per_trade:.2%} / {settings.hard_risk_max:.2%}"),
        ("Daily / weekly loss limit", f"{settings.daily_loss_limit:.1%} / {settings.weekly_loss_limit:.1%}"),
        ("Equity drawdown limit", f"{settings.drawdown_limit:.1%}"),
        ("Combined position risk", f"{settings.portfolio_risk_limit:.2%}"),
        ("Maximum open positions", str(settings.max_positions)),
        ("Maximum spread", f"{settings.max_spread} gold price units"),
        ("Allowed slippage", f"{settings.max_slippage_points} broker points"),
        ("Maximum quote age", f"{settings.quote_max_age_seconds} seconds"),
        ("Entries per session", str(settings.max_orders_session)),
        ("Consecutive-loss halt / cooldown", f"{settings.consecutive_loss_limit} losses / {settings.loss_cooldown_minutes} minutes"),
        ("Automatic emergency closure attempts", str(settings.emergency_close_attempts)),
    ]
    st.dataframe(pd.DataFrame(limits, columns=["Control", "Configured limit"]), hide_index=True, width="stretch")
    with st.expander("Persisted safety details"):
        st.json({"exposure": exposure, "risk": risk, "halt": halt})
    if st.button("EMERGENCY HALT — persistently block entries", type="primary", disabled=not account_key):
        assert account_key is not None
        from app.risk.circuit_breaker import CircuitBreaker
        CircuitBreaker(store, account_key).trip("Operator dashboard emergency halt")
        store.enqueue("halt")
        st.success("Persistent halt recorded; entries disarmed.")
    if st.button("Request halt reset after recovery", disabled=not account_key):
        store.enqueue("reset-halt")
        st.info("Reset queued; backend safety checks must pass. Reset does not arm entries.")
    positions = store.get(f"reconciliation:{account_key}", {}).get("positions", [])
    tickets = [p["ticket"] for p in positions if p["magic"] == settings.magic]
    if tickets:
        ticket = st.selectbox("Bot-owned position", tickets)
        confirmed = st.checkbox("I confirm closing this position at the available market price")
        if st.button("Request position closure", disabled=not confirmed):
            store.enqueue("close-position", {"ticket": ticket, "confirmed": True})
            st.success("Closure queued for identity and broker verification.")
    display_records("risk_decisions", 100)
elif page == "System Logs":
    table = st.selectbox("Event category", ["activity", "connection_events", "service_events", "errors",
                                          "circuit_breaker_events", "notifications"])
    needle = st.text_input("Filter text")
    frame = payload_frame(table)
    if needle and not frame.empty:
        frame = frame.loc[frame.astype(str).apply(lambda row: row.str.contains(needle, regex=False).any(), axis=1)]
    st.dataframe(frame, width="stretch", hide_index=True)
    st.download_button("Export logs", frame.to_csv(index=False), "events.csv", "text/csv")
elif page == "Settings":
    st.caption("Passwords and tokens are configured locally in .env and never displayed here.")
    st.json(settings.public())
    with st.expander("Demo broker selection — requires engine shutdown and restart"):
        terminal = st.text_input("MT5 terminal executable", value=settings.mt5_path or "")
        login = st.number_input("Expected DEMO login", min_value=1, value=settings.account_login or 1, step=1)
        server = st.text_input("Exact expected demo server", value=settings.account_server or "")
        selected_symbol = st.text_input("Gold symbol (including broker suffix)", value=settings.symbol or "")
        if st.button("Queue broker change and stop engine"):
            payload = {"mt5_path": terminal, "account_login": int(login), "account_server": server,
                       "symbol": selected_symbol or None}
            try:
                candidate = Settings(**{**settings.model_dump(), **payload})
                candidate.require_demo()
                store.enqueue("configure-broker", payload)
                st.info("Validated change queued. The engine saves it, disarms entries, and stops; restart and run doctor.")
            except Exception as exc:
                st.error(str(exc))
    risk = st.number_input("Equity risk per trade (%)", min_value=.01, max_value=.5,
                           value=settings.risk_per_trade * 100, step=.01)
    spread = st.number_input("Maximum spread (price units)", min_value=.01, value=settings.max_spread or 1.0)
    deviation = st.number_input("Maximum slippage deviation (broker points)", min_value=0,
                               value=settings.max_slippage_points or 0, step=1)
    if st.button("Queue validated settings"):
        store.enqueue("configure", {"risk_per_trade": risk / 100, "max_spread": spread,
                                     "max_slippage_points": int(deviation)})
        st.success("Settings queued. Changes disarm entries until explicitly reauthorized.")
    st.subheader("Control command status")
    display_records("commands", 30)
