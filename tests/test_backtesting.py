from datetime import timedelta

import pandas as pd
import pytest

from app.backtesting.engine import CostProfile, run_backtest
from app.backtesting.research import partitions, walk_forward_windows
from app.domain import Direction, Signal


def fixture_run(broker, direction=Direction.LONG, ambiguous=True, gap=False, cost=None):
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
    return run_backtest({"M15": frame, "H1": frame}, [signal], broker.spec("XAUUSD"), cost)


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
