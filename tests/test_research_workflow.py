import pytest

from app.backtesting.engine import CostProfile
from app.backtesting.reports import export_report
from app.backtesting.research import apply_stop_target_parameters, parameter_grid, research
from app.domain import digest
from app.strategies.registry import build


@pytest.mark.parametrize("name", ["ict_sweep_mss_fvg", "support_resistance_bounce"])
def test_parameter_grid_signal_reuse_matches_direct_strategy_signals(name, synthetic_frames):
    base = {"entry_timeframe": "M15"}
    template_strategy = build(name, base)
    frames = {"M15": synthetic_frames["M15"]}
    templates = template_strategy.signals(frames, "XAUUSD")

    for variation in parameter_grid():
        candidate = build(name, {**base, **variation})
        reused = apply_stop_target_parameters(templates, candidate)
        direct = candidate.signals(frames, "XAUUSD")
        assert [signal.model_dump(mode="json") for signal in reused] == [
            signal.model_dump(mode="json") for signal in direct]


def test_reports_are_reproducible_and_not_approval(broker, tmp_path):
    from tests.test_backtesting import fixture_run
    first, second = fixture_run(broker), fixture_run(broker)
    assert first["metrics"] == second["metrics"]
    assert first["dataset_hash"] == second["dataset_hash"]
    folder = export_report(first, tmp_path, "Synthetic software verification only")
    assert all((folder / name).is_file() for name in (
        "report.html", "trades.csv", "equity.csv", "summary.json", "entry_decisions.csv"))
    import json

    import pandas as pd
    saved = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
    assert "entry_decisions" not in saved  # Detailed decisions have a typed CSV, not a string in JSON.
    assert saved["execution_audit"]["filled"] == len(pd.read_csv(folder / "trades.csv"))


def test_holdout_cannot_be_reopened(store, synthetic_frames, broker, tmp_path):
    dataset_hash = digest({tf: f.to_json(date_format="iso") for tf, f in synthetic_frames.items()})
    store.set(f"holdout_consumed:{dataset_hash}", {"reason": "already inspected"})
    costs = CostProfile(commission_per_lot=0, spread_price=.2, slippage_price=.1,
                        swap_long_per_lot_day=0, swap_short_per_lot_day=0)
    with pytest.raises(ValueError, match="already consumed"):
        research(store, synthetic_frames, ["ict_sweep_mss_fvg"], broker.spec("XAUUSD"), costs,
                 tmp_path, consume_holdout=True)


def test_automatic_cost_estimates_cannot_consume_holdout(store, broker, tmp_path):
    costs = CostProfile(commission_per_lot=0, spread_price=.5, slippage_price=.2,
                        swap_long_per_lot_day=0, swap_short_per_lot_day=0,
                        costs_verified=False, source="automatic test estimates")
    with pytest.raises(ValueError, match="Verify broker commission"):
        research(store, {}, [], broker.spec("XAUUSD"), costs, tmp_path, consume_holdout=True)
    assert not any(row["key"].startswith("holdout_consumed:") for row in store.list_state("holdout_consumed:"))


def test_complete_short_sample_comparison_reports_no_approval(store, synthetic_frames, broker, tmp_path):
    frames = {"M15": synthetic_frames["M15"].iloc[:1000]}
    cost = CostProfile(commission_per_lot=5, spread_price=.2, slippage_price=.1,
                        swap_long_per_lot_day=-1, swap_short_per_lot_day=-1)
    result = research(store, frames, ["ict_sweep_mss_fvg", "support_resistance_bounce"],
                      broker.spec("XAUUSD"), cost, tmp_path)
    assert len(result["candidates"]) == 2
    assert result["status"] == "NO STRATEGY APPROVED FOR AUTOMATION"
    assert not result["approved"]
    assert store.list("backtest_results")
    from pathlib import Path
    report = Path(result["summary_report"])
    assert (report / "comparisons.csv").is_file() and (report / "report.html").is_file()


def test_development_research_accepts_hourly_entry_data(store, synthetic_frames, broker, tmp_path):
    cost = CostProfile(commission_per_lot=0, spread_price=.5, slippage_price=.2,
                       swap_long_per_lot_day=0, swap_short_per_lot_day=0)
    result = research(store, {"H1": synthetic_frames["H1"]}, ["support_resistance_bounce"],
                      broker.spec("XAUUSD"), cost, tmp_path)
    assert result["entry_timeframe"] == "H1"
    assert not result["approved"]
