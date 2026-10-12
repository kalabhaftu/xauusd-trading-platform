from datetime import UTC, datetime

import pandas as pd
import pytest

from app.data.validation import align_completed, closed, validate
from app.indicators.core import atr, bollinger, ema, rsi
from app.strategies.registry import REGISTRY, build


def test_indicator_known_values():
    values = pd.Series(range(1, 31), dtype=float)
    assert rsi(values).iloc[-1] == 100
    assert rsi(pd.Series([1.] * 30)).iloc[-1] == 50
    assert ema(pd.Series([5.] * 30), 20).iloc[-1] == 5
    bands = bollinger(pd.Series([5.] * 30))
    assert bands.upper.iloc[-1] == bands.lower.iloc[-1] == 5
    frame = pd.DataFrame({"close": [10.] * 30, "high": [11.] * 30, "low": [9.] * 30})
    assert atr(frame).iloc[-1] == 2


def test_htf_availability_at_completion():
    lower = pd.DataFrame(index=pd.date_range("2024-01-01", periods=8, freq="15min", tz="UTC"))
    higher = pd.DataFrame({"trend": [1, 999]}, index=pd.date_range("2024-01-01", periods=2, freq="1h", tz="UTC"))
    aligned = align_completed(lower, higher)
    assert pd.isna(aligned.trend.iloc[2])
    assert aligned.trend.iloc[3] == 1
    assert aligned.trend.iloc[6] == 1
    assert aligned.trend.iloc[7] == 999


def test_closed_candles(synthetic_frames):
    frame = synthetic_frames["M15"].iloc[:4]
    now = datetime(2023, 1, 1, 0, 40, tzinfo=UTC)
    assert len(closed(frame, "M15", now)) == 2


def test_data_rejects_invalid_ohlc_and_duplicates(synthetic_frames):
    frame = synthetic_frames["M15"].iloc[:10].copy()
    frame.iloc[2, frame.columns.get_loc("high")] = 1
    with pytest.raises(ValueError):
        validate(frame, "M15")
    duplicate = pd.concat([synthetic_frames["M15"].iloc[:10], synthetic_frames["M15"].iloc[:1]])
    with pytest.raises(ValueError):
        validate(duplicate, "M15")


def test_incremental_storage_and_gap_detection(store, synthetic_frames):
    frame = synthetic_frames["M15"].iloc[:100]
    store.save_candles(frame, "SYNTHETIC_TEST_ONLY", "XAUUSD", "M15")
    store.save_candles(frame, "SYNTHETIC_TEST_ONLY", "XAUUSD", "M15")
    assert len(store.load_candles("SYNTHETIC_TEST_ONLY", "XAUUSD", "M15")) == 100
    assert validate(frame.drop(frame.index[4]), "M15")["gaps"][0]["classification"] == "unresolved"


@pytest.mark.parametrize("name", list(REGISTRY))
def test_future_candles_do_not_change_prior_signals(name, synthetic_frames):
    strategy = build(name)
    cutoff = synthetic_frames["M15"].index[3000]
    prefix = {tf: f.loc[f.index + pd.Timedelta(minutes=15 if tf == "M15" else 60) <= cutoff]
              for tf, f in synthetic_frames.items()}
    earlier = strategy.signals(prefix, "XAUUSD")
    complete = [s for s in strategy.signals(synthetic_frames, "XAUUSD") if s.timestamp <= cutoff]
    assert earlier == complete


def test_configuration_change_invalidates_hash():
    assert build("ict_sweep_mss_fvg").config_hash != build(
        "ict_sweep_mss_fvg", {"swing_bars": 3}).config_hash
    with pytest.raises(ValueError):
        build("ict_sweep_mss_fvg", {"ema_fast": 50})


def test_london_newyork_dst():
    # Their DST changes occur on different dates; each uses its own IANA timezone.
    london = pd.Timestamp("2024-03-15 08:00", tz="Europe/London").tz_convert("UTC")
    ny = pd.Timestamp("2024-03-15 08:00", tz="America/New_York").tz_convert("UTC")
    assert london.hour == 8 and ny.hour == 12
    assert pd.Timestamp("2024-04-15 08:00", tz="Europe/London").tz_convert("UTC").hour == 7
