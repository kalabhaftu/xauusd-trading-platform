import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def wilder(series: pd.Series, period: int) -> pd.Series:
    # Wilder smoothing seeded with the first arithmetic mean, not the first sample.
    values = series.to_numpy(dtype=float)
    output = np.full(len(values), np.nan)
    valid = np.flatnonzero(np.isfinite(values))
    if len(valid) < period:
        return pd.Series(output, index=series.index)
    start = valid[0]
    seed = start + period - 1
    if np.isfinite(values[start:seed + 1]).all():
        output[seed] = values[start:seed + 1].mean()
    for i in range(seed + 1, len(values)):
        if np.isfinite(values[i]) and np.isfinite(output[i - 1]):
            output[i] = (output[i - 1] * (period - 1) + values[i]) / period
    return pd.Series(output, index=series.index)


def atr(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    previous = frame.close.shift(1)
    tr = pd.concat([frame.high - frame.low, (frame.high - previous).abs(),
                    (frame.low - previous).abs()], axis=1).max(axis=1)
    return wilder(tr, period)


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    diff = series.diff()
    gain = wilder(diff.clip(lower=0), period)
    loss = wilder(-diff.clip(upper=0), period)
    result = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
    result = result.mask((loss == 0) & (gain > 0), 100)
    return result.mask((loss == 0) & (gain == 0), 50)


def bollinger(series: pd.Series, period: int = 20, std: float = 2) -> pd.DataFrame:
    middle = series.rolling(period).mean()
    deviation = series.rolling(period).std(ddof=0) * std
    return pd.DataFrame({"middle": middle, "upper": middle + deviation, "lower": middle - deviation})


def adx(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    up = frame.high.diff()
    down = -frame.low.diff()
    plus = up.where((up > down) & (up > 0), 0.0)
    minus = down.where((down > up) & (down > 0), 0.0)
    denom = atr(frame, period).replace(0, np.nan)
    pdi = 100 * wilder(plus, period) / denom
    mdi = 100 * wilder(minus, period) / denom
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return wilder(dx, period)
