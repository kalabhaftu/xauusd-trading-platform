import pandas as pd
import pytest

from app.domain import Direction
from app.strategies.registry import build


@pytest.mark.parametrize("direction", [Direction.LONG, Direction.SHORT])
def test_trend_continuation_rule(monkeypatch, direction):
    import app.strategies.trend_pullback as module
    index = pd.date_range("2024-01-02 04:00", periods=12, freq="15min", tz="UTC")
    frame = pd.DataFrame({"open": 2000., "high": 2001., "low": 1999., "close": 2000.}, index=index)
    if direction == Direction.LONG:
        frame.iloc[-1] = [2000, 2003, 2000, 2002]
    else:
        frame.iloc[-1] = [2000, 2000, 1997, 1998]
    higher = frame.resample("1h").agg({"open": "first", "high": "max", "low": "min", "close": "last"})
    sign = 1 if direction == Direction.LONG else -1
    monkeypatch.setattr(module, "ema", lambda series, period: pd.Series(
        2000 + sign if period == 50 else 2000 - sign if period == 200 else 2000, index=series.index))
    monkeypatch.setattr(module, "atr", lambda frame, period: pd.Series(2., index=frame.index))
    signals = build("trend_pullback").signals({"M15": frame, "H1": higher}, "XAUUSD")
    assert signals[-1].direction == direction
    assert signals[-1].timestamp == index[-1] + pd.Timedelta(minutes=15)


def test_mean_reversion_requires_weak_trend_and_prior_extreme(monkeypatch):
    import app.strategies.mean_reversion as module
    index = pd.date_range("2024-01-02", periods=30, freq="15min", tz="UTC")
    frame = pd.DataFrame({"open": 100., "high": 101., "low": 89., "close": 100.}, index=index)
    frame.loc[index[-2], "close"] = 89
    frame.loc[index[-1], "close"] = 91
    monkeypatch.setattr(module, "bollinger", lambda series, *args: pd.DataFrame(
        {"lower": 90., "middle": 100., "upper": 110.}, index=series.index))
    momentum = pd.Series(50., index=index)
    momentum.iloc[-2] = 20
    monkeypatch.setattr(module, "rsi", lambda *args: momentum)
    monkeypatch.setattr(module, "atr", lambda *args: pd.Series(2., index=index))
    monkeypatch.setattr(module, "adx", lambda *args: pd.Series(10., index=index))
    assert build("mean_reversion").signals({"M15": frame}, "XAUUSD")[-1].direction == Direction.LONG
    monkeypatch.setattr(module, "adx", lambda *args: pd.Series(30., index=index))
    assert not build("mean_reversion").signals({"M15": frame}, "XAUUSD")


def test_session_breakout_confirmed_close(monkeypatch):
    import app.strategies.session_breakout as module
    index = pd.date_range("2024-01-02", periods=36, freq="15min", tz="UTC")
    frame = pd.DataFrame({"open": 2000., "high": 2001., "low": 1999., "close": 2000.}, index=index)
    frame.loc[pd.Timestamp("2024-01-02 08:00", tz="UTC"), ["high", "close"]] = [2003, 2002]
    # Seed sufficient prior history for a causal 48-observation volatility reference.
    preceding = frame.iloc[:32].copy()
    preceding.index -= pd.Timedelta(days=1)
    frame = pd.concat([preceding, frame])
    monkeypatch.setattr(module, "atr", lambda f, period: pd.Series(2., index=f.index))
    signals = build("session_breakout").signals({"M15": frame}, "XAUUSD")
    london = [s for s in signals if s.session.startswith("London")]
    assert len(london) == 1 and london[0].direction == Direction.LONG
    assert london[0].exit_at == london[0].timestamp + pd.Timedelta(hours=4)
