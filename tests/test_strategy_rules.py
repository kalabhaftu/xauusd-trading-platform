import pandas as pd
import pytest

from app.domain import Direction
from app.strategies.registry import build


def test_ict_sweep_mss_fvg_retest_uses_confirmed_structure():
    index = pd.date_range("2024-01-02", periods=11, freq="15min", tz="UTC")
    values = [
        (105, 110, 100, 105),
        (110, 120, 104, 110),  # Buy-side liquidity target swing.
        (105, 110, 102, 105),
        (100, 103, 98, 100),
        (103, 106, 99, 103),  # Later opposing swing for the market structure shift.
        (101, 104, 97, 101),  # Sell-side liquidity swing.
        (102, 103, 100, 102),  # Confirms the low and the opposing high.
        (98, 102, 96, 99),    # Sweep and reclaim of the confirmed low.
        (108, 110, 107, 109),  # MSS above 106 and bullish 3-candle FVG [103, 107].
        (108, 109, 105, 106),  # First completed-candle return into the FVG.
        (106, 108, 105, 107),
    ]
    frame = pd.DataFrame(values, columns=["open", "high", "low", "close"], index=index)
    strategy = build("ict_sweep_mss_fvg", {"entry_timeframe": "M15", "swing_bars": 1,
                                            "max_level_age_bars": 20, "setup_expiry_bars": 8,
                                            "minimum_reward_risk": 1.0})

    signals = strategy.signals({"M15": frame}, "XAUUSDm")

    assert len(signals) == 1
    signal = signals[0]
    assert signal.direction == Direction.LONG
    assert signal.timestamp == index[9] + pd.Timedelta(minutes=15)
    assert signal.stop_loss == 96
    assert signal.take_profit == 120
    assert signal.indicators["mss_level"] == 106
    assert signal.indicators["fvg_low"] == 103
    assert signal.indicators["fvg_high"] == 107


def test_ict_sweep_needs_mss_and_fvg_before_return():
    index = pd.date_range("2024-01-02", periods=11, freq="15min", tz="UTC")
    values = [
        (105, 110, 100, 105), (110, 120, 104, 110), (105, 110, 102, 105),
        (100, 103, 98, 100), (103, 106, 99, 103), (101, 104, 97, 101),
        (102, 103, 100, 102), (98, 102, 96, 99),
        (105, 106, 102, 105),  # Crosses structure but does not create an FVG.
        (105, 106, 103, 105), (105, 106, 103, 105),
    ]
    frame = pd.DataFrame(values, columns=["open", "high", "low", "close"], index=index)
    strategy = build("ict_sweep_mss_fvg", {"entry_timeframe": "M15", "swing_bars": 1,
                                            "max_level_age_bars": 20, "setup_expiry_bars": 8})
    assert strategy.signals({"M15": frame}, "XAUUSDm") == []


def test_bearish_ict_uses_latest_opposing_swing_and_matches_mirrored_long():
    index = pd.date_range("2024-01-02", periods=11, freq="15min", tz="UTC")
    bullish = pd.DataFrame([
        (105, 110, 100, 105), (110, 120, 104, 110), (105, 110, 102, 105),
        (100, 103, 98, 100), (103, 106, 99, 103), (101, 104, 97, 101),
        (102, 103, 100, 102), (98, 102, 96, 99), (108, 110, 107, 109),
        (108, 109, 105, 106), (106, 108, 105, 107),
    ], columns=["open", "high", "low", "close"], index=index)
    bearish = pd.DataFrame({"open": 210 - bullish.open, "high": 210 - bullish.low,
                            "low": 210 - bullish.high, "close": 210 - bullish.close}, index=index)
    parameters = {"entry_timeframe": "M15", "swing_bars": 1, "max_level_age_bars": 20,
                  "setup_expiry_bars": 8, "minimum_reward_risk": 1.0}
    strategy = build("ict_sweep_mss_fvg", parameters)
    signals = strategy.signals({"M15": bearish}, "XAUUSDm")
    assert len(signals) == 1
    assert signals[0].direction == Direction.SHORT
    assert signals[0].indicators["mss_level"] == 104  # Latest low, not the older target at 90.
    assert signals[0].stop_loss == 114 and signals[0].take_profit == 90


def test_support_resistance_bounce_uses_trailing_levels_and_configurable_filter():
    index = pd.date_range("2024-01-02", periods=19, freq="15min", tz="UTC")
    close = [100 + (i % 4) * 0.1 for i in range(8)] + [120] + [101 + (i % 3) * .1 for i in range(6)] + [110, 110, 109, 108]
    frame = pd.DataFrame({"open": close, "high": [x + .2 for x in close],
                          "low": [x - .2 for x in close], "close": close}, index=index)
    frame.iloc[16, frame.columns.get_loc("low")] = 100
    strategy = build("support_resistance_bounce", {"entry_timeframe": "M15",
                                                    "level_window_minutes": 240,
                                                    "minimum_prior_bounces": 0,
                                                    "minimum_reward_risk": 0.1})

    signals = strategy.signals({"M15": frame}, "XAUUSDm")

    assert signals
    assert signals[0].direction == Direction.LONG
    assert signals[0].stop_loss < signals[0].entry < signals[0].take_profit
    assert signals[0].indicators["lookback_bars"] == 16


def test_old_prototype_strategy_names_are_not_registered():
    for name in ("trend_pullback", "session_breakout", "mean_reversion"):
        try:
            build(name)
        except ValueError as error:
            assert "Unknown strategy" in str(error)
        else:
            raise AssertionError(f"retired strategy remained registered: {name}")


def test_support_resistance_accepts_every_stored_timeframe():
    for timeframe in ("M1", "M5", "M15", "M30", "H1", "H4", "D1"):
        strategy = build("support_resistance_bounce", {"entry_timeframe": timeframe})
        assert strategy.parameters.entry_timeframe == timeframe


@pytest.mark.parametrize("direction", [Direction.LONG, Direction.SHORT])
def test_ict_confirms_gap_after_the_structure_break_without_future_leakage(direction):
    index = pd.date_range("2024-01-02", periods=12, freq="15min", tz="UTC")
    frame = pd.DataFrame([
        (105, 110, 100, 105), (110, 120, 104, 110), (105, 110, 102, 105),
        (100, 103, 98, 100), (103, 106, 99, 103), (101, 104, 97, 101),
        (102, 103, 100, 102), (98, 102, 96, 99),
        (99, 110, 98, 109),  # Breaks 106; its own low has no three-candle gap.
        (109, 111, 105, 110),  # Completes gap [102, 105] around the displacement.
        (105, 108, 103, 104),  # First close-confirmed return.
        (104, 108, 103, 107),
    ], columns=["open", "high", "low", "close"], index=index)
    if direction == Direction.SHORT:
        frame = pd.DataFrame({"open": 210 - frame.open, "high": 210 - frame.low,
                              "low": 210 - frame.high, "close": 210 - frame.close}, index=index)
    parameters = {"entry_timeframe": "M15", "swing_bars": 1, "fvg_confirmation_bars": 2}
    model = build("ict_sweep_mss_fvg", parameters)
    assert model.signals({"M15": frame.iloc[:10]}, "XAUUSD") == []
    signals = model.signals({"M15": frame}, "XAUUSD")
    assert len(signals) == 1 and signals[0].direction == direction
    assert signals[0].timestamp == index[10] + pd.Timedelta(minutes=15)
    assert signals[0].indicators["fvg_confirmation_delay"] == 1
    strict = build("ict_sweep_mss_fvg", {**parameters, "fvg_confirmation_bars": 0})
    assert strict.signals({"M15": frame}, "XAUUSD") == []


def test_sr_counts_historical_bounces_and_has_enough_h1_observations():
    index = pd.date_range("2024-01-02", periods=19, freq="1h", tz="UTC")
    close = [100 + (i % 4) * .1 for i in range(8)] + [120] + [101 + (i % 3) * .1 for i in range(6)] + [110, 110, 109, 108]
    frame = pd.DataFrame({"open": close, "high": [x + .2 for x in close],
                          "low": [x - .2 for x in close], "close": close}, index=index)
    frame.loc[index[16], "low"] = 100
    model = build("support_resistance_bounce", {"entry_timeframe": "H1", "minimum_reward_risk": .1})
    signals = model.signals({"H1": frame}, "XAUUSD")
    assert signals and signals[0].indicators["prior_bounces"] >= 1
    assert signals[0].indicators["lookback_bars"] == 16
    # A weekend/missing-data jump cannot complete a pending rolling-zone bounce.
    gap = frame.copy()
    gap.index = pd.DatetimeIndex(list(index[:16]) + list(index[16:] + pd.Timedelta(days=2)))
    assert model.signals({"H1": gap}, "XAUUSD") == []
