from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest


@pytest.mark.parametrize("page", ["Overview", "Market Analysis", "Strategies", "Backtesting Lab", "Trade Journal",
                                   "Risk Center", "System Logs", "Settings"])
def test_dashboard_pages_render_empty_honest_state(tmp_path, monkeypatch, page):
    monkeypatch.setenv("XAU_DATABASE_URL", f"sqlite:///{tmp_path / 'dashboard.db'}")
    application = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app/dashboard/home.py"))
    application.run(timeout=30)
    application.sidebar.radio[0].set_value(page).run(timeout=30)
    assert not application.exception
    assert application.title[0].value == page


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
    application.sidebar.radio[0].set_value(page).run(timeout=30)
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
    application.sidebar.radio[0].set_value("Backtesting Lab").run(timeout=30)
    application.checkbox[0].set_value(True).run(timeout=30)
    assert not application.exception
    assert len(application.get("plotly_chart")) == 1
    assert "Drawdown %" in (report / "report.html").read_text(encoding="utf-8")
