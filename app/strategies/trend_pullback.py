import pandas as pd

from app.data.validation import TIMEFRAMES, align_completed
from app.domain import Direction
from app.indicators.core import atr, ema
from app.strategies.base import Strategy


class TrendPullback(Strategy):
    name = "trend_pullback"
    description = "Completed H1 EMA50/200 trend; M15 EMA20 pullback and continuation close; ATR stop / 2R target."

    def signals(self, frames, symbol):
        p = self.parameters
        frame, higher = frames[p.entry_timeframe], frames[p.trend_timeframe]
        if frame.empty or higher.empty:
            return []
        p = self.parameters
        trend = pd.DataFrame({"fast": ema(higher.close, p.ema_fast),
                              "slow": ema(higher.close, p.ema_slow)}, index=higher.index)
        trend = align_completed(frame, trend, TIMEFRAMES[p.trend_timeframe], TIMEFRAMES[p.entry_timeframe])
        average, volatility = ema(frame.close, p.pullback_ema), atr(frame, p.atr_period)
        result = []
        for i in range(1, len(frame)):
            a, v = average.iloc[i], volatility.iloc[i]
            fast, slow = trend.fast.iloc[i], trend.slow.iloc[i]
            long = fast > slow and abs(frame.low.iloc[i] - a) <= p.pullback_atr * v \
                and frame.close.iloc[i] > frame.high.iloc[i - 1]
            short = fast < slow and abs(frame.high.iloc[i] - a) <= p.pullback_atr * v \
                and frame.close.iloc[i] < frame.low.iloc[i - 1]
            if long or short:
                signal = self.make_signal(frame, i, symbol, Direction.LONG if long else Direction.SHORT, v,
                                          "HTF trend, EMA pullback and continuation close", "trend",
                                          {"atr": v, "ema_entry": a, "ema_fast_h1": fast, "ema_slow_h1": slow})
                if signal:
                    result.append(signal)
        return result
