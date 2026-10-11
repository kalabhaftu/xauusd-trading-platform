from datetime import date, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from app.data.validation import TIMEFRAMES
from app.domain import Direction
from app.indicators.core import atr
from app.strategies.base import Strategy


class SessionBreakout(Strategy):
    name = "session_breakout"
    description = "London/New York local 08:00 breakout of prior hour; volatility filter, optional retest, 4h exit."

    def signals(self, frames, symbol):
        frame = frames[self.parameters.entry_timeframe]
        if frame.empty:
            return []
        p = self.parameters
        minutes = TIMEFRAMES[p.entry_timeframe]
        volatility = atr(frame, p.atr_period)
        median = volatility.rolling(96, min_periods=48).median().shift(1)
        result = []
        used = set()
        candidates: dict[tuple[str, date], tuple[int, Direction]] = {}
        for i in range(1, len(frame)):
            timestamp = frame.index[i] + pd.Timedelta(minutes=minutes)
            v = volatility.iloc[i]
            ratio = v / median.iloc[i]
            if not p.volatility_min_ratio <= ratio <= p.volatility_max_ratio:
                continue
            for name, tz, hour in (("London", "Europe/London", p.london_open_hour),
                                   ("New York", "America/New_York", p.new_york_open_hour)):
                local = timestamp.tz_convert(ZoneInfo(tz))
                opening = local.replace(hour=hour, minute=0, second=0, microsecond=0)
                opening = opening.tz_convert("UTC")
                key = (name, local.date())
                if key in used or not opening < timestamp <= opening + pd.Timedelta(minutes=p.entry_window_minutes):
                    continue
                window = frame.loc[(frame.index >= opening - pd.Timedelta(minutes=p.pre_session_minutes))
                                   & (frame.index + pd.Timedelta(minutes=minutes) <= opening)]
                if len(window) != p.pre_session_minutes // minutes:
                    continue
                upper, lower = window.high.max(), window.low.min()
                price = frame.close.iloc[i]
                direction = None
                if not p.retest:
                    if price > upper + p.breakout_buffer_atr * v and frame.close.iloc[i - 1] <= upper:
                        direction = Direction.LONG
                    elif price < lower - p.breakout_buffer_atr * v and frame.close.iloc[i - 1] >= lower:
                        direction = Direction.SHORT
                else:
                    prior = candidates.get(key)
                    if prior and 0 < i - prior[0] <= p.retest_max_bars:
                        if prior[1] == Direction.LONG and frame.low.iloc[i] <= upper and price > upper:
                            direction = Direction.LONG
                        elif prior[1] == Direction.SHORT and frame.high.iloc[i] >= lower and price < lower:
                            direction = Direction.SHORT
                    if key not in candidates:
                        if price > upper + p.breakout_buffer_atr * v:
                            candidates[key] = (i, Direction.LONG)
                        elif price < lower - p.breakout_buffer_atr * v:
                            candidates[key] = (i, Direction.SHORT)
                if direction:
                    signal = self.make_signal(frame, i, symbol, direction, v, "Confirmed pre-session range breakout",
                                              "breakout", {"atr": v, "range_high": upper, "range_low": lower},
                                              session=f"{name}:{local.date()}",
                                              exit_at=timestamp.to_pydatetime() + timedelta(hours=p.breakout_holding_hours))
                    if signal:
                        result.append(signal)
                        used.add(key)
        return result
