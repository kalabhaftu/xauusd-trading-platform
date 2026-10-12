"""Run: python -m streamlit run app/dashboard/home.py"""
# ruff: noqa: E402 -- Streamlit's direct-file entrypoint needs the project path before local imports.
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import get_args
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots
from sqlalchemy import func, select

# Streamlit executes this file directly; make the project importable from its absolute path.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config.settings import Settings
from app.config.user_setup import save_telegram_credentials, save_user_setup
from app.database.models import candles as candle_table
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


def remove_saved_reports(paths):
    """Remove only report folders that resolve inside the configured reports directory."""
    root = settings.report_dir.resolve()
    removed = 0
    failures = []
    for value in paths:
        candidate = Path(value).resolve()
        if candidate == root or not candidate.is_relative_to(root):
            continue
        if not candidate.exists():
            continue
        try:
            if candidate.is_dir():
                shutil.rmtree(candidate)
            else:
                candidate.unlink()
            removed += 1
        except OSError as exc:
            failures.append(f"{candidate.name}: {exc}")
    return removed, failures

pages = ["Getting Started", "Overview", "Market Analysis", "Backtesting Lab", "Trade Journal", "Strategies",
         "Risk Center", "System Logs", "Settings"]
st.sidebar.title("◈ AURUM")
st.sidebar.caption("XAU/USD research and trading")
setup_complete = bool(store.get("setup_complete", False))
page = st.sidebar.selectbox("Workspace", pages, index=1 if setup_complete else 0, key="workspace",
                            label_visibility="collapsed", help="Choose a dashboard page.")
if settings.mode == "DEMO":
    st.sidebar.success("DEMO ACCOUNT • simulated funds")
elif settings.mode == "LIVE":
    st.sidebar.error("LIVE ACCOUNT • REAL MONEY • losses are real")
else:
    st.sidebar.info("BACKTEST • simulated orders only")
selected_stored_mode = store.get(f"execution_mode:{account_key}") if account_key else None
visible_mode = selected_stored_mode if selected_stored_mode in ("SIGNAL_ONLY", settings.auto_execution_mode) \
    else settings.auto_execution_mode if settings.mode in ("DEMO", "LIVE") else "SIGNAL_ONLY"
st.sidebar.caption(f"Service: {health['state']} • Mode: "
                   f"{visible_mode}")
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


PARAMETER_GROUPS = {
    "Timeframes": ("entry_timeframe", "trend_timeframe"),
    "Stops and exits": ("atr_period", "stop_atr", "reward_risk", "max_holding_hours"),
    "Trend pullback": ("ema_fast", "ema_slow", "pullback_ema", "pullback_atr"),
    "Mean reversion": ("band_period", "band_std", "rsi_period", "rsi_low", "rsi_high", "adx_period", "adx_max"),
    "Session breakout": ("breakout_buffer_atr", "london_open_hour", "new_york_open_hour",
                         "pre_session_minutes", "entry_window_minutes", "breakout_holding_hours",
                         "retest_max_bars", "retest"),
    "Volatility filter": ("volatility_min_ratio", "volatility_max_ratio"),
}
PARAMETER_HELP = {
    "entry_timeframe": "Candles used to create entries. Only downloaded periods are available in Backtesting Lab.",
    "trend_timeframe": "Completed higher-timeframe candles used to confirm direction.",
    "stop_atr": "Stop distance as a multiple of ATR. Default is 1.5 ATR. Strategy comparisons can search the stop/target grid instead.",
    "reward_risk": "Target distance divided by stop distance. Default is 2R. Strategy comparisons can search the stop/target grid instead.",
    "pullback_atr": "Maximum distance from the entry EMA, measured in ATR.",
    "breakout_buffer_atr": "Distance beyond the session range required before a breakout signal.",
    "pre_session_minutes": "Length of the completed range immediately before each session opens.",
    "entry_window_minutes": "How long after the local session open new breakout entries are allowed.",
    "breakout_holding_hours": "Maximum hold time for a breakout position before the time exit.",
    "retest": "Wait for price to return to the broken range edge before signaling.",
    "max_holding_hours": "General position time exit used when a strategy has no earlier exit rule.",
    "volatility_min_ratio": "Minimum ATR divided by its recent rolling median.",
    "volatility_max_ratio": "Maximum ATR divided by its recent rolling median.",
}


def strategy_parameter_inputs(name, existing, key_prefix, include_timeframes=True):
    """Render typed editable rule controls; Pydantic remains the final validator."""
    current = build(name, existing).parameters
    values = current.model_dump()
    fields = Parameters.model_fields
    with st.expander("Strategy rules and filters", expanded=False):
        tabs = st.tabs(list(PARAMETER_GROUPS))
        for tab, (group, field_names) in zip(tabs, PARAMETER_GROUPS.items()):
            with tab:
                columns = st.columns(2)
                visible = [field for field in field_names
                           if include_timeframes or field not in ("entry_timeframe", "trend_timeframe")]
                for index, field_name in enumerate(visible):
                    definition = fields[field_name]
                    label = field_name.replace("_", " ").title()
                    help_text = PARAMETER_HELP.get(field_name, f"Strategy rule setting for {label.lower()}.")
                    widget = columns[index % 2]
                    options = get_args(definition.annotation)
                    if options:
                        value = widget.selectbox(label, options, index=options.index(values[field_name]),
                                                 key=f"{key_prefix}_{field_name}", help=help_text)
                    elif isinstance(values[field_name], bool):
                        value = widget.checkbox(label, value=values[field_name],
                                                key=f"{key_prefix}_{field_name}", help=help_text)
                    elif isinstance(values[field_name], int):
                        lower_int, upper_int = 0, 10000
                        for constraint in definition.metadata:
                            for attr, bound in (("ge", "lower"), ("gt", "lower"),
                                                ("le", "upper"), ("lt", "upper")):
                                limit = getattr(constraint, attr, None)
                                if limit is not None:
                                    if bound == "lower":
                                        lower_int = max(lower_int, int(limit) + (1 if attr == "gt" else 0))
                                    else:
                                        upper_int = min(upper_int, int(limit) - (1 if attr == "lt" else 0))
                        value = widget.number_input(label, min_value=lower_int, max_value=upper_int,
                                                    value=values[field_name], step=1,
                                                    key=f"{key_prefix}_{field_name}", help=help_text)
                    else:
                        lower, upper = 0.0, max(10.0, float(values[field_name]) * 10)
                        for constraint in definition.metadata:
                            for attr, bound in (("ge", "lower"), ("gt", "lower"),
                                                ("le", "upper"), ("lt", "upper")):
                                limit = getattr(constraint, attr, None)
                                if limit is not None:
                                    if bound == "lower":
                                        lower = max(lower, float(limit) + (0.0001 if attr == "gt" else 0))
                                    else:
                                        upper = min(upper, float(limit) - (0.0001 if attr == "lt" else 0))
                        if lower >= float(values[field_name]):
                            lower = 0.0
                        if upper <= float(values[field_name]):
                            upper = max(float(values[field_name]) * 2, 1.0)
                        value = widget.number_input(label, min_value=lower, max_value=upper,
                                                    value=float(values[field_name]), step=0.1,
                                                    key=f"{key_prefix}_{field_name}", help=help_text)
                    values[field_name] = value
    return values


def display_metrics(metrics):
    columns = st.columns(4)
    columns[0].metric("Net return", f"{metrics.get('total_net_return', 0):.2%}")
    columns[1].metric("Win rate", f"{metrics['win_rate']:.1%}" if metrics.get("win_rate") is not None else "—")
    columns[2].metric("Net P&L", f"{metrics.get('final_equity', 0) - metrics.get('initial_capital', 0):,.2f} USD")
    columns[3].metric("Trades", f"{metrics.get('total_trades', 0):,}")
    details = pd.DataFrame([{"Metric": label, "Result": str(value) if value is not None else "—"} for label, value in (
        ("Profit factor", metrics.get("profit_factor")),
        ("Expectancy (USD / trade)", metrics.get("expectancy")),
        ("Expectancy (R / trade)", metrics.get("expectancy_r")),
        ("Maximum drawdown", metrics.get("maximum_drawdown")),
        ("Average win", metrics.get("average_win")), ("Average loss", metrics.get("average_loss")),
        ("Transaction costs", metrics.get("estimated_transaction_costs")),
        ("Sample start", metrics.get("sample_start")), ("Sample end", metrics.get("sample_end")),
    )])
    st.dataframe(details, width="stretch", hide_index=True)


def flatten_values(values, prefix=""):
    rows = []
    if isinstance(values, dict):
        for key, value in values.items():
            child = f"{prefix} / {key}" if prefix else str(key)
            rows.extend(flatten_values(value, child))
    elif values is not None:
        rows.append({"Setting": prefix, "Value": str(values)})
    return rows


def available_timeframes():
    if not source or not symbol:
        return []
    with store.engine.connect() as connection:
        found = set(connection.execute(select(candle_table.c.timeframe).where(
            candle_table.c.source == source, candle_table.c.symbol == symbol).distinct()).scalars())
    return [tf for tf in ("M1", "M5", "M15", "M30", "H1", "H4", "D1") if tf in found]


def equity_chart():
    frame = payload_frame("equity_snapshots")
    if frame.empty:
        st.info("Broker equity snapshots appear after the market monitor connects.")
        return
    frame = frame.sort_values("timestamp")
    fig = go.Figure(go.Scatter(x=frame.timestamp, y=frame.equity, name="Broker demo equity"))
    fig.update_layout(template="plotly_dark", height=340)
    st.plotly_chart(fig, width="stretch")


def candles_chart():
    if not symbol or not source:
        st.info("Configure and download broker history to display market charts.")
        return
    choices = available_timeframes()
    if not choices:
        st.info("No broker candles are stored yet. Download history from the Backtesting Lab instructions.")
        return
    preferred = "M15" if "M15" in choices else choices[0]
    timeframe = st.selectbox("Downloaded timeframe", choices, index=choices.index(preferred))
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


def launch_authorization(environment, login):
    if health["state"] not in ("STOPPED", "STALE", "FAILED"):
        st.error("Stop the trading monitor and wait until it is stopped before authorizing execution.")
        return
    command = "arm-live" if environment == "LIVE" else "arm-demo"
    confirmation_flag = "--confirm-first-live-order" if environment == "LIVE" else "--confirm-first-order"
    Path("logs").mkdir(exist_ok=True)
    with open("logs/authorization-console.log", "a", encoding="utf-8") as log:
        subprocess.Popen([sys.executable, "-m", "app.main", command, "--confirm-account", str(login),
                          confirmation_flag], cwd=ROOT, stdout=log, stderr=log,
                         creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    st.info("Authorization checks started. No order is submitted by this action; check System Logs for the result.")


def check_broker_connection(candidate=None):
    from app.main import doctor

    result = doctor(candidate or configured(store), store)
    store.set("last_doctor_check", result)
    return result


if page == "Getting Started":
    st.subheader("Set up your account once")
    st.write("Choose the MT5 account, strategy, trading style, and risk limits here. Settings are saved on this PC; "
             "you do not need to edit configuration files.")
    if health["state"] not in ("STOPPED", "STALE", "FAILED"):
        st.warning("Stop the trading monitor before changing account or risk settings.")

    environment = st.selectbox(
        "Account type", ["DEMO", "LIVE"], index=1 if settings.mode == "LIVE" else 0,
        format_func=lambda value: "Demo account • simulated funds" if value == "DEMO" else "Live account • REAL MONEY",
        help="Choose the account already logged into MT5. This setting does not convert a demo account into a live one.",
    )
    same_environment = environment == settings.mode
    login_text = st.text_input(
        "MT5 account number", value=str(settings.account_login or "") if same_environment else "",
        key=f"setup_login_{environment}", help="The number shown in MT5 next to the account name. It is saved locally.",
    )
    account_server = st.text_input(
        "Exact MT5 server name", value=(settings.account_server or "") if same_environment else "",
        key=f"setup_server_{environment}", help="Copy the server exactly as MT5 shows it, including any suffix.",
    )
    selected_symbol = st.text_input(
        "Gold symbol", value=(settings.symbol or symbol or "") if same_environment else "",
        key=f"setup_symbol_{environment}", help="The exact Market Watch name, including a broker suffix such as m.",
    )
    terminal_path = st.text_input(
        "MT5 terminal (optional)", value=settings.mt5_path or "", key="setup_mt5_path",
        help="Leave blank to let the MT5 Python package find the open terminal automatically.",
    )
    if environment == "LIVE":
        st.error("LIVE ACCOUNT • REAL MONEY. Losses come from your real balance. Setup alone sends no orders.")
        live_ack = st.checkbox(
            "I understand this is a real-money account and Auto execute can place real orders after separate checks.",
            key="setup_live_ack",
            help="This confirms the account choice only. It does not authorize orders or turn on the monitor.",
        )
    else:
        live_ack = True

    strategy_name = st.selectbox(
        "Primary strategy for execution", list(REGISTRY), index=list(REGISTRY).index(settings.active_strategy)
        if settings.active_strategy in REGISTRY else 0,
        help="The monitor checks every strategy selected on the Strategies page. This primary strategy is the configuration used for account authorization; each rule set still needs its own research approval.",
    )
    auto_mode = f"AUTO_{environment}"
    mode_choices = ["SIGNAL_ONLY", auto_mode]
    selected_mode = st.radio(
        "How should signals be handled?", mode_choices, index=1,
        format_func=lambda value: "Send signals and track simulated outcomes" if value == "SIGNAL_ONLY"
        else "Auto execute • Demo account" if value == "AUTO_DEMO" else "Auto execute • LIVE real money",
        horizontal=True,
        help="Auto execute is the saved default. Actual order entry remains blocked until broker, risk, strategy, and account checks pass and you authorize it.",
    )

    st.subheader("Risk limits")
    first, second, third = st.columns(3)
    risk_pct = first.number_input("Risk per trade (% equity)", min_value=.01, max_value=.5,
                                  value=settings.risk_per_trade * 100, step=.01,
                                  help="Maximum planned loss at the stop for one new trade. Default: 0.25%.")
    hard_pct = second.number_input("Hard cap per trade (%)", min_value=.01, max_value=.5,
                                   value=settings.hard_risk_max * 100, step=.01,
                                   help="Absolute per-trade risk ceiling. Position size is rounded down to the broker lot step.")
    portfolio_pct = third.number_input("Combined open risk (%)", min_value=.01, max_value=2.0,
                                       value=settings.portfolio_risk_limit * 100, step=.01,
                                       help="Maximum equity risk across open positions and a proposed trade.")
    first, second, third = st.columns(3)
    daily_pct = first.number_input("Daily loss stop (%)", min_value=.1, max_value=10.0,
                                   value=settings.daily_loss_limit * 100, step=.1,
                                   help="Blocks entries after this loss from the 00:00 UTC daily equity baseline.")
    weekly_pct = second.number_input("Weekly loss stop (%)", min_value=.1, max_value=20.0,
                                     value=settings.weekly_loss_limit * 100, step=.1,
                                     help="Blocks entries after this loss from Monday 00:00 UTC.")
    drawdown_pct = third.number_input("Maximum drawdown (%)", min_value=.1, max_value=50.0,
                                      value=settings.drawdown_limit * 100, step=.1,
                                      help="Blocks entries when equity falls this far from its recorded high-water mark.")
    first, second, third = st.columns(3)
    max_spread = first.number_input("Maximum spread (gold price units)", min_value=.001,
                                    value=float(settings.max_spread or .5), step=.05, format="%.3f",
                                    help="Rejects an entry when the live bid/ask spread exceeds this limit.")
    max_slippage = second.number_input("Maximum slippage (MT5 points)", min_value=0,
                                       value=int(settings.max_slippage_points or 0), step=1,
                                       help="Maximum price deviation MT5 may accept from the requested price.")
    entries_per_session = third.number_input("Entries per session", min_value=1, max_value=100,
                                             value=settings.max_orders_session, step=1,
                                             help="Limits new entries in each strategy session; continuous strategies use UTC days.")

    with st.expander("More risk and cost settings", expanded=False):
        first, second, third = st.columns(3)
        consecutive_losses = first.number_input("Losses before halt", min_value=1, max_value=20,
                                                value=settings.consecutive_loss_limit, step=1,
                                                help="A persistent safety halt is recorded after this many net losing trades in a row.")
        cooldown = second.number_input("Loss cooldown (minutes)", min_value=1, max_value=1440,
                                       value=settings.loss_cooldown_minutes, step=5,
                                       help="Minimum wait after a loss-triggered halt before safety checks may reset it.")
        close_attempts = third.number_input("Emergency close attempts", min_value=1, max_value=10,
                                            value=settings.emergency_close_attempts, step=1,
                                            help="Bounded retries for bot-owned positions that have no protective stop.")
        first, second, third, fourth = st.columns(4)
        commission = first.number_input("Commission per lot", min_value=0.0,
                                        value=float(settings.commission_per_lot or 0), step=1.0,
                                        help="Round-trip account-currency commission per lot used in backtests and signal tracking.")
        modeled_slippage = second.number_input("Modeled slippage (price units)", min_value=0.0,
                                               value=float(settings.slippage_price or 0), step=.01, format="%.3f",
                                               help="Adverse price movement charged to simulated entry and exit fills.")
        swap_long = third.number_input("Long financing per lot/day", value=float(settings.swap_long_per_lot_day or 0),
                                       step=1.0, help="Broker's daily financing amount for a long position.")
        swap_short = fourth.number_input("Short financing per lot/day", value=float(settings.swap_short_per_lot_day or 0),
                                         step=1.0, help="Broker's daily financing amount for a short position.")
        session_path = st.text_input("Trading session schedule file (optional)",
                                     value=str(settings.session_file or ""),
                                     help="A current broker-exported schedule is required before the bot can authorize entries.")
        telegram_defaults = store.get("telegram_preferences", {})
        telegram_configured = bool(settings.telegram_token and settings.telegram_chat_id)
        notifications_on = st.checkbox("Send Telegram updates", value=telegram_defaults.get("enabled", telegram_configured),
                                       help="Safety alerts and selected trading events are sent by the background notification worker.")
        default_events = telegram_defaults.get("events", ["signal", "position_open", "position_update",
            "connection_loss", "connection_recovery", "trading_permission_lost", "trading_permission_restored",
            "circuit_breaker", "stop_loss", "take_profit", "trade_exit", "daily_summary", "startup", "shutdown"])
        event_groups = [
            ("New trade signals", ["signal"], "Send the strategy, direction, entry, stop, target, and rationale."),
            ("Open-position alerts and updates", ["position_open", "position_update", "position_closed"],
             "Send open and closed notices, plus floating P&L updates at the selected interval."),
            ("Connection and MT5 permission changes", ["connection_loss", "connection_recovery",
             "trading_permission_lost", "trading_permission_restored"],
             "Tell you when MT5 or market data becomes unavailable, and when it recovers."),
            ("Safety halts and trade exits", ["circuit_breaker", "stop_loss", "take_profit", "trade_exit"],
             "Notify you about risk halts and recorded bot trade exits."),
            ("Daily account summary", ["daily_summary"], "Send one daily UTC profit/loss summary."),
            ("Service start and stop", ["startup", "shutdown"], "Notify when the monitor starts or stops."),
        ]
        selected_events = []
        for label, group, hint in event_groups:
            enabled = any(event in default_events for event in group)
            if st.checkbox(label, value=enabled, help=hint):
                selected_events.extend(group)
        position_interval = st.select_slider(
            "Position update frequency (minutes)", options=[5, 10, 15, 30, 60],
            value=int(telegram_defaults.get("position_update_minutes", 15)),
            help="How often Telegram repeats status for each open bot-owned position; the first open notice is immediate.",
        )
        with st.expander("Telegram bot connection (optional)"):
            st.caption("Credentials are saved in a local app settings file on this PC and never shown again.")
            if telegram_configured:
                st.success("A Telegram bot is configured. Leave both fields blank to keep it.")
            telegram_token = st.text_input("Bot token", type="password", value="", key="setup_telegram_token",
                                           placeholder="Leave blank to keep the saved token",
                                           help="Create or copy the token from BotFather. Do not paste it into chat.")
            telegram_chat = st.text_input("Destination chat ID", value="", key="setup_telegram_chat",
                                          placeholder="Leave blank to keep the saved chat",
                                          help="The numeric chat ID where this bot should send private status updates.")
        if notifications_on and not telegram_configured and not (telegram_token.strip() and telegram_chat.strip()):
            st.warning("Telegram updates are selected, but no bot is connected. Add both credentials below to receive them.")
        st.caption("The broker's current commission and swap rates can change. Check them in MT5 before approving research.")

    if st.button("Save setup and check MT5 connection", type="primary",
                 disabled=health["state"] not in ("STOPPED", "STALE", "FAILED"),
                 help="Saves these settings locally and runs read-only account/data checks. This button does not place an order."):
        try:
            if not login_text.strip().isdigit() or int(login_text) < 1:
                raise ValueError("Enter the account number shown in MT5")
            if environment == "LIVE" and not live_ack:
                raise ValueError("Confirm that this is a real-money account before saving Live settings")
            if bool(telegram_token.strip()) != bool(telegram_chat.strip()):
                raise ValueError("Enter both Telegram credential fields, or leave both blank")
            risk_changes = {
                "risk_per_trade": risk_pct / 100, "hard_risk_max": hard_pct / 100,
                "portfolio_risk_limit": portfolio_pct / 100, "daily_loss_limit": daily_pct / 100,
                "weekly_loss_limit": weekly_pct / 100, "drawdown_limit": drawdown_pct / 100,
                "max_spread": max_spread, "max_slippage_points": int(max_slippage),
                "max_orders_session": int(entries_per_session), "consecutive_loss_limit": int(consecutive_losses),
                "loss_cooldown_minutes": int(cooldown), "emergency_close_attempts": int(close_attempts),
                "commission_per_lot": commission, "slippage_price": modeled_slippage,
                "swap_long_per_lot_day": swap_long, "swap_short_per_lot_day": swap_short,
                "session_file": Path(session_path) if session_path.strip() else None,
            }
            if telegram_token.strip() and telegram_chat.strip():
                save_telegram_credentials(telegram_token, telegram_chat)
            save_user_setup(store, settings, mode=environment, account_login=int(login_text),
                            account_server=account_server, symbol=selected_symbol, mt5_path=terminal_path,
                            settings_changes=risk_changes, strategy=strategy_name,
                            execution_mode=selected_mode,
                            telegram_preferences={"enabled": notifications_on, "events": selected_events,
                                                  "position_update_minutes": position_interval})
            result = check_broker_connection(configured(store))
            verified = result.get("broker", "").startswith("verified")
            st.success("Settings saved. Your chosen execution mode is saved, but no order was authorized or submitted.")
            if verified:
                account_status = result.get("account", {})
                st.info(f"Connected to {account_status.get('environment')} account "
                        f"{account_status.get('login')} on {account_status.get('server')}. "
                        f"MT5 Algo Trading: {'On' if account_status.get('terminal_algo_trading_allowed') else 'Off'}; "
                        f"Python API: {'Allowed' if account_status.get('external_api_allowed') else 'Blocked'}.")
            else:
                st.warning(result.get("broker", "Connection check did not return a result."))
            st.rerun()
        except Exception as exc:
            st.error(str(exc))

    if not setup_complete:
        st.info("Recommended defaults are already filled in. Save once to finish setup. You can change these options later in Settings.")
    if settings.mode == "LIVE":
        st.caption("Live mode configuration is visible throughout the dashboard. Saving it does not authorize automatic orders.")

if page == "Overview":
    st.caption("Broker account figures are real snapshots. Signal-only outcomes are labeled as simulated.")
    if settings.mode == "LIVE":
        st.error("LIVE TRADING • REAL MONEY. Auto execute can place real broker orders after you authorize it. "
                 "Review the account, risk limits, open exposure, and strategy validation first.")

    @st.fragment(run_every=5)
    def monitor_status_panel():
        current_health = service_health(store)
        current_mode = store.get(f"execution_mode:{account_key}", settings.auto_execution_mode) \
            if account_key else "SIGNAL_ONLY"
        current_permission = store.get(f"armed:{account_key}", {}).get("active", False) if account_key else False
        watched = current_health.get("strategies") or store.get("monitored_strategies", list(REGISTRY))
        watch_label = ", ".join(watched) if watched else "none selected"
        state = current_health.get("state", "STOPPED")
        if state == "STOPPED":
            st.warning("Dashboard is open, but the trading monitor is stopped. It is not scanning live candles "
                       "or looking for signals. Start the monitor below to check the selected strategies.")
        elif state in ("STALE", "FAILED", "DISCONNECTED"):
            st.error(f"Trading monitor is {state.lower()}; its latest check is not current. Review System Logs.")
        elif state == "HALTED":
            st.warning(f"Monitor is running and checking {watch_label}, but the safety halt blocks new orders.")
        else:
            st.success(f"Monitor is running • checking {watch_label}.")
        st.caption(f"Selected to scan: {watch_label} • {current_mode.replace('_', ' ')} mode • automatic order permission "
                   f"{'on' if current_permission else 'off'} • checked {local_time(current_health.get('heartbeat'))}")

    monitor_status_panel()
    if not account:
        st.info("No verified account snapshot. Configure the MT5 account, then run Check connection in Settings.")
    columns = st.columns(4)
    for column, label, key in zip(columns, ["Balance", "Equity", "Free margin", "Floating P&L"],
                                  ["balance", "equity", "free_margin", "floating"]):
        value = account.get(key) if key != "floating" else account.get("equity", 0) - account.get("balance", 0)
        column.metric(label, f"{value:,.2f} {account.get('currency', '')}" if account and value is not None else "—")
    if account:
        algo_allowed = account.get("terminal_trade_allowed", account.get("expert_allowed"))
        python_allowed = account.get("external_api_allowed", account.get("expert_allowed"))
        first, second = st.columns(2)
        first.metric("MT5 Algo Trading", "On" if algo_allowed is True else "Off" if algo_allowed is False else "Not checked")
        second.metric("Python trading access", "Allowed" if python_allowed is True
                      else "Blocked" if python_allowed is False else "Not checked")
        if algo_allowed is False:
            st.error("MT5 Algo Trading is off. Turn on the top-toolbar Algo Trading button and Tools → Options → Expert Advisors → Allow algorithmic trading.")
        if python_allowed is False:
            st.error("MT5 blocks the Python API. In Tools → Options → Expert Advisors, clear ‘Disable algorithmic trading via external Python API’.")
    @st.fragment(run_every=15)
    def pnl_panel():
        # Re-read persisted snapshots so the open dashboard doesn't retain closed-trade P&L.
        current_account = store.get("active_account", account)
        risk = store.get(f"risk:{account_key}", {})
        currency = current_account.get("currency", "")
        left, right = st.columns(2)
        left.metric("Daily P&L (UTC)", f"{risk.get('daily_pnl', 0):,.2f} {currency}".strip()
                    if risk else "—")
        right.metric("Weekly P&L (UTC)", f"{risk.get('weekly_pnl', 0):,.2f} {currency}".strip()
                     if risk else "—")
        risk_time = risk.get("last_seen")
        risk_age = (utcnow() - pd.Timestamp(risk_time).to_pydatetime()).total_seconds() if risk_time else None
        account_time = current_account.get("timestamp")
        st.caption("Period P&L includes realized and floating broker results; floating P&L above is current equity "
                   "minus balance. Closed-position totals are in Trade Journal.")
        if risk and not risk.get("baselines_verified"):
            st.warning("This P&L is the latest account-based estimate. The UTC day/week starting balance was not "
                       "verified from reset, so automatic entries remain blocked until a trustworthy baseline is available.")
        if risk.get("daily_loss", 0) >= settings.daily_loss_limit:
            st.error(f"The saved daily loss ({risk['daily_loss']:.2%}) has reached the "
                     f"{settings.daily_loss_limit:.2%} limit. Automatic orders must stay blocked.")
        if risk_age is None or risk_age > 30:
            st.warning(f"Risk figures are the last saved check ({local_time(risk_time)}). "
                       "Refresh from MT5 to include any newer position close.")
            if st.button("Refresh account, trades, and P&L from MT5 (no orders)",
                         disabled=health["state"] not in ("STOPPED", "STALE", "FAILED"),
                         help="Reads MT5 and updates balances, open positions, and daily/weekly P&L. It never sends an order."):
                try:
                    result = check_broker_connection()
                    if result.get("broker", "").startswith("verified"):
                        st.success("Account and risk figures refreshed from MT5.")
                        st.rerun()
                    st.error(result.get("broker", "MT5 check could not be completed."))
                except Exception as exc:
                    st.error(f"MT5 refresh failed: {exc}")
        elif health["state"] not in ("RUNNING", "HALTED"):
            st.caption("The monitor is stopped, so new prices will not update automatically; the displayed account check is current.")
        if account_time:
            st.caption(f"Account balance/equity last checked: {local_time(account_time)}")

    pnl_panel()
    active_permission = store.get(f"armed:{account_key}", {}).get("active", False) if account_key else False
    auto_selected = store.get(f"execution_mode:{account_key}", settings.auto_execution_mode) != "SIGNAL_ONLY" \
        if account_key and settings.mode in ("DEMO", "LIVE") else False
    st.caption(f"Monitor strategies: {health.get('strategy', ', '.join(store.get('monitored_strategies', list(REGISTRY))))} • "
               f"Auto execute selected: {auto_selected} • "
               f"Order permission: {'On' if active_permission else 'Off'}")
    exposure = store.get(f"exposure:{account_key}", {})
    st.caption(f"Current broker stop risk: {exposure.get('stop_risk_amount', 'unavailable')} "
               f"{account.get('currency', '')} • snapshot: {account.get('timestamp', 'unavailable')}")
    mode_key = f"execution_mode:{account_key}" if account_key else None
    execution_mode = store.get(mode_key, settings.auto_execution_mode if settings.mode in ("DEMO", "LIVE")
                               else "SIGNAL_ONLY") if mode_key else (
                                   settings.auto_execution_mode if settings.mode in ("DEMO", "LIVE") else "SIGNAL_ONLY")
    if execution_mode not in ("SIGNAL_ONLY", settings.auto_execution_mode):
        execution_mode = settings.auto_execution_mode if settings.mode in ("DEMO", "LIVE") else "SIGNAL_ONLY"
    st.subheader("How the bot handles signals")
    choices = ["SIGNAL_ONLY", settings.auto_execution_mode] if settings.mode in ("DEMO", "LIVE") else ["SIGNAL_ONLY"]
    selected_mode = st.radio("Trading mode", choices, horizontal=True,
                             format_func=lambda value: "Signal only • Telegram + simulated outcome tracking"
                             if value == "SIGNAL_ONLY" else "Auto execute • LIVE real money"
                             if value == "AUTO_LIVE" else "Auto execute • DEMO account",
                             index=0 if execution_mode == "SIGNAL_ONLY" else 1,
                             help="Auto execute is the default mode. New orders stay blocked until account identity, "
                                  "strategy approval, risk checks, broker permissions, and your explicit authorization pass.")
    if selected_mode != execution_mode:
        mode_already_queued = any(command["kind"] == "set-execution-mode" and command["state"] == "PENDING"
                                  and command["payload"].get("mode") == selected_mode
                                  for command in store.list("commands", 50))
        if not account_key:
            st.error("Verify the MT5 account with doctor before changing its operating mode.")
        elif not mode_already_queued:
            store.enqueue("set-execution-mode", {"mode": selected_mode})
            st.info("Mode change queued. Auto execute requires a fresh, account-specific authorization before any order.")
        else:
            st.info("Mode change is queued; start the trading monitor if it is stopped so the engine can apply it.")
    if selected_mode == settings.auto_execution_mode:
        active_permission = store.get(f"armed:{account_key}", {}).get("active", False) if account_key else False
        halt = store.get(f"halt:{account_key}", {}) if account_key else {}
        reconciliation = store.get(f"reconciliation:{account_key}", {}) if account_key else {}
        current_strategy = build(settings.active_strategy,
                                 store.get(f"strategy_parameters:{settings.active_strategy}", {}))
        approval = store.get(f"approval:{current_strategy.config_hash}", {})
        permission_state = store.get(f"trading_permissions:{account_key}", {}) if account_key else {}
        if active_permission and not halt.get("active"):
            if settings.mode == "LIVE":
                st.error("LIVE AUTO EXECUTE IS ON • REAL MONEY orders may be submitted when a valid signal occurs.")
            else:
                st.success("Demo Auto execute is authorized for this account.")
        elif halt.get("active"):
            st.error(f"Auto execute is selected, but orders are blocked by a safety halt: "
                     f"{halt.get('reason', 'review required')}")
        elif reconciliation and not reconciliation.get("safe", False):
            reason = "; ".join(reconciliation.get("reasons", []))
            st.warning("Auto execute is selected, but the account has open exposure the bot cannot verify. "
                       f"Review the position before enabling orders. {reason}")
        elif permission_state.get("blocked"):
            st.warning("Auto execute is selected, but MT5 permissions are off. "
                       + " ".join(permission_state.get("blockers", [])))
        elif not approval.get("approved"):
            st.info("Auto execute is selected. This strategy has not passed the saved validation policy yet, so no orders can be authorized.")
        else:
            st.info(f"Auto execute is selected for this {settings.mode} account. Review the checks below and authorize once.")
        st.caption("Authorization permits future qualifying signals. It never places an immediate order.")
        acknowledged = st.checkbox(
            "I reviewed this account and risk limits and want to allow automatic broker orders",
            key="execution_risk_acknowledged",
            help="This is a one-time authorization for the verified account. Risk, identity, and strategy checks still run before every order.",
        )
        authorization_blockers = []
        if not account_key:
            authorization_blockers.append("Connect and verify the selected MT5 account")
        if health["state"] not in ("STOPPED", "STALE", "FAILED"):
            authorization_blockers.append("Stop the monitor before authorizing")
        if halt.get("active"):
            authorization_blockers.append("Recover and reset the active safety halt")
        if reconciliation and not reconciliation.get("safe", False):
            authorization_blockers.append("Review the open position the bot cannot verify")
        if not approval.get("approved"):
            authorization_blockers.append("Validate this strategy against historical data")
        if account and (not account.get("trade_allowed") or not account.get("expert_allowed")):
            authorization_blockers.append("Turn on MT5 trading and Python API permissions")
        can_authorize = bool(account_key and settings.account_login and acknowledged and not authorization_blockers)
        button_label = "Authorize LIVE auto execution • REAL MONEY" if settings.mode == "LIVE" \
            else "Authorize Auto execute"
        if st.button(button_label, type="primary" if settings.mode == "LIVE" else "secondary",
                     disabled=not can_authorize,
                     help="Runs the final account, strategy, risk, cost, market-session, and broker checks. No order is sent now."):
            launch_authorization(settings.mode, settings.account_login)
        if authorization_blockers:
            st.caption("Still needed: " + " • ".join(authorization_blockers))
    else:
        st.caption("Signals go to Telegram when configured; forward outcomes use simulated fills and stay separate.")
    start_control, stop_control = st.columns(2)
    if start_control.button("Start strategy monitor • scan selected strategies",
                            disabled=health["state"] in ("RUNNING", "HALTED"),
                            help="Starts live candle monitoring. Auto execute is only the selected mode; broker orders also require strategy approval, account authorization, and passing risk checks."):
        launch_service()
    if stop_control.button("Stop service gracefully", disabled=health["state"] not in ("RUNNING", "HALTED")):
        store.enqueue("stop")
        st.success("Stop queued; protective broker orders remain active.")
    equity_chart()
    @st.fragment(run_every=5)
    def position_panel():
        state = store.get(f"reconciliation:{account_key}", {})
        positions = state.get("positions", [])
        snapshot = state.get("timestamp")
        if snapshot:
            age = max(0, (utcnow() - datetime.fromisoformat(snapshot)).total_seconds())
            if age > max(30, settings.poll_seconds * 3) or health["state"] != "RUNNING":
                st.warning(f"Last broker position check was {age / 60:.0f} minutes ago. "
                           "The monitor is not current, so this list may be stale.")
            else:
                st.caption(f"Broker positions checked {age:.0f} seconds ago • {local_time(snapshot)}")
        else:
            st.warning("No verified broker position check has been saved yet.")
        if positions:
            bot_positions = [position for position in positions if position.get("magic") == settings.magic]
            other_positions = [position for position in positions if position.get("magic") != settings.magic]
            if bot_positions:
                st.warning(f"The bot currently has {len(bot_positions)} open position(s). "
                           "Open and 15-minute updates are also sent to Telegram when it is configured.")
            if other_positions:
                st.warning(f"{len(other_positions)} account position(s) are manual or unrecognized. "
                           "Auto execute stays blocked until the bot can safely reconcile this account exposure.")
            display_positions = [{**position,
                                  "Owner": "This bot" if position.get("magic") == settings.magic
                                  else "Manual / unrecognized"} for position in positions]
            st.dataframe(pd.DataFrame(display_positions), hide_index=True)
        else:
            if snapshot and health["state"] == "RUNNING":
                st.info("No broker positions are currently open.")
            else:
                st.info("No positions in the last saved broker check; start the monitor to confirm the current account.")

    with st.expander("Open positions", expanded=True):
        position_panel()
    with st.expander("Latest signal"):
        latest = store.get("latest_signal")
        if latest:
            st.write(f"**{latest.get('direction')} {latest.get('symbol')}** • {latest.get('strategy')}")
            st.caption(f"Completed candle: {latest.get('timestamp')} • Entry {latest.get('entry')} • "
                       f"Stop {latest.get('stop_loss')} • Target {latest.get('take_profit')}")
            st.write(latest.get("rationale", ""))
        else:
            st.caption("No persisted strategy signal.")
    with st.expander("Recent activity"):
        display_records("activity", 30)
elif page == "Market Analysis":
    candles_chart()
elif page == "Strategies":
    available = list(REGISTRY)
    current_monitored = store.get("monitored_strategies", available)
    current_monitored = [name for name in current_monitored if name in REGISTRY]
    monitored = st.multiselect("Strategies the live monitor checks", available, default=current_monitored,
        help="All registered strategies are checked by default. Saving a smaller list changes what the live monitor scans; it does not approve or authorize broker orders.")
    if st.button("Save monitored strategies", disabled=not monitored,
                 help="Apply this list when the monitor processes its next cycle."):
        if monitored != current_monitored:
            store.enqueue("set-monitored-strategies", {"names": monitored})
            st.info("Strategy list queued. The monitor applies it on its next check and automatic order permission is cleared for review.")
        else:
            st.success("The monitor already checks this strategy list.")
    name = st.selectbox("Strategy", list(REGISTRY), help="Choose the rule set whose defaults or saved values you want to edit.")
    existing = store.get(f"strategy_parameters:{name}", {})
    strategy = build(name, existing)
    st.write(strategy.description)
    approval = store.get(f"approval:{strategy.config_hash}", {})
    st.caption(f"Validation: {'APPROVED' if approval.get('approved') else 'NOT APPROVED'} • {strategy.config_hash[:16]}")
    parameters = strategy_parameter_inputs(name, existing, f"strategy_{name}")
    if st.button("Save strategy rules and set as primary", help="Validate and queue these rules. The monitor still checks every selected strategy; automatic orders need matching research approval and account permission."):
        try:
            validated = Parameters(**parameters)
            build(name, validated.model_dump())
            store.enqueue("select-strategy", {"name": name, "parameters": validated.model_dump()})
            st.success("Configuration queued. Automatic orders stay blocked until matching approval exists.")
        except Exception as exc:
            st.error(str(exc))
    with st.expander("Saved research comparisons"):
        display_records("backtest_results", 30)
elif page == "Backtesting Lab":
    st.caption("Historical candles only • simulated fills never reach MT5. Every run keeps its own rules, costs, risk settings, and trade ledger.")
    operation = st.selectbox("Research task", ["compare-strategies", "validate-strategy", "backtest"],
                             format_func=lambda value: {"compare-strategies": "Compare all strategies",
                                 "validate-strategy": "Evaluate selected strategy",
                                 "backtest": "Exploratory full-range simulation"}[value],
                             help="Comparison and validation use chronological partitions. Full-range simulation is exploratory and cannot approve automation.")
    research_name = st.selectbox("Strategy to evaluate", list(REGISTRY)) \
        if operation != "compare-strategies" else None
    parameter_strategy = research_name or st.selectbox("Rule settings template", list(REGISTRY),
        help="For comparisons, these shared indicator and session settings are applied while stop/target combinations are also tested.")
    stored_timeframes = available_timeframes()
    entries = [tf for tf in ("M1", "M5", "M15", "M30") if tf in stored_timeframes]
    trends = [tf for tf in ("H1", "H4") if tf in stored_timeframes]
    if not entries or not trends:
        st.warning("Download an entry timeframe (M1/M5/M15/M30) and trend timeframe (H1/H4).")
        entries, trends = entries or ["M15"], trends or ["H1"]
    timeframe = st.selectbox("Entry timeframe", entries, index=entries.index("M15") if "M15" in entries else 0,
                             help="Signals are generated from each completed candle at this interval.")
    trend_timeframe = st.selectbox("Trend confirmation", trends,
                                   index=trends.index("H1") if "H1" in trends else 0,
                                   help="Trend rules use only higher-timeframe candles that had completed at the signal time.")
    first, second = st.columns(2)
    start = first.date_input("Start", value=(utcnow() - pd.Timedelta(days=365 * 3)).date(),
                             help="Beginning of the scored interval. Earlier stored candles may be used only for indicator warm-up.")
    end = second.date_input("End (exclusive)", value=utcnow().date(),
                            help="The end date is excluded from the backtest interval.")
    selected_history = store.load_candles(source, symbol, timeframe) if source and symbol else pd.DataFrame()
    selected_trend_history = store.load_candles(source, symbol, trend_timeframe) \
        if source and symbol else pd.DataFrame()
    history_ready = not selected_history.empty and not selected_trend_history.empty
    if not selected_history.empty:
        coverage_start, coverage_end = selected_history.index[0], selected_history.index[-1]
        st.caption(f"Stored {timeframe} coverage: {coverage_start:%Y-%m-%d} through {coverage_end:%Y-%m-%d} "
                   f"({len(selected_history):,} candles)")
        if pd.Timestamp(start, tz="UTC") < coverage_start:
            st.warning(f"The requested start precedes stored {timeframe} history. The report will show the shortfall.")
    if not history_ready:
        st.warning("The selected entry and confirmation candles are not both stored. Download them before analysis.")
    if source and symbol:
        with store.engine.connect() as connection:
            coverage = connection.execute(select(candle_table.c.timeframe, func.count().label("candles"),
                func.min(candle_table.c.timestamp).label("first"), func.max(candle_table.c.timestamp).label("last"))
                .where(candle_table.c.source == source, candle_table.c.symbol == symbol)
                .group_by(candle_table.c.timeframe)).mappings().all()
        if coverage:
            coverage_rows = []
            for row in coverage:
                first_bar, last_bar = pd.Timestamp(row["first"]), pd.Timestamp(row["last"])
                years = (last_bar - first_bar).total_seconds() / (365.25 * 24 * 3600)
                coverage_rows.append({"Timeframe": row["timeframe"], "Candles": row["candles"],
                    "First saved candle (UTC)": first_bar.strftime("%Y-%m-%d %H:%M"),
                    "Last saved candle (UTC)": last_bar.strftime("%Y-%m-%d %H:%M"),
                    "Years available": round(years, 2)})
            with st.expander("All saved timeframe coverage", expanded=False):
                st.dataframe(pd.DataFrame(coverage_rows).sort_values("Timeframe"),
                             width="stretch", hide_index=True)
                st.caption("Each backtest uses the timeframe selected above. Lower-timeframe history may be shorter than three years; no candles are invented to fill gaps.")
    default_capital = round(max(100.0, float(account.get("balance", 10000.0))), 2)
    sizing = st.columns(2)
    capital = sizing[0].number_input("Starting balance for this backtest (USD)", min_value=1.0,
        value=default_capital, step=100.0,
        help="Enter any simulated account balance, such as 1,000 or 5,000. This only changes the offline backtest; it does not deposit or move money. It defaults to the last verified MT5 account balance.")
    risk_percent = sizing[1].number_input("Risk per trade (%)", min_value=0.01, max_value=0.5,
        value=settings.risk_per_trade * 100, step=0.01,
        help="Percent of the starting balance budgeted for one trade, including estimated costs and broker lot-step rounding.")
    risk_inputs = st.columns(3)
    day_loss = risk_inputs[0].number_input("Daily loss stop (%)", min_value=0.01, max_value=10.0,
        value=settings.daily_loss_limit * 100, step=0.1,
        help="Block new simulated entries once losses reach this percent of the day's starting balance.")
    week_loss = risk_inputs[1].number_input("Weekly loss stop (%)", min_value=0.01, max_value=20.0,
        value=settings.weekly_loss_limit * 100, step=0.1,
        help="Block new simulated entries after this percent loss from Monday 00:00 UTC equity.")
    portfolio_risk = risk_inputs[2].number_input("Combined open risk cap (%)", min_value=0.01, max_value=2.0,
        value=settings.portfolio_risk_limit * 100, step=0.01,
        help="Block an entry if estimated stop risk across open and proposed positions would exceed this percent.")
    sizing_summary = st.columns(3)
    sizing_summary[0].metric("Risk per trade", f"${capital * risk_percent / 100:,.2f}")
    sizing_summary[1].metric("Daily loss limit", f"${capital * day_loss / 100:,.2f}")
    sizing_summary[2].metric("Weekly loss limit", f"${capital * week_loss / 100:,.2f}")
    sizing_summary = st.columns(2)
    sizing_summary[0].metric("Hard per-trade cap", f"${capital * settings.hard_risk_max:,.2f}")
    sizing_summary[1].metric("Combined open-risk limit", f"${capital * portfolio_risk / 100:,.2f}")
    st.caption("Amounts are recalculated from the balance and percentages you enter. Broker minimum lot size may cause a trade to be skipped if it would exceed the risk cap.")
    parameters = strategy_parameter_inputs(parameter_strategy,
        store.get(f"strategy_parameters:{parameter_strategy}", {}), "backtest_rules", include_timeframes=False)
    tune_stop_targets = True
    if operation != "backtest":
        tune_stop_targets = st.checkbox("Search alternative stop/target values", value=True,
            help="On: test the built-in nine stop/target combinations. Off: use the exact ATR stop and reward/risk values above.")
    with st.expander("Risk, costs, and execution assumptions", expanded=False):
        columns = st.columns(2)
        spread = columns[0].number_input("Spread (gold price units)", min_value=0.0,
            value=float(settings.max_spread or 0.0), step=0.01,
            help="Estimated full bid/ask spread charged in each historical trade.")
        commission = columns[1].number_input("Commission per lot (USD)", min_value=0.0,
            value=float(settings.commission_per_lot or 0.0), step=0.1,
            help="Round-trip commission is charged at entry and exit using this per-lot amount.")
        slippage = columns[0].number_input("Slippage per fill (price units)", min_value=0.0,
            value=float(settings.slippage_price or 0.0), step=0.01,
            help="Adverse price movement applied to entries and exits.")
        swap_long = columns[1].number_input("Long swap per lot per day (USD)",
            value=float(settings.swap_long_per_lot_day or 0.0), step=0.1,
            help="Daily financing assumption for a long position. Broker rates can change and may vary by weekday.")
        swap_short = columns[0].number_input("Short swap per lot per day (USD)",
            value=float(settings.swap_short_per_lot_day or 0.0), step=0.1,
            help="Daily financing assumption for a short position.")
        margin_percent = columns[1].number_input("Required margin (%)", min_value=0.1, max_value=100.0,
            value=1.0, step=0.1,
            help="Broker margin fraction for simulation: 1% approximates 100:1 leverage.")
        drawdown = columns[1].number_input("Peak drawdown stop (%)", min_value=0.01, max_value=50.0,
            value=settings.drawdown_limit * 100, step=0.1,
            help="Block entries after the simulated equity falls this percentage from its high-water mark.")
        consecutive = columns[0].number_input("Consecutive loss stop", min_value=1,
            value=settings.consecutive_loss_limit, step=1,
            help="Block further entries after this many consecutive net losing trades.")
        session_entries = columns[1].number_input("Entries per session", min_value=1,
            value=settings.max_orders_session, step=1,
            help="Maximum requested entries per named strategy session, or UTC day for continuous strategies.")
        max_positions = columns[0].number_input("Simultaneous positions", min_value=1, max_value=10,
            value=settings.max_positions, step=1,
            help="Maximum number of open simulated positions. The live/demo default remains one position.")
    consume_holdout = False
    if operation == "backtest":
        consume_holdout = st.checkbox("I understand this full-range run is exploratory, not approval evidence",
            help="This allows repeatable parameter experiments. Use Validate strategy for the one-time approval holdout.")
    if operation == "validate-strategy":
        consume_holdout = st.checkbox("Run the explicit one-time final holdout evaluation",
            help="The holdout is claimed before it is read and cannot be retuned afterward.")
    st.caption("Contract size comes from the saved broker specification. These cost and risk assumptions are saved with the run.")
    if st.button("Start historical analysis", disabled=not history_ready or
                 ((operation == "backtest" or operation == "validate-strategy") and not consume_holdout)):
        if not source or not symbol or start >= end:
            st.error("Broker source, symbol, and a valid date range are required.")
        else:
            try:
                validated_parameters = Parameters(**parameters).model_dump()
                risk_limits = {"daily_loss_limit": day_loss / 100, "weekly_loss_limit": week_loss / 100,
                    "drawdown_limit": drawdown / 100, "consecutive_loss_limit": int(consecutive),
                    "max_orders_session": int(session_entries), "max_positions": int(max_positions),
                    "portfolio_risk_limit": portfolio_risk / 100}
                Path("logs").mkdir(exist_ok=True)
                command = [sys.executable, "-m", "app.main", operation, "--source", source,
                    "--symbol", symbol, "--start", start.isoformat(), "--end", end.isoformat(),
                    "--capital", str(capital), "--spread", str(spread), "--commission", str(commission),
                    "--slippage", str(slippage), "--swap-long", str(swap_long), "--swap-short", str(swap_short),
                    "--margin", str(margin_percent / 100),
                    "--risk-fraction", str(risk_percent / 100), "--risk-limits-json", json.dumps(risk_limits),
                    "--parameters-json", json.dumps(validated_parameters), "--timeframe", timeframe,
                    "--trend-timeframe", trend_timeframe]
                if research_name:
                    command.extend(["--strategy", research_name])
                if operation == "validate-strategy" and consume_holdout:
                    command.append("--consume-holdout")
                if operation != "backtest" and not tune_stop_targets:
                    command.append("--fixed-stop-target")
                with open("logs/research-console.log", "a", encoding="utf-8") as log:
                    process = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=log,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                st.success(f"Analysis started (PID {process.pid}). Refresh later to see its saved result.")
            except Exception as exc:
                st.error(f"Check the research settings: {exc}")
    st.caption("Repeat the comparison with M5, M15, and M30 to compare the strategy rules across entry intervals.")
    with st.expander("Dataset quality and gaps"):
        quality_records = store.list("data_quality", 100)
        if quality_records:
            quality_rows = []
            for record in quality_records:
                value = record["payload"]
                quality_rows.append({"checked": record["timestamp"], "timeframe": value.get("timeframe"),
                    "rows": value.get("rows", 0), "from": value.get("start"), "through": value.get("end"),
                    "unresolved gaps": len(value.get("gaps", [])),
                    "empty chunks": len(value.get("empty_chunks", [])),
                    "shortfall days": round((value.get("leading_history_shortfall_seconds", 0) or 0) / 86400, 1),
                    "complete": value.get("complete", False)})
            st.dataframe(pd.DataFrame(quality_rows), width="stretch", hide_index=True)
            quality_csv = pd.DataFrame(quality_rows).to_csv(index=False)
            st.download_button("Export coverage summary", quality_csv, "data-quality.csv", "text/csv")
            selected_quality = st.selectbox("Gap detail timeframe", sorted({
                str(row.get("payload", {}).get("timeframe", "unknown")) for row in quality_records}))
            detail: dict = next((row["payload"] for row in quality_records
                                 if row["payload"].get("timeframe") == selected_quality), {})
            gap_frame = pd.DataFrame(detail.get("gaps", []))
            if not gap_frame.empty:
                st.dataframe(gap_frame.head(100), width="stretch", hide_index=True)
                st.download_button("Export all listed gaps", gap_frame.to_csv(index=False),
                                   f"{selected_quality}-gaps.csv", "text/csv")
        else:
            st.info("No coverage report is stored yet.")
    st.subheader("Saved results and downloadable reports")
    results = store.list("backtest_results", 30)
    if not results:
        st.info("No research results available.")
    if results:
        labels = [f"{record['timestamp']} • {record['payload'].get('status', record['payload'].get('approval', 'research run'))}"
                  for record in results]
        selected = st.selectbox("Research run", range(len(results)), format_func=lambda index: labels[index],
                                help="Each run retains its own settings, results, and full simulated trade ledger.")
        record = results[selected]
        payload = record["payload"]
        st.write(f"**{payload.get('status', payload.get('approval', 'Research run complete'))}**")
        if payload.get("initial_capital") is not None:
            st.caption(f"This historical simulation started with ${payload['initial_capital']:,.2f}. "
                       "Its simulated P&L is separate from MT5 account profit and loss.")
        if str(payload.get("status", "")).startswith("NO STRATEGY APPROVED"):
            st.warning("No candidate passed the development checks. The untouched final 20% holdout was not read; "
                       "there is no strategy approval for automatic trading.")
        if payload.get("metrics"):
            display_metrics(payload["metrics"])
        if payload.get("candidates"):
            candidates = payload["candidates"]
            ranked_candidates = sorted(candidates,
                key=lambda item: (item.get("median_forward_r") is not None,
                                  item.get("median_forward_r") or float("-inf")), reverse=True)
            comparison = pd.DataFrame([{"Rank by forward expectancy": index + 1,
                "Strategy": candidate.get("strategy"), "Development result":
                    "Passed" if candidate.get("qualified", False) else
                    candidate.get("reason") or "Forward validation gates not met",
                "Median forward R": candidate.get("median_forward_r"),
                "Validation expectancy": (candidate.get("validation") or {}).get("expectancy"),
                "Profit factor": (candidate.get("validation") or {}).get("profit_factor"),
                "Maximum drawdown": (candidate.get("validation") or {}).get("maximum_drawdown"),
                "Trades": (candidate.get("validation") or {}).get("total_trades", 0),
                "Positive forward folds": sum(1 for fold in candidate.get("folds", [])
                                               if (fold.get("expectancy") or 0) > 0),
                "Forward folds": len(candidate.get("folds", [])),
                "Stop ATR": (candidate.get("configuration") or {}).get("strategy_parameters", {}).get("stop_atr"),
                "Reward / risk": (candidate.get("configuration") or {}).get("strategy_parameters", {}).get("reward_risk")}
                for index, candidate in enumerate(ranked_candidates)])
            st.dataframe(comparison, width="stretch", hide_index=True)
            st.caption("Ranking uses saved out-of-sample forward expectancy where available; it does not authorize trading.")
            for candidate in candidates:
                sensitivity = candidate.get("sensitivity", [])
                if sensitivity:
                    with st.expander(f"{candidate.get('strategy', 'Strategy')} • training stop/target grid"):
                        st.caption("These settings were screened on training data only. They do not include the untouched final holdout.")
                        st.dataframe(pd.DataFrame([{
                            "Stop ATR": row.get("parameters", {}).get("stop_atr"),
                            "Reward / risk": row.get("parameters", {}).get("reward_risk"),
                            "Training trades": (row.get("metrics") or {}).get("total_trades"),
                            "Training expectancy": (row.get("metrics") or {}).get("expectancy"),
                            "Training profit factor": (row.get("metrics") or {}).get("profit_factor"),
                            "Maximum drawdown": (row.get("metrics") or {}).get("maximum_drawdown"),
                            "Selected for forward test": row.get("score") is not None,
                        } for row in sensitivity]), width="stretch", hide_index=True)
        else:
            ranked_candidates = []
        best_candidate = payload.get("selected") or (ranked_candidates[0] if ranked_candidates else {})
        configuration = payload.get("configuration") or best_candidate.get("configuration") or {
            "strategy settings": best_candidate.get("parameters", payload.get("parameters")),
            "cost assumptions": payload.get("cost_assumptions"), "risk limits": payload.get("risk_limits"),
            "risk per trade": payload.get("risk_fraction"), "capital": payload.get("initial_capital"),
            "timeframes": [payload.get("entry_timeframe"), payload.get("trend_timeframe")],
        }
        configuration_rows = flatten_values(configuration)
        if configuration_rows:
            with st.expander("Settings used for this run / top-ranked candidate"):
                st.dataframe(pd.DataFrame(configuration_rows), width="stretch", hide_index=True)
        if payload.get("risk_blocks"):
            st.caption("Historical entry blocks: " + ", ".join(
                f"{reason.replace('_', ' ')} {count}" for reason, count in payload["risk_blocks"].items()))

        reports = []
        if payload.get("report"):
            reports.append((payload.get("strategy", "Full run"), payload["report"], payload.get("metrics"),
                            payload.get("configuration", {})))
        if payload.get("holdout_report"):
            reports.append(("Final holdout", payload["holdout_report"], payload.get("holdout"), {}))
        for candidate in payload.get("candidates", []):
            if candidate.get("report"):
                reports.append((candidate.get("strategy", "Strategy"), candidate["report"],
                                candidate.get("validation"), candidate.get("configuration", {})))
        for title, report, metrics, report_configuration in reports:
            folder = Path(report).resolve()
            if not folder.is_relative_to(settings.report_dir.resolve()):
                continue
            trades_path = folder / "trades.csv"
            ledger = pd.read_csv(trades_path) if trades_path.is_file() else pd.DataFrame()
            with st.expander(f"{title} • {len(ledger):,} saved trades", expanded=not bool(payload.get("candidates"))):
                if report_configuration:
                    st.dataframe(pd.DataFrame(flatten_values(report_configuration)),
                                 width="stretch", hide_index=True)
                if trades_path.is_file():
                    trades = ledger
                    st.caption("Complete simulated position details: broker-volume size, fill prices, costs, net P&L, and R multiple.")
                    st.dataframe(trades, width="stretch", hide_index=True)
                    st.download_button("Download full trade ledger", trades_path.read_bytes(),
                                       f"{title.lower().replace(' ', '-')}-trades.csv",
                                       "text/csv", key=f"trades_{record['id']}_{title}")
                else:
                    st.caption("No validation trade ledger was generated for this candidate.")
                equity_path = folder / "equity.csv"
                if equity_path.is_file() and metrics and st.checkbox("Show equity and drawdown",
                                                                      key=f"chart_{record['id']}_{title}"):
                    equity_data = pd.read_csv(equity_path, index_col=0, parse_dates=True)
                    initial = metrics.get("initial_capital", 0)
                    equity = equity_data.Equity
                    figure = make_subplots(rows=2, cols=1, shared_xaxes=True,
                                           subplot_titles=["Historical equity (USD)", "Drawdown (%)"])
                    figure.add_trace(go.Scatter(x=equity.index, y=equity, name="Equity"), row=1, col=1)
                    figure.add_trace(go.Scatter(x=equity.index,
                        y=100 * (1 - equity / equity.cummax().clip(lower=initial)),
                        name="Drawdown %", fill="tozeroy"), row=2, col=1)
                    figure.update_layout(template="plotly_dark", height=500)
                    st.plotly_chart(figure, width="stretch")
                for filename in ("report.html", "equity.csv", "comparisons.csv", "sensitivity.csv"):
                    path = folder / filename
                    if path.is_file():
                        st.download_button(f"Download {filename}", path.read_bytes(), filename,
                                           key=f"{record['id']}_{title}_{filename}")
elif page == "Trade Journal":
    st.caption("Actual broker fills and signal-only simulated outcomes are reported separately.")
    journal = pd.DataFrame(trade_journal(store, account_key, settings.magic)) if account_key else pd.DataFrame()
    all_positions = pd.DataFrame(trade_journal(store, account_key, None, settings.magic)) \
        if account_key else pd.DataFrame()
    position_rows = store.get(f"reconciliation:{account_key}", {}).get("positions", []) if account_key else []
    bot_floating = sum(float(p.get("profit", 0)) for p in position_rows if p.get("magic") == settings.magic)
    outcomes = [row["value"] for row in store.list_state(f"signal_outcome:{account_key}:")] if account_key else []
    broker_tab, signals_tab, audit_tab = st.tabs(["Broker trade journal", "Signal-only outcomes", "Execution audit"])
    with broker_tab:
        if not all_positions.empty:
            closed_account = all_positions.loc[all_positions.status == "CLOSED"]
            bot_closed_account = closed_account.loc[closed_account.owner == "Bot"]
            other_closed_account = closed_account.loc[closed_account.owner != "Bot"]
            summary = st.columns(3)
            summary[0].metric("All closed MT5 trades", f"{closed_account.realized_net_pnl.sum():,.2f} {account.get('currency', '')}")
            summary[1].metric("Bot closed trades", f"{bot_closed_account.realized_net_pnl.sum():,.2f} {account.get('currency', '')}")
            summary[2].metric("Manual / unrecognized", f"{other_closed_account.realized_net_pnl.sum():,.2f} {account.get('currency', '')}")
            st.caption("Net P&L combines broker profit, commission, swap, and fees for each completed position. "
                       "Manual/unrecognized trades are shown for account reconciliation and never count toward bot win rate.")
            broker_times = payload_frame("broker_deals").get("time", pd.Series(dtype=float))
            if not broker_times.empty:
                first_synced = pd.to_datetime(broker_times, unit="s", utc=True, errors="coerce").min()
                st.caption(f"Broker trade history currently saved from {local_time(first_synced.isoformat())} onward.")
        if not journal.empty:
            closed_trades = journal.loc[journal.status == "CLOSED"]
            win_rate = float((closed_trades.realized_net_pnl > 0).mean()) if not closed_trades.empty else None
            columns = st.columns(4)
            columns[0].metric("Closed positions", len(closed_trades))
            columns[1].metric("Win rate", f"{win_rate:.1%}" if win_rate is not None else "—")
            columns[2].metric("Realized net P&L", f"{closed_trades.realized_net_pnl.sum():,.2f}")
            columns[3].metric("Open bot floating P&L", f"{bot_floating:,.2f}")
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
            st.download_button("Export broker trade journal", journal.to_csv(index=False),
                               "trade-journal.csv", "text/csv")
        else:
            st.info("No bot-owned broker fills are stored yet. Signal alerts do not count as executed trades.")
        broker_deals = payload_frame("broker_deals")
        if not broker_deals.empty and "magic" in broker_deals:
            other = broker_deals.loc[pd.to_numeric(broker_deals.magic, errors="coerce").fillna(0)
                                      != settings.magic].copy()
            if not journal.empty and "position_id" in other:
                other = other.loc[~other.position_id.isin(journal.position)]
            if not other.empty:
                other["UTC time"] = pd.to_datetime(other.time, unit="s", utc=True, errors="coerce")
                other["Side"] = other.type.map({0: "Buy", 1: "Sell"}).fillna("Other")
                other["Action"] = other.entry.map({0: "Entry", 1: "Exit", 2: "Reverse", 3: "Close by"}) \
                    .fillna("Other")
                other["Exit reason"] = other.reason.map({0: "Client", 1: "Mobile", 2: "Web",
                    3: "Expert advisor", 4: "Stop loss", 5: "Take profit"}).fillna("Other")
                net_deal = pd.Series(0.0, index=other.index)
                for name in ("profit", "commission", "swap", "fee"):
                    if name in other:
                        net_deal += pd.to_numeric(other[name], errors="coerce").fillna(0)
                other["Net deal P&L"] = net_deal
                with st.expander("Other account trades (not counted in bot performance)", expanded=True):
                    st.caption("These are manual or unrecognized broker deals. Their results are kept separate from the bot win rate.")
                    visible_columns = [name for name in ("UTC time", "position_id", "symbol", "Side", "Action", "volume",
                                      "price", "Exit reason", "Net deal P&L", "magic", "comment") if name in other]
                    st.dataframe(other[visible_columns].sort_values("UTC time", ascending=False),
                                 width="stretch", hide_index=True)
                    st.download_button("Export other broker deals", other[visible_columns].to_csv(index=False),
                                       "other-account-deals.csv", "text/csv")
        with st.expander("Raw broker deals"):
            display_records("broker_deals")
    with signals_tab:
        st.info("These are simulated signal outcomes. A Telegram signal is not a broker order or open MT5 position.")
        signal_frame = pd.DataFrame(outcomes)
        if not signal_frame.empty:
            closed_signals = signal_frame.loc[signal_frame.status == "CLOSED"]
            open_signals = signal_frame.loc[signal_frame.status == "OPEN"]
            signal_wr = float((closed_signals.realized_net_pnl > 0).mean()) if not closed_signals.empty else None
            columns = st.columns(4)
            columns[0].metric("Completed signals", len(closed_signals))
            columns[1].metric("Signal win rate", f"{signal_wr:.1%}" if signal_wr is not None else "—")
            columns[2].metric("Simulated realized P&L", f"{closed_signals.realized_net_pnl.sum():,.2f}"
                              if not closed_signals.empty else "0.00")
            columns[3].metric("Simulated open P&L", f"{open_signals.get('unrealized_net_pnl', pd.Series(dtype=float)).sum():,.2f}")
            status_labels = {"PENDING_ENTRY": "Waiting for entry candle", "OPEN": "Simulated position open",
                      "CLOSED": "Simulated trade closed", "BELOW_MINIMUM_VOLUME": "Not entered • minimum lot exceeds risk limit",
                      "COSTS_UNAVAILABLE": "Not tracked • cost assumptions missing", "EXPIRED": "Signal expired",
                      "INVALID_BRACKET": "Not entered • price invalidated stop or target"}
            readable = signal_frame.copy()
            readable["status"] = [status_labels.get(str(value), str(value).replace("_", " ").title())
                                  for value in readable["status"]]
            if (signal_frame.status == "BELOW_MINIMUM_VOLUME").any():
                st.warning("The latest signal was not simulated as a trade because the broker minimum lot would exceed the configured risk cap. "
                           "Any open MT5 position is shown separately above and may be manual or unrecognized.")
            st.dataframe(readable.sort_values("signal_time", ascending=False), width="stretch", hide_index=True)
            st.download_button("Export signal outcome ledger", signal_frame.to_csv(index=False),
                               "signal-outcomes.csv", "text/csv")
        else:
            st.info("Signal outcome tracking starts when the monitor sees a new signal in Signal-only mode.")
    with audit_tab:
        audit_tabs = st.tabs(["Order intents", "Risk decisions"])
        with audit_tabs[0]:
            display_records("order_intents")
        with audit_tabs[1]:
            display_records("risk_decisions")
elif page == "Risk Center":
    exposure = store.get(f"exposure:{account_key}", {})
    risk = store.get(f"risk:{account_key}", {})
    halt = store.get(f"halt:{account_key}", {})
    armed = store.get(f"armed:{account_key}", {}).get("active", False)
    st.caption(f"Automatic order permission: {'On' if armed else 'Off'} • daily/weekly resets use UTC")
    if halt.get("active"):
        st.error(f"Persistent halt: {halt.get('reason', 'Safety verification required')}")
    if risk.get("last_seen"):
        st.caption(f"Risk last checked: {local_time(risk['last_seen'])}. "
                   f"Daily P&L: {risk.get('daily_pnl', 0):,.2f}; weekly P&L: {risk.get('weekly_pnl', 0):,.2f} "
                   f"{account.get('currency', '')}.")
        if (utcnow() - pd.Timestamp(risk["last_seen"]).to_pydatetime()).total_seconds() > 30:
            st.warning("These risk figures are old. Refresh the MT5 connection on Overview to recalculate them.")
    if not risk or not exposure:
        st.info("No verified risk or exposure snapshot. Start the monitor to refresh account and position details.")
    columns = st.columns(3)
    for column, label, key in zip(columns, ["Daily loss", "Weekly loss", "Equity drawdown"],
                                  ["daily_loss", "weekly_loss", "drawdown"]):
        column.metric(label, f"{risk[key]:.2%}" if key in risk else "—")
    left, right = st.columns(2)
    amount = exposure.get("stop_risk_amount")
    left.metric("Open position stop risk", f"{amount:,.2f} {account.get('currency', '')}" if amount is not None else "—")
    right.metric("Consecutive losses", str(risk["consecutive_losses"]) if "consecutive_losses" in risk else "—")
    st.caption(f"Period baselines: {'verified' if risk.get('baselines_verified') else 'unverified — entries blocked'}")
    if not risk.get("baselines_verified"):
        st.warning("The daily/weekly starting balances were not observed continuously from their UTC reset. "
                   "The P&L is useful for monitoring, but automatic orders stay blocked until trustworthy baselines are established.")
    if risk.get("daily_loss", 0) >= settings.daily_loss_limit:
        st.error(f"Daily loss {risk['daily_loss']:.2%} reached the configured {settings.daily_loss_limit:.2%} limit.")
    if risk.get("weekly_loss", 0) >= settings.weekly_loss_limit:
        st.error(f"Weekly loss {risk['weekly_loss']:.2%} reached the configured {settings.weekly_loss_limit:.2%} limit.")
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
        st.dataframe(pd.DataFrame(flatten_values({"exposure": exposure, "risk": risk, "halt": halt})),
                     width="stretch", hide_index=True)
    if st.button("EMERGENCY HALT — persistently block entries", type="primary", disabled=not account_key):
        assert account_key is not None
        from app.risk.circuit_breaker import CircuitBreaker
        CircuitBreaker(store, account_key).trip("Operator dashboard emergency halt")
        store.enqueue("halt")
        st.success("Persistent safety halt recorded. Automatic orders are now blocked.")
    if st.button("Request halt reset after recovery", disabled=not account_key):
        store.enqueue("reset-halt")
        st.info("Reset queued; safety checks must pass. Reset does not authorize automatic orders.")
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
    if table == "errors" and "message" in frame:
        compact = frame.copy()
        compact["message"] = compact["message"].astype(str).str.slice(0, 240)
        st.dataframe(compact, width="stretch", hide_index=True)
        with st.expander("Full error details"):
            st.dataframe(frame, width="stretch", hide_index=True)
    else:
        st.dataframe(frame, width="stretch", hide_index=True)
    st.download_button("Export logs", frame.to_csv(index=False), "events.csv", "text/csv")
elif page == "Settings":
    st.subheader("MT5 account connection")
    if account:
        st.write(f"**{account.get('environment', 'Unknown')} account** • {account.get('server')} • "
                 f"login {account.get('login')} • {symbol or 'gold symbol not checked'}")
        algo_allowed = account.get("terminal_trade_allowed", account.get("expert_allowed"))
        python_allowed = account.get("external_api_allowed", account.get("expert_allowed"))
        first, second, third = st.columns(3)
        first.metric("Account trading", "Allowed" if account.get("trade_allowed") is True
                     else "Blocked" if account.get("trade_allowed") is False else "Not checked")
        second.metric("MT5 Algo Trading", "On" if algo_allowed is True else "Off" if algo_allowed is False else "Not checked")
        third.metric("Python API", "Allowed" if python_allowed is True
                     else "Blocked" if python_allowed is False else "Not checked")
        if algo_allowed is False or python_allowed is False:
            st.error("MT5 is connected, but automatic orders are blocked by its permission settings. "
                     "Check the exact setting shown on Overview, then run this connection check again.")
    else:
        st.info("No MT5 account has been verified yet. Open Getting Started to enter the account shown in MT5.")
    st.caption("The connection check reads account, symbol, quote, and candles. It never submits an order.")
    if st.button("Check MT5 connection (no orders)", disabled=health["state"] not in ("STOPPED", "STALE", "FAILED"),
                 help="Refreshes account identity and Algo Trading/Python API permission status."):
        try:
            result = check_broker_connection()
            if result.get("broker", "").startswith("verified"):
                verified = result.get("account", {})
                st.success(f"Connected: {verified.get('environment')} account {verified.get('login')} "
                           f"on {verified.get('server')}. MT5 Algo Trading: "
                           f"{'On' if verified.get('terminal_algo_trading_allowed') else 'Off'}; Python API: "
                           f"{'Allowed' if verified.get('external_api_allowed') else 'Blocked'}.")
            else:
                st.error(result.get("broker", "MT5 connection could not be verified."))
        except Exception as exc:
            st.error(f"Connection check failed: {exc}")
    st.subheader("Your saved setup")
    st.write("Account type, strategy, risk limits, execution preference, and Telegram update choices are edited together.")
    if st.button("Edit account, strategy, risk, and Telegram settings", help="Opens the guided setup. Changes are saved locally without editing configuration files."):
        st.session_state["workspace"] = "Getting Started"
        st.rerun()
    with st.expander("Advanced configuration details"):
        st.dataframe(pd.DataFrame(flatten_values(settings.public())), width="stretch", hide_index=True)
    st.subheader("Control command status")
    display_records("commands", 30)
    st.subheader("Manage saved data")
    st.caption("These controls remove only local saved copies. MT5 account history remains at your broker. "
               "Your active safety halt, order intents, current account, and open-position snapshot are preserved.")
    active_research_jobs = store.active_research_jobs()
    research_busy = bool(active_research_jobs)
    if research_busy:
        st.info("A historical analysis is still running. Data deletion will become available when it finishes.")
    cleanup_tab, candles_tab, account_tab = st.tabs(["Backtests", "Market candles", "Demo / live history"])
    with cleanup_tab:
        preview = store.research_cleanup_preview()
        saved_runs = sum(preview["counts"].values())
        st.write(f"Saved research records: **{saved_runs:,}** "
                 f"(results {preview['counts']['backtest_results']:,}, "
                 f"experiments {preview['counts']['experiments']:,}, approvals {preview['counts']['approvals']:,}).")
        st.caption("This clears saved result tables and generated reports. Market candles and one-time holdout protection remain.")
        clear_research = st.checkbox("I want to remove the saved backtest results and reports",
                                     key="confirm_clear_research")
        if st.button("Delete saved backtest results", disabled=not clear_research or not saved_runs or research_busy,
                     help="Removes saved research rows, validation approvals, and generated reports inside the reports folder."):
            removed_reports, failures = remove_saved_reports(preview["report_paths"])
            if failures:
                st.error("Some report files could not be removed, so saved research references were kept: " + "; ".join(failures))
            else:
                result = store.clear_research_history()
                st.success(f"Deleted {sum(result['counts'].values()):,} research records and {removed_reports:,} report folders. "
                           "Historical candles and the holdout-used safeguard remain.")
                st.rerun()

    with candles_tab:
        with store.engine.connect() as connection:
            data_rows = connection.execute(select(candle_table.c.source, candle_table.c.symbol,
                candle_table.c.timeframe).distinct()).all()
        data_sources = sorted({row.source for row in data_rows})
        if not data_rows:
            st.info("No locally saved market candles to clear.")
        else:
            selected_source = st.selectbox("Data provider", data_sources, key="cleanup_candle_source")
            data_symbols = sorted({row.symbol for row in data_rows if row.source == selected_source})
            selected_data_symbol = st.selectbox("Market", data_symbols, key="cleanup_candle_symbol")
            data_timeframes = sorted({row.timeframe for row in data_rows if row.source == selected_source
                                      and row.symbol == selected_data_symbol},
                                     key=lambda tf: ("M1 M5 M15 M30 H1 H4 D1".split().index(tf)
                                                     if tf in "M1 M5 M15 M30 H1 H4 D1".split() else 99))
            selected_timeframes = st.multiselect("Timeframes to clear", data_timeframes,
                default=data_timeframes, help="Only the selected market and timeframes are affected.")
            date_mode = st.radio("How much candle history?", ["All saved dates", "Only dates before a cutoff"],
                                 horizontal=True, key="cleanup_candle_date_mode")
            cutoff = None
            if date_mode == "Only dates before a cutoff":
                cutoff_day = st.date_input("Remove candles before (UTC)", value=utcnow().date(), key="cleanup_candle_cutoff")
                cutoff = f"{cutoff_day.isoformat()}T00:00:00+00:00"
            candle_count = store.count_candles(selected_source, selected_data_symbol, selected_timeframes, cutoff)
            st.write(f"Matching saved candles: **{candle_count:,}**")
            st.caption("Deleted candles can be downloaded again from MT5 when that history is available.")
            confirm_candles = st.checkbox("I understand these local market candles will be removed",
                                          key="confirm_clear_candles")
            if st.button("Delete selected candle data", disabled=not confirm_candles or candle_count == 0 or research_busy):
                removed = store.clear_candles(selected_source, selected_data_symbol, selected_timeframes, cutoff)
                st.success(f"Deleted {removed:,} local candles for {selected_data_symbol}.")
                st.rerun()

    with account_tab:
        if not account_key or not account:
            st.info("Connect to an MT5 account first. Only the currently verified demo/live account can be selected here.")
        else:
            environment = account.get("environment", "Unknown")
            st.write(f"Selected account: **{environment}** • {account.get('server')} • login {account.get('login')}")
            if health["state"] not in ("STOPPED", "STALE", "FAILED"):
                st.warning("Stop the monitor before deleting old account history.")
            cutoff_day = st.date_input("Remove account records dated before (UTC)",
                value=(utcnow() - pd.Timedelta(days=90)).date(), key="cleanup_account_cutoff")
            cutoff = f"{cutoff_day.isoformat()}T00:00:00+00:00"
            history_preview: dict | None = None
            try:
                preview_result = store.account_history_cleanup_preview(account_key, cutoff)
                history_preview = preview_result
                total_records = sum(preview_result["counts"].values()) + preview_result["completed_signal_outcomes"]
                st.write(f"Older local records to remove: **{total_records:,}**. "
                         f"Completed simulated signal outcomes: {preview_result['completed_signal_outcomes']:,}.")
                if preview_result["open_positions_kept"]:
                    st.info(f"{preview_result['open_positions_kept']} currently saved open position(s) and their broker deals will be kept.")
            except ValueError as exc:
                st.error(str(exc))
                total_records = 0
            st.caption("Keeps unresolved order records, all safety halts and risk baselines, and any open position. "
                       "Old broker deals are hidden locally from this cutoff onward and won’t be re-imported by the app.")
            confirm_history = st.checkbox(f"I want to delete old local {environment.lower()} account history",
                                          key="confirm_clear_account_history")
            if st.button("Delete old account history", disabled=not confirm_history or not total_records
                         or history_preview is None or research_busy
                         or health["state"] not in ("STOPPED", "STALE", "FAILED")):
                result = store.clear_account_history(account_key, cutoff)
                st.success(f"Removed {sum(result['deleted'].values()) + result['completed_signal_outcomes']:,} old local records. "
                           "Open exposure, order intents, and safety state were kept.")
                st.rerun()
