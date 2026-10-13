from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest


@pytest.mark.parametrize("page", ["Getting Started", "Overview", "Market Analysis", "Strategies", "Backtesting Lab",
                                   "Trade Journal", "Risk Center", "System Logs", "Settings"])
def test_dashboard_pages_render_empty_honest_state(tmp_path, monkeypatch, page):
    monkeypatch.setenv("XAU_DATABASE_URL", f"sqlite:///{tmp_path / 'dashboard.db'}")
    application = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app/dashboard/home.py"))
    application.run(timeout=30)
    application.sidebar.selectbox[0].set_value(page).run(timeout=30)
    assert not application.exception
    assert application.title[0].value == page


def test_dashboard_execution_mode_change_queues_command_and_stays_disarmed(tmp_path, monkeypatch):
    from app.database.storage import Store

    database_url = f"sqlite:///{tmp_path / 'dashboard.db'}"
    monkeypatch.setenv("XAU_DATABASE_URL", database_url)
    store = Store(database_url)
    store.create_for_tests()
    store.set("active_account", {"server": "Exness-Demo", "login": 12345})
    store.set("execution_mode:Exness-Demo:12345", "SIGNAL_ONLY")
    store.set("armed:Exness-Demo:12345", {"active": False})
    store.set("setup_complete", True)

    application = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app/dashboard/home.py"))
    application.run(timeout=30)
    application.radio[0].set_value("AUTO_DEMO").run(timeout=30)

    assert not application.exception
    queued = [command for command in store.list("commands") if command["kind"] == "set-execution-mode"]
    assert queued[-1]["state"] == "PENDING"
    assert queued[-1]["payload"]["mode"] == "AUTO_DEMO"
    assert store.get("armed:Exness-Demo:12345") == {"active": False}


def test_dashboard_lands_on_guided_setup_with_auto_selected(tmp_path, monkeypatch):
    monkeypatch.setenv("XAU_DATABASE_URL", f"sqlite:///{tmp_path / 'dashboard.db'}")
    application = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app/dashboard/home.py"))
    application.run(timeout=30)

    assert not application.exception
    assert application.title[0].value == "Getting Started"
    assert application.radio[0].value == "AUTO_DEMO"
    assert any("Save once to finish setup" in item.value for item in application.info)


def test_backtest_starting_balance_is_independent_of_connected_account(tmp_path, monkeypatch):
    from app.database.storage import Store

    database_url = f"sqlite:///{tmp_path / 'dashboard.db'}"
    monkeypatch.setenv("XAU_DATABASE_URL", database_url)
    store = Store(database_url)
    store.create_for_tests()
    store.set("active_account", {"server": "Exness-Demo", "login": 12345, "balance": 989.68})
    store.set("setup_complete", True)

    application = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app/dashboard/home.py"))
    application.run(timeout=30)
    application.sidebar.selectbox[0].set_value("Backtesting Lab").run(timeout=30)

    assert not application.exception
    capital = next(item for item in application.number_input
                   if item.label == "Starting balance for this backtest (USD)")
    assert capital.value == 1000.0


def test_dashboard_labels_live_as_real_money_and_defaults_auto(tmp_path, monkeypatch):
    monkeypatch.setenv("XAU_DATABASE_URL", f"sqlite:///{tmp_path / 'dashboard.db'}")
    monkeypatch.setenv("XAU_MODE", "LIVE")
    monkeypatch.setenv("XAU_ACCOUNT_LOGIN", "12345")
    monkeypatch.setenv("XAU_ACCOUNT_SERVER", "Exness-Real")
    monkeypatch.setenv("XAU_MT5_PATH", "C:/Program Files/MetaTrader 5/terminal64.exe")
    application = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app/dashboard/home.py"))
    application.run(timeout=30)

    assert not application.exception
    assert application.radio[0].value == "AUTO_LIVE"
    assert any("REAL MONEY" in item.value for item in application.error)


@pytest.mark.parametrize("page", ["Overview", "Market Analysis", "Trade Journal", "Risk Center", "System Logs"])
def test_dashboard_renders_persisted_fixture_records(
        monkeypatch, page, settings, store, broker, signal, book, schedule, permission, synthetic_frames):
    from app.domain import utcnow
    from app.execution.engine import ExecutionEngine
    # This database belongs to pytest's temporary directory. No fixtures reach the user's dashboard.
    monkeypatch.setenv("XAU_DATABASE_URL", str(store.engine.url))
    store.set("active_account", broker.account())
    store.set("active_symbol", "XAUUSD")
    store.set("latest_quote", broker.quote("XAUUSD"))
    store.record("equity_snapshots", broker.account(), broker.account().key)
    store.save_candles(synthetic_frames["M15"].tail(300), broker.account().server, "XAUUSD", "M15", "bid")
    ExecutionEngine(settings, store, broker).execute(signal, utcnow(), book, schedule)
    for deal in broker.history(utcnow(), utcnow()):
        store.record("broker_deals", deal, broker.account().key)
    application = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app/dashboard/home.py"))
    application.run(timeout=30)
    application.sidebar.selectbox[0].set_value(page).run(timeout=30)
    assert not application.exception
    if page == "Trade Journal":
        assert len(application.multiselect) == 3
        application.multiselect[1].set_value(["SHORT"]).run(timeout=30)
        assert not application.exception
        assert application.dataframe[0].value.empty


def test_research_report_chart_loads_from_persisted_export(tmp_path, store, broker, monkeypatch):
    from app.backtesting.reports import export_report
    from tests.test_backtesting import fixture_run
    report_dir = tmp_path / "fixture_reports"
    result = fixture_run(broker)
    report = export_report(result, report_dir, "Synthetic software fixture")
    store.record("backtest_results", {"report": str(report.resolve()), "metrics": result["metrics"]})
    monkeypatch.setenv("XAU_DATABASE_URL", str(store.engine.url))
    monkeypatch.setenv("XAU_REPORT_DIR", str(report_dir))
    application = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app/dashboard/home.py"))
    application.run(timeout=30)
    application.sidebar.selectbox[0].set_value("Backtesting Lab").run(timeout=30)
    next(box for box in application.checkbox if box.label == "Show equity and drawdown").set_value(True).run(timeout=30)
    assert not application.exception
    assert len(application.get("plotly_chart")) == 1
    assert "Drawdown %" in (report / "report.html").read_text(encoding="utf-8")
    assert any("VolumeLots" in frame.value.columns for frame in application.dataframe)


def test_dashboard_marks_old_broker_position_snapshot_and_unrecognized_exposure(
        tmp_path, monkeypatch, store, broker):
    from datetime import UTC, datetime

    monkeypatch.setenv("XAU_DATABASE_URL", str(store.engine.url))
    account = broker.account().model_dump(mode="json")
    store.set("active_account", account)
    store.set("active_symbol", "XAUUSD")
    store.set(f"reconciliation:{broker.account().key}", {
        "safe": False, "reasons": ["Unexpected account exposure"],
        "timestamp": datetime(2026, 10, 1, tzinfo=UTC).isoformat(),
        "positions": [{"ticket": 99, "symbol": "XAUUSD", "direction": "LONG", "volume": .01,
                       "entry": 2000., "stop_loss": 1990., "take_profit": 2020., "profit": -1.,
                       "magic": 0}],
    })
    application = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app/dashboard/home.py"))
    application.run(timeout=30)
    application.sidebar.selectbox[0].set_value("Overview").run(timeout=30)

    assert not application.exception
    messages = [entry.value for entry in application.warning]
    assert any("list may be stale" in message for message in messages)
    assert any("manual or unrecognized" in message for message in messages)


def test_dashboard_marks_daily_pnl_snapshot_stale(monkeypatch, store, broker):
    from app.domain import utcnow

    monkeypatch.setenv("XAU_DATABASE_URL", str(store.engine.url))
    account = broker.account().model_dump(mode="json")
    store.set("active_account", account)
    account_key = broker.account().key
    store.set("risk:" + account_key, {"daily_pnl": -5.18, "weekly_pnl": -5.18,
        "last_seen": "2020-01-01T00:00:00+00:00", "baselines_verified": False})
    halt_time = utcnow().isoformat()
    store.set("halt:" + account_key, {"active": True, "reason": "Broker or data verification failed",
                                       "timestamp": halt_time})
    store.record("errors", {"type": "BrokerError",
                             "message": "Account trading or external API permission is disabled"}, account_key)
    store.set("trading_permissions:" + account_key, {"blocked": False, "trade_allowed": True,
                                                      "external_api_allowed": True})
    application = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app/dashboard/home.py"))
    application.run(timeout=30)
    application.sidebar.selectbox[0].set_value("Overview").run(timeout=30)

    assert not application.exception
    assert any("Risk figures are the last saved check" in item.value for item in application.warning)
    assert any("Refresh account, trades, and P&L from MT5" in item.label for item in application.button)
    assert any("trading monitor is stopped" in item.value for item in application.warning)
    assert any("Original cause" in item.value and "MT5 now reports account trading and Python API access allowed"
               in item.value for item in application.info)

    application.sidebar.selectbox[0].set_value("Backtesting Lab").run(timeout=30)
    assert not application.exception
    inputs = {item.label for item in application.number_input}
    metrics = {item.label for item in application.metric}
    assert "Starting balance for this backtest (USD)" in inputs
    assert "Risk per trade (%)" in inputs
    assert "Daily loss stop (%)" in inputs
    assert "Weekly loss stop (%)" in inputs
    assert "Combined open risk cap (%)" in inputs
    assert {"Risk per trade", "Daily loss limit", "Weekly loss limit",
            "Per-trade risk limit", "Combined open-risk limit"} <= metrics
