import pytest

from app.backtesting.engine import CostProfile
from app.backtesting.reports import export_report
from app.backtesting.research import research
from app.domain import digest


def test_reports_are_reproducible_and_not_approval(broker, tmp_path):
    from tests.test_backtesting import fixture_run
    first, second = fixture_run(broker), fixture_run(broker)
    assert first["metrics"] == second["metrics"]
    assert first["dataset_hash"] == second["dataset_hash"]
    folder = export_report(first, tmp_path, "Synthetic software verification only")
    assert all((folder / name).is_file() for name in ("report.html", "trades.csv", "equity.csv", "summary.json"))


def test_holdout_cannot_be_reopened(store, synthetic_frames, broker, tmp_path):
    dataset_hash = digest({tf: f.to_json(date_format="iso") for tf, f in synthetic_frames.items()})
    store.set(f"holdout_consumed:{dataset_hash}", {"reason": "already inspected"})
    costs = CostProfile(commission_per_lot=0, spread_price=.2, slippage_price=.1,
                        swap_long_per_lot_day=0, swap_short_per_lot_day=0)
    with pytest.raises(ValueError, match="already consumed"):
        research(store, synthetic_frames, ["trend_pullback"], broker.spec("XAUUSD"), costs,
                 tmp_path, consume_holdout=True)


def test_complete_short_sample_comparison_reports_no_approval(store, synthetic_frames, broker, tmp_path):
    frames = {"M15": synthetic_frames["M15"].iloc[:1000], "H1": synthetic_frames["H1"].iloc[:250]}
    cost = CostProfile(commission_per_lot=5, spread_price=.2, slippage_price=.1,
                        swap_long_per_lot_day=-1, swap_short_per_lot_day=-1)
    result = research(store, frames, ["trend_pullback", "session_breakout", "mean_reversion"],
                      broker.spec("XAUUSD"), cost, tmp_path)
    assert len(result["candidates"]) == 3
    assert result["status"] == "NO STRATEGY APPROVED FOR DEMO AUTOMATION"
    assert not result["approved"]
    assert store.list("backtest_results")
    from pathlib import Path
    report = Path(result["summary_report"])
    assert (report / "comparisons.csv").is_file() and (report / "report.html").is_file()
