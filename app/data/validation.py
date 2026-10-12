from datetime import datetime

import numpy as np
import pandas as pd

TIMEFRAMES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440}


def validate(frame: pd.DataFrame, timeframe: str) -> dict:
    if timeframe not in TIMEFRAMES:
        raise ValueError("Unsupported timeframe")
    if frame.empty:
        return {"valid": False, "reason": "No broker candles", "rows": 0, "gaps": []}
    if not isinstance(frame.index, pd.DatetimeIndex) or frame.index.tz is None:
        raise ValueError("Candle index must be timezone aware")
    values = frame[["open", "high", "low", "close"]]
    if not np.isfinite(values.to_numpy()).all() or (values <= 0).any().any():
        raise ValueError("Non-finite or nonpositive prices")
    if ((frame.high < values.max(axis=1)) | (frame.low > values.min(axis=1))).any():
        raise ValueError("Invalid OHLC envelope")
    if frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
        raise ValueError("Duplicate or unordered candle timestamps")
    expected = pd.Timedelta(minutes=TIMEFRAMES[timeframe])
    gaps = [{"after": frame.index[i - 1].isoformat(), "before": frame.index[i].isoformat(),
             "classification": "unresolved"} for i in range(1, len(frame))
            if frame.index[i] - frame.index[i - 1] > expected]
    return {"valid": True, "rows": len(frame), "start": frame.index[0].isoformat(),
            "end": frame.index[-1].isoformat(), "gaps": gaps, "complete": not gaps}


def closed(frame: pd.DataFrame, timeframe: str, asof: datetime) -> pd.DataFrame:
    return frame.loc[frame.index + pd.Timedelta(minutes=TIMEFRAMES[timeframe]) <= pd.Timestamp(asof)].copy()


def align_completed(lower: pd.DataFrame, higher: pd.DataFrame, higher_minutes: int = 60,
                    lower_minutes: int = 15) -> pd.DataFrame:
    left = pd.DataFrame({"available": lower.index + pd.Timedelta(minutes=lower_minutes)}, index=lower.index)
    right = higher.copy()
    right["available"] = right.index + pd.Timedelta(minutes=higher_minutes)
    merged = pd.merge_asof(left.reset_index(drop=True), right.reset_index(drop=True), on="available",
                           direction="backward", tolerance=pd.Timedelta(minutes=higher_minutes))
    merged.index = lower.index
    return merged.drop(columns="available")
