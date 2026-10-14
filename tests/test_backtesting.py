from datetime import timedelta

import pandas as pd
import pytest

from app.backtesting.engine import BacktestRiskLimits, CostProfile, run_backtest
from app.backtesting.research import partitions, walk_forward_windows
from app.domain import Direction, Signal


def fixture_run(broker, direction=Direction.LONG, ambiguous=True, gap=False, cost=None, risk_fraction=.0025):
    index = pd.date_range("2024-01-01", periods=8, freq="15min", tz="UTC")
    frame = pd.DataFrame({"open": 2000., "high": 2001., "low": 1999., "close": 2000., "tick_volume": 1}, index=index)
    frame.loc[index[3], ["high", "low"]] = [2025, 1985] if ambiguous else [2005, 1995]
    if gap:
        frame.loc[index[3], ["open", "high", "low", "close"]] = [1980, 1982, 1979, 1981]
    sign = 1 if direction == Direction.LONG else -1
    stamp = (index[1] + timedelta(minutes=15)).to_pydatetime()
    signal = Signal(symbol="XAUUSD", strategy="test", config_hash="test", direction=direction,
                    timestamp=stamp, entry=2000, stop_loss=2000 - sign * 10, take_profit=2000 + sign * 20,
                    rationale="Synthetic test fixture", regime="test", indicators={"atr": 5})
    cost = cost or CostProfile(commission_per_lot=0, spread_price=0, slippage_price=0,
                              swap_long_per_lot_day=0, swap_short_per_lot_day=0)
    return run_backtest({"M15": frame, "H1": frame}, [signal], broker.spec("XAUUSD"), cost,
                        risk_fraction=risk_fraction)


@pytest.mark.parametrize("direction", [Direction.LONG, Direction.SHORT])
def test_stop_first_ambiguous_candle(broker, direction):
    result = fixture_run(broker, direction)
    assert len(result["trades"]) == 1
    assert result["trades"].PnL.iloc[0] == -20
    assert result["trades"].EntryTime.iloc[0] == pd.Timestamp("2024-01-01 00:30", tz="UTC")


def test_adverse_stop_gap(broker):
    result = fixture_run(broker, gap=True)
    assert result["trades"].ExitPrice.iloc[0] == 1980
    assert result["trades"].PnL.iloc[0] == -40


def test_transaction_costs_and_lot_conversion(broker):
    cost = CostProfile(commission_per_lot=5, spread_price=.2, slippage_price=.1,
                       swap_long_per_lot_day=0, swap_short_per_lot_day=0)
    result = fixture_run(broker, cost=cost)
    trade = result["trades"].iloc[0]
    assert trade.VolumeLots == .02
    assert trade.EntryPrice == pytest.approx(2000.3)
    assert trade.ExitPrice == pytest.approx(1989.9)
    assert trade.PnL == pytest.approx(-21)
    assert result["metrics"]["estimated_transaction_costs"] > 0


def test_degenerate_metrics_are_null(broker):
    result = fixture_run(broker)
    assert result["metrics"]["annualized_return"] is None
    assert result["metrics"]["sharpe"] is None
    assert result["metrics"]["win_rate"] == 0


def test_backtest_allows_user_risk_above_legacy_half_percent_cap(broker):
    result = fixture_run(broker, risk_fraction=.01)
    assert len(result["trades"]) == 1


def test_partition_time_order_and_holdout_exclusion(synthetic_frames):
    start, validation, holdout, end = partitions(synthetic_frames["M15"])
    assert start < validation < holdout < end
    assert all(test_end <= holdout for _, _, test_end in walk_forward_windows(start, holdout))


def test_unsupported_currency_is_rejected(broker):
    cost = CostProfile(commission_per_lot=0, spread_price=0, slippage_price=0,
                       swap_long_per_lot_day=0, swap_short_per_lot_day=0, account_currency="EUR")
    with pytest.raises(ValueError, match="USD"):
        fixture_run(broker, cost=cost)


def test_short_stop_uses_ask_trigger(broker):
    index = pd.date_range("2024-01-01", periods=8, freq="15min", tz="UTC")
    frame = pd.DataFrame({"open": 2000., "high": 2001., "low": 1999., "close": 2000.,
                          "tick_volume": 1}, index=index)
    # Bid never reaches 2010, but the ask does with a 0.20 spread.
    frame.loc[index[3], "high"] = 2009.9
    signal = Signal(symbol="XAUUSD", strategy="test", config_hash="test", direction=Direction.SHORT,
                    timestamp=index[2].to_pydatetime(), entry=2000, stop_loss=2010, take_profit=1980,
                    rationale="Synthetic ask-side stop fixture", regime="test", indicators={"atr": 5})
    cost = CostProfile(commission_per_lot=0, spread_price=.2, slippage_price=.1,
                       swap_long_per_lot_day=0, swap_short_per_lot_day=0)
    result = run_backtest({"M15": frame}, [signal], broker.spec("XAUUSD"), cost)
    trade = result["trades"].iloc[0]
    assert trade.ExitTime == index[3]
    assert trade.ExitPrice == pytest.approx(2010.1)
    assert trade.PnL == pytest.approx(-20.4)


def test_financing_reconciles_trade_and_equity(broker):
    index = pd.date_range("2024-01-01", periods=300, freq="15min", tz="UTC")
    frame = pd.DataFrame({"open": 2000., "high": 2001., "low": 1999., "close": 2000.,
                          "tick_volume": 1}, index=index)
    signal = Signal(symbol="XAUUSD", strategy="test", config_hash="test", direction=Direction.LONG,
                    timestamp=index[2].to_pydatetime(), entry=2000, stop_loss=1990, take_profit=2020,
                    exit_at=index[2].to_pydatetime() + timedelta(days=2),
                    rationale="Synthetic funding fixture", regime="test", indicators={"atr": 5})
    cost = CostProfile(commission_per_lot=5, spread_price=.2, slippage_price=.1,
                       swap_long_per_lot_day=-3, swap_short_per_lot_day=-2)
    result = run_backtest({"M15": frame}, [signal], broker.spec("XAUUSD"), cost)
    trade = result["trades"].iloc[0]
    assert trade.VolumeLots == .02
    assert trade.Financing == pytest.approx(-.12)
    assert trade.ExitTime == pd.Timestamp(signal.exit_at)
    assert result["equity"].Equity.iloc[-1] - 10000 == pytest.approx(trade.PnL)


def test_persistent_consecutive_loss_limit_blocks_later_entries(broker):
    index = pd.date_range("2024-01-01", periods=10, freq="15min", tz="UTC")
    frame = pd.DataFrame({"open": 2000., "high": 2001., "low": 1999., "close": 2000.,
                          "tick_volume": 1}, index=index)
    frame.loc[index[3], ["high", "low"]] = [2025, 1985]
    frame.loc[index[6], ["high", "low"]] = [2025, 1985]
    signals = [Signal(symbol="XAUUSD", strategy="test", config_hash="test", direction=Direction.LONG,
        timestamp=index[2].to_pydatetime(), entry=2000, stop_loss=1990, take_profit=2020,
        rationale="first expected loss", regime="test", indicators={"atr": 5}),
        Signal(symbol="XAUUSD", strategy="test", config_hash="test", direction=Direction.LONG,
        timestamp=index[4].to_pydatetime(), entry=2000, stop_loss=1990, take_profit=2020,
        rationale="blocked after loss", regime="test", indicators={"atr": 5})]
    cost = CostProfile(commission_per_lot=0, spread_price=0, slippage_price=0,
                       swap_long_per_lot_day=0, swap_short_per_lot_day=0)
    result = run_backtest({"M15": frame}, signals, broker.spec("XAUUSD"), cost,
                          risk_limits=BacktestRiskLimits(consecutive_loss_limit=1))
    assert len(result["trades"]) == 1
    assert result["risk_blocks"]["consecutive_loss_limit"] == 1


def test_daily_loss_limit_blocks_entries_after_equity_breaches_threshold(broker):
    index = pd.date_range("2024-01-01", periods=10, freq="15min", tz="UTC")
    frame = pd.DataFrame({"open": 2000., "high": 2001., "low": 1999., "close": 2000.,
                          "tick_volume": 1}, index=index)
    frame.loc[index[3], ["high", "low"]] = [2025, 1985]
    signals = [Signal(symbol="XAUUSD", strategy="test", config_hash="test", direction=Direction.LONG,
        timestamp=index[2].to_pydatetime(), entry=2000, stop_loss=1990, take_profit=2020,
        rationale="first expected loss", regime="test", indicators={"atr": 5}),
        Signal(symbol="XAUUSD", strategy="test", config_hash="test", direction=Direction.LONG,
        timestamp=index[4].to_pydatetime(), entry=2000, stop_loss=1990, take_profit=2020,
        rationale="blocked by daily loss", regime="test", indicators={"atr": 5})]
    cost = CostProfile(commission_per_lot=0, spread_price=0, slippage_price=0,
                       swap_long_per_lot_day=0, swap_short_per_lot_day=0)
    limits = BacktestRiskLimits(daily_loss_limit=0.001, consecutive_loss_limit=5)
    result = run_backtest({"M15": frame}, signals, broker.spec("XAUUSD"), cost, risk_limits=limits)
    assert len(result["trades"]) == 1
    assert result["risk_blocks"]["daily_loss_limit"] == 1


def test_configured_position_count_allows_more_than_one_simulated_position(broker):
    index = pd.date_range("2024-01-01", periods=10, freq="15min", tz="UTC")
    frame = pd.DataFrame({"open": 2000., "high": 2001., "low": 1999., "close": 2000.,
                          "tick_volume": 1}, index=index)
    signals = [Signal(symbol="XAUUSD", strategy="test", config_hash="test", direction=Direction.LONG,
        timestamp=index[2].to_pydatetime(), entry=2000, stop_loss=1990, take_profit=2020,
        rationale="first position", regime="test", indicators={"atr": 5}),
        Signal(symbol="XAUUSD", strategy="test", config_hash="test", direction=Direction.SHORT,
        timestamp=index[3].to_pydatetime(), entry=2000, stop_loss=2010, take_profit=1980,
        rationale="second position", regime="test", indicators={"atr": 5})]
    cost = CostProfile(commission_per_lot=0, spread_price=0, slippage_price=0,
                       swap_long_per_lot_day=0, swap_short_per_lot_day=0)
    limits = BacktestRiskLimits(max_positions=2, portfolio_risk_limit=0.005)
    result = run_backtest({"M15": frame}, signals, broker.spec("XAUUSD"), cost,
                          risk_fraction=0.001, risk_limits=limits)
    assert len(result["trades"]) == 2


def test_rejection_audit_counts_only_candidate_entries(broker):
    index = pd.date_range("2024-01-01", periods=8, freq="15min", tz="UTC")
    frame = pd.DataFrame({"open": 2000., "high": 2001., "low": 1999., "close": 2000.,
                          "tick_volume": 1}, index=index)
    signals = [Signal(symbol="XAUUSD", strategy="test", config_hash="test", direction=Direction.LONG,
        timestamp=index[2].to_pydatetime(), entry=2000, stop_loss=1990, take_profit=2020,
        rationale="first candidate", regime="test", indicators={"atr": 5}),
        Signal(symbol="XAUUSD", strategy="test", config_hash="test", direction=Direction.LONG,
        timestamp=index[4].to_pydatetime(), entry=2000, stop_loss=1990, take_profit=2020,
        rationale="candidate while the first position is open", regime="test", indicators={"atr": 5})]
    cost = CostProfile(commission_per_lot=0, spread_price=0, slippage_price=0,
                       swap_long_per_lot_day=0, swap_short_per_lot_day=0)

    result = run_backtest({"M15": frame}, signals, broker.spec("XAUUSD"), cost)

    assert result["candidate_signal_count"] == 2
    assert len(result["trades"]) == 1
    assert result["risk_blocks"]["max_positions"] == 1
    blocked = result["entry_decisions"].loc[result["entry_decisions"].reason == "max_positions"].iloc[0]
    assert blocked.risk_budget_usd == pytest.approx(25)
    assert blocked.estimated_minimum_lot_risk_usd == pytest.approx(10)
    assert blocked.minimum_capital == pytest.approx(4000)


def test_backtest_audits_minimum_lot_risk_rejections(broker):
    index = pd.date_range("2024-01-01", periods=8, freq="15min", tz="UTC")
    frame = pd.DataFrame({"open": 2000., "high": 2001., "low": 1999., "close": 2000.,
                          "tick_volume": 1}, index=index)
    signal = Signal(symbol="XAUUSD", strategy="test", config_hash="test", direction=Direction.LONG,
        timestamp=index[2].to_pydatetime(), entry=2000, stop_loss=1000, take_profit=3000,
        rationale="minimum lot exceeds risk budget", regime="test", indicators={"atr": 5})
    cost = CostProfile(commission_per_lot=0, spread_price=0, slippage_price=0,
                       swap_long_per_lot_day=0, swap_short_per_lot_day=0)

    result = run_backtest({"M15": frame}, [signal], broker.spec("XAUUSD"), cost,
                          capital=1000, risk_fraction=.0025)

    assert result["candidate_signal_count"] == 1
    assert result["trades"].empty
    assert result["risk_blocks"]["minimum_lot_risk"] == 1


def test_r_uses_the_all_in_initial_risk_including_costs(broker):
    cost = CostProfile(commission_per_lot=5, spread_price=.2, slippage_price=.1,
                       swap_long_per_lot_day=0, swap_short_per_lot_day=0)
    trade = fixture_run(broker, cost=cost)["trades"].iloc[0]
    assert trade.R == pytest.approx(trade.PnL / trade.InitialRiskUSD)
    assert trade.R == pytest.approx(-1)


def test_signal_expiry_blocks_a_next_bar_fill_after_missing_history(broker):
    index = pd.DatetimeIndex(["2024-01-01T00:00Z", "2024-01-01T00:15Z", "2024-01-02T00:00Z",
                              "2024-01-02T00:15Z", "2024-01-02T00:30Z"])
    frame = pd.DataFrame({"open": 2000., "high": 2001., "low": 1999., "close": 2000.,
                          "tick_volume": 1}, index=index)
    signal = Signal(symbol="XAUUSD", strategy="test", config_hash="test", direction=Direction.LONG,
                    timestamp=(index[1] + timedelta(minutes=15)).to_pydatetime(), entry=2000,
                    stop_loss=1990, take_profit=2020, expires_at=pd.Timestamp("2024-01-01T00:45Z"),
                    rationale="expired across an unresolved gap", regime="test", indicators={})
    cost = CostProfile(commission_per_lot=0, spread_price=0, slippage_price=0,
                       swap_long_per_lot_day=0, swap_short_per_lot_day=0)
    result = run_backtest({"M15": frame}, [signal], broker.spec("XAUUSD"), cost)
    assert result["trades"].empty
    assert result["risk_blocks"]["expired_signal"] == 1
    assert result["execution_audit"]["filled"] == 0


def test_funding_minimum_lot_rejection_is_counted_and_has_capital_estimate(broker):
    index = pd.date_range("2024-01-01", periods=8, freq="15min", tz="UTC")
    frame = pd.DataFrame({"open": 2000., "high": 2000.2, "low": 1999.8, "close": 2000.,
                          "tick_volume": 1}, index=index)
    signal = Signal(symbol="XAUUSD", strategy="test", config_hash="test", direction=Direction.LONG,
                    timestamp=index[2].to_pydatetime(), entry=2000, stop_loss=1998.5, take_profit=2005,
                    exit_at=(index[2] + timedelta(hours=24)).to_pydatetime(),
                    rationale="funding pushes the minimum lot above budget", regime="test", indicators={})
    cost = CostProfile(commission_per_lot=0, spread_price=.5, slippage_price=.2,
                       swap_long_per_lot_day=-51.64, swap_short_per_lot_day=0)
    result = run_backtest({"M15": frame}, [signal], broker.spec("XAUUSD"), cost, capital=1000)
    assert result["trades"].empty
    assert result["risk_blocks"]["minimum_lot_risk"] == 1
    assert result["entry_decisions"].iloc[0].minimum_capital == pytest.approx(1166.56)


def test_explicit_research_cooldown_resumes_after_the_loss_streak(broker):
    index = pd.date_range("2024-01-01", periods=12, freq="15min", tz="UTC")
    frame = pd.DataFrame({"open": 2000., "high": 2001., "low": 1999., "close": 2000.,
                          "tick_volume": 1}, index=index)
    frame.loc[index[[3, 8]], "low"] = 1985
    signals = [Signal(symbol="XAUUSD", strategy="test", config_hash="test", direction=Direction.LONG,
                      timestamp=index[i].to_pydatetime(), entry=2000, stop_loss=1990, take_profit=2020,
                      rationale="cooldown research fixture", regime="test", indicators={}) for i in [2, 4, 7]]
    cost = CostProfile(commission_per_lot=0, spread_price=0, slippage_price=0,
                       swap_long_per_lot_day=0, swap_short_per_lot_day=0)
    limits = BacktestRiskLimits(consecutive_loss_limit=1, consecutive_loss_policy="cooldown",
                                loss_cooldown_minutes=30)
    result = run_backtest({"M15": frame}, signals, broker.spec("XAUUSD"), cost, risk_limits=limits)
    assert len(result["trades"]) == 2
    assert result["risk_blocks"]["consecutive_loss_limit"] == 1
    assert result["execution_audit"]["filled"] + result["execution_audit"]["rejected"] == 3


@pytest.mark.parametrize("limit", ["daily_loss_limit", "weekly_loss_limit"])
def test_utc_reset_counts_a_loss_on_the_first_bar_of_the_new_period(broker, limit):
    # Sunday to Monday tests both the daily and weekly boundary.
    index = pd.date_range("2024-01-07 23:15", periods=10, freq="15min", tz="UTC")
    frame = pd.DataFrame({"open": 2000., "high": 2001., "low": 1999., "close": 2000.,
                          "tick_volume": 1}, index=index)
    frame.loc[index[3], "low"] = 1985
    signals = [Signal(symbol="XAUUSD", strategy="test", config_hash="test", direction=Direction.LONG,
                      timestamp=index[i].to_pydatetime(), entry=2000, stop_loss=1990, take_profit=2020,
                      rationale="UTC boundary fixture", regime="test", indicators={}) for i in [2, 4]]
    cost = CostProfile(commission_per_lot=0, spread_price=0, slippage_price=0,
                       swap_long_per_lot_day=0, swap_short_per_lot_day=0)
    result = run_backtest({"M15": frame}, signals, broker.spec("XAUUSD"), cost,
                          risk_limits=BacktestRiskLimits(**{limit: .001}))
    assert len(result["trades"]) == 1
    assert result["risk_blocks"][limit] == 1


def test_fill_time_margin_rejection_and_duplicates_are_audited(broker):
    index = pd.date_range("2024-01-01", periods=8, freq="15min", tz="UTC")
    frame = pd.DataFrame({"open": 2000., "high": 2001., "low": 1999., "close": 2000.,
                          "tick_volume": 1}, index=index)
    signal = Signal(symbol="XAUUSD", strategy="test", config_hash="test", direction=Direction.LONG,
                    timestamp=index[2].to_pydatetime(), entry=2000, stop_loss=1990, take_profit=2020,
                    rationale="margin fixture", regime="test", indicators={})
    cost = CostProfile(commission_per_lot=0, spread_price=0, slippage_price=0,
                       swap_long_per_lot_day=0, swap_short_per_lot_day=0, margin_fraction=1)
    result = run_backtest({"M15": frame}, [signal, signal], broker.spec("XAUUSD"), cost,
                          capital=1000, risk_fraction=.5)
    assert result["trades"].empty
    assert result["risk_blocks"] == {"duplicate_completed_candle": 1, "insufficient_margin": 1}
    assert result["execution_audit"]["rejected"] == result["candidate_signal_count"] == 2
    assert result["entry_decisions"].risk_budget_usd.tolist() == [500, 500]


def test_daily_order_limit_restarts_on_the_next_utc_day(broker):
    index = pd.date_range("2024-01-01 23:15", periods=8, freq="15min", tz="UTC")
    frame = pd.DataFrame({"open": 2000., "high": 2001., "low": 1999., "close": 2000.,
                          "tick_volume": 1}, index=index)
    frame.loc[index[3], "low"] = 1985
    signals = [Signal(symbol="XAUUSD", strategy="test", config_hash="test", direction=Direction.LONG,
                      timestamp=index[i].to_pydatetime(), entry=2000, stop_loss=1990, take_profit=2020,
                      session="London", rationale="daily session quota fixture", regime="test", indicators={})
               for i in [2, 4]]
    cost = CostProfile(commission_per_lot=0, spread_price=0, slippage_price=0,
                       swap_long_per_lot_day=0, swap_short_per_lot_day=0)
    limits = BacktestRiskLimits(max_orders_session=1)

    result = run_backtest({"M15": frame}, signals, broker.spec("XAUUSD"), cost, risk_limits=limits)

    assert len(result["trades"]) == 2
    assert result["risk_blocks"].get("max_orders_session", 0) == 0


def test_daily_entry_quota_applies_across_named_sessions(broker):
    index = pd.date_range("2024-01-01", periods=9, freq="15min", tz="UTC")
    frame = pd.DataFrame({"open": 2000., "high": 2001., "low": 1999., "close": 2000.,
                          "tick_volume": 1}, index=index)
    signals = [Signal(symbol="XAUUSD", strategy="test", config_hash="test", direction=Direction.LONG,
                      timestamp=index[i].to_pydatetime(), entry=2000, stop_loss=1990, take_profit=2020,
                      session=session, rationale="UTC daily quota fixture", regime="test", indicators={})
               for i, session in [(2, "London"), (4, "New York")]]
    cost = CostProfile(commission_per_lot=0, spread_price=0, slippage_price=0,
                       swap_long_per_lot_day=0, swap_short_per_lot_day=0)

    result = run_backtest({"M15": frame}, signals, broker.spec("XAUUSD"), cost,
                          risk_limits=BacktestRiskLimits(max_orders_session=1, max_positions=2))

    assert len(result["trades"]) == 1
    assert result["risk_blocks"]["max_orders_session"] == 1


def test_fill_gap_cannot_push_combined_risk_over_portfolio_limit(broker):
    index = pd.date_range("2024-01-01", periods=9, freq="15min", tz="UTC")
    frame = pd.DataFrame({"open": 2000., "high": 2002., "low": 1998., "close": 2000.,
                          "tick_volume": 1}, index=index)
    frame.loc[index[3], ["open", "high", "low", "close"]] = [2001., 2003., 2001., 2002.]
    signals = [
        Signal(symbol="XAUUSD", strategy="test", config_hash="test", direction=Direction.LONG,
               timestamp=index[2].to_pydatetime(), entry=2000, stop_loss=1990, take_profit=2020,
               rationale="first position consumes risk budget", regime="test", indicators={}),
        Signal(symbol="XAUUSD", strategy="test", config_hash="test", direction=Direction.LONG,
               timestamp=index[3].to_pydatetime(), entry=2000, stop_loss=1991, take_profit=2020,
               rationale="fill gap expands the second risk", regime="test", indicators={}),
    ]
    cost = CostProfile(commission_per_lot=0, spread_price=0, slippage_price=0,
                       swap_long_per_lot_day=0, swap_short_per_lot_day=0)
    limits = BacktestRiskLimits(max_positions=2, portfolio_risk_limit=.197)

    result = run_backtest({"M15": frame}, signals, broker.spec("XAUUSD"), cost,
                          capital=1000, risk_fraction=.1, risk_limits=limits)

    assert len(result["trades"]) == 2
    assert result["trades"].Size.abs().tolist() == [10, 9]
    # Only the $10 open-quote gain is available when the second trade fills.
    # The additional gain at this candle's future close cannot affect sizing.
    assert result["entry_decisions"].iloc[1].risk_budget_usd == pytest.approx(101)
    assert result["trades"].InitialRiskUSD.sum() <= (1000 + 10) * limits.portfolio_risk_limit


def test_failed_gap_fill_does_not_consume_daily_trade_quota(broker):
    index = pd.date_range("2024-01-01", periods=8, freq="15min", tz="UTC")
    frame = pd.DataFrame({"open": 2000., "high": 2001., "low": 1999.5, "close": 2000.}, index=index)
    frame.loc[index[2], ["open", "high"]] = [2005., 2006.]
    signals = [Signal(symbol="XAUUSD", strategy="test", config_hash="test", direction=Direction.LONG,
                      timestamp=index[i].to_pydatetime(), entry=2000, stop_loss=1999, take_profit=2010,
                      rationale="gap failure followed by valid order", regime="test", indicators={}) for i in (2, 4)]
    cost = CostProfile(commission_per_lot=0, spread_price=0, slippage_price=0,
                       swap_long_per_lot_day=0, swap_short_per_lot_day=0)
    result = run_backtest({"M15": frame}, signals, broker.spec("XAUUSD"), cost, capital=1000,
                          risk_limits=BacktestRiskLimits(max_orders_session=1))
    assert result["trades"].EntryTime.tolist() == [index[4]]
    assert result["risk_blocks"] == {"minimum_lot_risk_at_fill": 1}


def test_setup_study_executes_over_budget_setups_without_reporting_account_returns(broker):
    index = pd.date_range("2024-01-01", periods=8, freq="15min", tz="UTC")
    frame = pd.DataFrame({"open": 2000., "high": 2001., "low": 1999., "close": 2000.}, index=index)
    frame.loc[index[5], "low"] = 1985
    signals = [Signal(symbol="XAUUSD", strategy="test", config_hash="test", direction=Direction.LONG,
                      timestamp=index[i].to_pydatetime(), entry=2000, stop_loss=1990, take_profit=2020,
                      rationale="independent setup study fixture", regime="test", indicators={}) for i in (2, 3, 4)]
    cost = CostProfile(commission_per_lot=0, spread_price=0, slippage_price=0,
                       swap_long_per_lot_day=0, swap_short_per_lot_day=0)
    result = run_backtest({"M15": frame}, signals, broker.spec("XAUUSD"), cost, capital=1000,
                          risk_limits=BacktestRiskLimits(max_orders_session=1), mode="setup_study")
    assert len(result["trades"]) == 3
    assert result["trades"].VolumeLots.tolist() == [.01, .01, .01]
    assert result["execution_audit"]["filled_over_reference_risk_budget"] == 3
    assert result["metrics"]["initial_capital"] is None and result["metrics"]["total_net_return"] is None
    assert result["metrics"]["net_pnl"] == pytest.approx(-30)
    assert result["equity"].CumulativePnL.iloc[-1] == pytest.approx(-30)
    assert not result["account_limits_applied"]


def test_end_liquidation_uses_last_close_and_never_fills_a_last_candle_signal(broker):
    index = pd.date_range("2024-01-01", periods=8, freq="15min", tz="UTC")
    frame = pd.DataFrame({"open": 2000., "high": 2001., "low": 1999., "close": 2000.}, index=index)
    frame.loc[index[-1], ["high", "close"]] = [2006., 2005.]
    signals = [Signal(symbol="XAUUSD", strategy="test", config_hash="test", direction=Direction.LONG,
                      timestamp=stamp.to_pydatetime(), entry=2000, stop_loss=1990, take_profit=2020,
                      rationale="end boundary fixture", regime="test", indicators={})
               for stamp in (index[2], index[-1] + pd.Timedelta(minutes=15))]
    cost = CostProfile(commission_per_lot=0, spread_price=0, slippage_price=0,
                       swap_long_per_lot_day=0, swap_short_per_lot_day=0)
    result = run_backtest({"M15": frame}, signals, broker.spec("XAUUSD"), cost,
                          risk_limits=BacktestRiskLimits(max_positions=2))
    assert len(result["trades"]) == 1
    assert result["trades"].ExitPrice.iloc[0] == 2005
    assert result["trades"].ExitTime.iloc[0] == index[-1] + pd.Timedelta(minutes=15)
    assert result["risk_blocks"] == {"unfilled_at_end": 1}
    assert result["metrics"]["net_pnl"] == result["metrics"]["final_equity"] - 10000


def test_backtest_rejects_an_empty_or_reversed_evaluation_window(broker):
    index = pd.date_range("2024-01-01", periods=8, freq="15min", tz="UTC")
    frame = pd.DataFrame({"open": 2000., "high": 2001., "low": 1999., "close": 2000.,
                          "tick_volume": 1}, index=index)
    cost = CostProfile(commission_per_lot=0, spread_price=0, slippage_price=0,
                       swap_long_per_lot_day=0, swap_short_per_lot_day=0)
    signal = []

    with pytest.raises(ValueError, match="after start"):
        run_backtest({"M15": frame}, signal, broker.spec("XAUUSD"), cost,
                     start=index[4], end=index[4])
    with pytest.raises(ValueError, match="timezone aware"):
        run_backtest({"M15": frame}, signal, broker.spec("XAUUSD"), cost,
                     start=pd.Timestamp("2024-01-01"))
