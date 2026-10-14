from pathlib import Path

import pandas as pd

from app.backtesting.campaign import DEFAULT_TIMEFRAMES, run_campaign
from app.backtesting.engine import CostProfile
from app.data.validation import TIMEFRAMES
from app.strategies.registry import REGISTRY


def test_campaign_runs_both_strategies_on_all_five_intervals(store, synthetic_frames, broker, tmp_path):
    frames = {}
    for timeframe in DEFAULT_TIMEFRAMES:
        frame = synthetic_frames["M15"].iloc[:300].copy()
        frame.index = pd.date_range("2024-01-01", periods=len(frame), freq=f"{TIMEFRAMES[timeframe]}min", tz="UTC")
        frames[timeframe] = frame
    cost = CostProfile(commission_per_lot=0, spread_price=0, slippage_price=0,
                       swap_long_per_lot_day=0, swap_short_per_lot_day=0)
    result = run_campaign(store, frames, list(REGISTRY), broker.spec("XAUUSD"), cost, tmp_path)
    assert len(result["comparisons"]) == 10 and len(result["runs"]) == 20
    assert {(run["strategy"], run["entry_timeframe"], run["mode"]) for run in result["runs"]} == {
        (name, tf, mode) for name in REGISTRY for tf in DEFAULT_TIMEFRAMES for mode in ("account", "setup_study")}
    for run in result["runs"]:
        decisions = pd.read_csv(Path(run["report"]) / "entry_decisions.csv")
        trades = pd.read_csv(Path(run["report"]) / "trades.csv")
        assert len(decisions) == run["candidate_signal_count"]
        assert len(trades) == run["metrics"]["total_trades"]
        assert run["execution_audit"]["filled"] + run["execution_audit"]["rejected"] == len(decisions)
        if run["mode"] == "account" and len(trades):
            filled = decisions.loc[decisions.status == "filled"]
            assert (filled.initial_risk_usd <= filled.risk_budget_usd + 1e-8).all()
            assert pd.to_datetime(trades.EntryTime).dt.date.value_counts().max() <= 10
    root = Path(result["summary_report"])
    assert (root / "report.html").is_file() and (root / "plotly.min.js").is_file()
    assert store.list("backtest_results")[-1]["payload"]["timeframes"] == list(DEFAULT_TIMEFRAMES)


def test_campaign_records_missing_intervals_instead_of_silently_dropping_them(store, synthetic_frames, broker, tmp_path):
    cost = CostProfile(commission_per_lot=0, spread_price=0, slippage_price=0,
                       swap_long_per_lot_day=0, swap_short_per_lot_day=0)
    result = run_campaign(store, {"M15": synthetic_frames["M15"].iloc[:50]}, list(REGISTRY),
                          broker.spec("XAUUSD"), cost, tmp_path, include_setup_study=False)
    missing = [row["timeframe"] for row in result["coverage"] if row["status"] == "NO EVALUATION HISTORY"]
    assert missing == ["M1", "M5", "M30", "H1"]
    assert len(result["runs"]) == 2


def test_cli_default_dispatches_every_interval_and_saves_a_single_comparison(
        store, broker, synthetic_frames, tmp_path, monkeypatch, capsys):
    from app.config.settings import Settings
    from app.main import main

    source, symbol = "Exness-Demo", "XAUUSD"
    for timeframe in DEFAULT_TIMEFRAMES:
        frame = synthetic_frames["M15"].iloc[:30].copy()
        frame.index = pd.date_range("2024-01-01", periods=len(frame), freq=f"{TIMEFRAMES[timeframe]}min", tz="UTC")
        store.save_candles(frame, source, symbol, timeframe, "bid")
    store.set(f"spec:{source}:{symbol}", broker.spec(symbol))
    settings = Settings(_env_file=None, database_url=str(store.engine.url), report_dir=tmp_path / "reports")
    monkeypatch.setattr("app.main.Settings", lambda: settings)
    monkeypatch.setattr("app.main.configured", lambda store: settings)
    monkeypatch.chdir(tmp_path)
    assert main(["backtest", "--source", source, "--symbol", symbol,
                 "--start", "2024-01-01", "--end", "2024-01-03"]) == 0
    import json
    output = json.loads(capsys.readouterr().out)
    assert len(output["comparisons"]) == 10
    assert output["risk_limits"]["consecutive_loss_policy"] == "cooldown"
    assert output["initial_capital"] == 1000
    assert (Path(output["summary_report"]) / "batch.zip").is_file()
