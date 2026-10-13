import pandas as pd

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
