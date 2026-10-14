from dataclasses import dataclass
from typing import cast

import pandas as pd

from app.data.validation import TIMEFRAMES
from app.domain import Direction, Signal
from app.strategies.base import IctParameters, Strategy


@dataclass(frozen=True)
class _Swing:
    price: float
    confirmed_at: int


@dataclass
class _Setup:
    direction: Direction
    sweep_index: int
    sweep_extreme: float
    mss_level: float
    phase: str = "await_mss"
    mss_index: int = -1
    fvg_index: int = -1
    fvg_low: float = 0.0
    fvg_high: float = 0.0


class IctSweepMssFvg(Strategy):
    """Liquidity sweep -> market structure shift with FVG -> first FVG retest."""

    name = "ict_sweep_mss_fvg"
    description = (
        "ICT concept sequence: sweep a confirmed swing liquidity level and reclaim it; "
        "close through the latest opposing confirmed swing, then confirm a three-candle FVG in that displacement; "
        "then wait for a completed-candle return into that FVG. Stop is beyond the sweep wick; "
        "target is the next unswept opposing swing liquidity."
    )
    parameters_model = IctParameters
    rules_version = 5

    def _swings(self, frame: pd.DataFrame) -> tuple[list[tuple[int, _Swing]], list[tuple[int, _Swing]]]:
        right = cast(IctParameters, self.parameters).swing_bars
        high = frame.high.to_numpy(dtype=float)
        low = frame.low.to_numpy(dtype=float)
        highs: list[tuple[int, _Swing]] = []
        lows: list[tuple[int, _Swing]] = []
        for pivot_index in range(right, len(frame) - right):
            left_high = high[pivot_index - right:pivot_index]
            right_high = high[pivot_index + 1:pivot_index + right + 1]
            left_low = low[pivot_index - right:pivot_index]
            right_low = low[pivot_index + 1:pivot_index + right + 1]
            confirmed_at = pivot_index + right
            if high[pivot_index] > left_high.max() and high[pivot_index] >= right_high.max():
                highs.append((confirmed_at, _Swing(float(high[pivot_index]), confirmed_at)))
            if low[pivot_index] < left_low.min() and low[pivot_index] <= right_low.min():
                lows.append((confirmed_at, _Swing(float(low[pivot_index]), confirmed_at)))
        return highs, lows

    def signals(self, frames: dict[str, pd.DataFrame], symbol: str) -> list[Signal]:
        timeframe = self.parameters.entry_timeframe
        p = cast(IctParameters, self.parameters)
        frame = frames.get(timeframe)
        if frame is None or len(frame) < 2 * p.swing_bars + 5:
            return []

        high = frame.high.to_numpy(dtype=float)
        low = frame.low.to_numpy(dtype=float)
        close = frame.close.to_numpy(dtype=float)
        timestamps = cast(pd.DatetimeIndex, frame.index).as_unit("ns").to_numpy(dtype="int64")
        candle_ns = pd.Timedelta(minutes=TIMEFRAMES[timeframe]).value
        high_events, low_events = self._swings(frame)
        active_highs: list[_Swing] = []
        active_lows: list[_Swing] = []
        high_event_index = low_event_index = 0
        setup: _Setup | None = None
        output: list[Signal] = []
        for i in range(len(frame)):
            if i and timestamps[i] - timestamps[i - 1] > candle_ns:
                setup = None  # An unobserved price path cannot complete a pending intraday sequence.
            # A pivot becomes usable only after its right-side confirmation candle
            # has itself closed. It is therefore first available on the next bar.
            while high_event_index < len(high_events) and high_events[high_event_index][0] < i:
                active_highs.append(high_events[high_event_index][1])
                high_event_index += 1
            while low_event_index < len(low_events) and low_events[low_event_index][0] < i:
                active_lows.append(low_events[low_event_index][1])
                low_event_index += 1

            active_highs = [pivot for pivot in active_highs
                            if i - pivot.confirmed_at <= p.max_level_age_bars]
            active_lows = [pivot for pivot in active_lows
                           if i - pivot.confirmed_at <= p.max_level_age_bars]
            # Keep the levels that were active immediately before this candle.
            # The current candle can sweep a swing and remove it from the active
            # set, but that same close may be the market structure shift through it.
            mss_highs = active_highs.copy()
            mss_lows = active_lows.copy()

            # Record liquidity taken on this candle, but only treat a wick-through
            # followed by a close back across the level as a sweep-and-reclaim.
            low_sweeps = [pivot for pivot in active_lows
                          if low[i] < pivot.price < close[i]]
            high_sweeps = [pivot for pivot in active_highs
                           if high[i] > pivot.price > close[i]]
            both_sides = bool(low_sweeps and high_sweeps)

            active_highs = [pivot for pivot in active_highs if high[i] < pivot.price]
            active_lows = [pivot for pivot in active_lows if low[i] > pivot.price]

            if setup is not None:
                if i - setup.sweep_index > p.setup_expiry_bars:
                    setup = None
                elif setup.direction == Direction.LONG and low[i] <= setup.sweep_extreme:
                    setup = None
                elif setup.direction == Direction.SHORT and high[i] >= setup.sweep_extreme:
                    setup = None

            if setup is not None and not both_sides:
                if setup.phase == "await_mss" and i > setup.sweep_index:
                    previous_close = close[i - 1]
                    mss_level = None
                    if setup.direction == Direction.LONG:
                        opposing = [pivot for pivot in mss_highs if previous_close <= pivot.price]
                        if opposing:
                            mss_level = max(opposing, key=lambda pivot: pivot.confirmed_at).price
                    else:
                        opposing = [pivot for pivot in mss_lows if previous_close >= pivot.price]
                        if opposing:
                            mss_level = max(opposing, key=lambda pivot: pivot.confirmed_at).price
                    if mss_level is not None:
                        setup.mss_level = mss_level
                    if (setup.direction == Direction.LONG
                            and mss_level is not None and previous_close <= mss_level < close[i]):
                        setup.phase = "await_fvg"
                        setup.mss_index = i
                    elif (setup.direction == Direction.SHORT
                          and mss_level is not None and previous_close >= mss_level > close[i]):
                        setup.phase = "await_fvg"
                        setup.mss_index = i
                if setup.phase == "await_fvg":
                    if i - setup.mss_index > p.fvg_confirmation_bars:
                        setup = None
                    elif i >= 2 and setup.direction == Direction.LONG and low[i] > high[i - 2]:
                        setup.phase = "await_fvg_return"
                        setup.fvg_index = i
                        setup.fvg_low, setup.fvg_high = float(high[i - 2]), float(low[i])
                    elif i >= 2 and setup.direction == Direction.SHORT and high[i] < low[i - 2]:
                        setup.phase = "await_fvg_return"
                        setup.fvg_index = i
                        setup.fvg_low, setup.fvg_high = float(high[i]), float(low[i - 2])
                if setup is not None and setup.phase == "await_fvg_return" and i > setup.fvg_index:
                    close_inside_fvg = setup.fvg_low <= close[i] <= setup.fvg_high
                    revisited_fvg = (low[i] <= setup.fvg_high if setup.direction == Direction.LONG
                                     else high[i] >= setup.fvg_low)
                    if close_inside_fvg and revisited_fvg:
                        if setup.direction == Direction.LONG:
                            targets = [pivot.price for pivot in active_highs if pivot.price > close[i]]
                            target = min(targets) if targets else None
                            stop = setup.sweep_extreme
                            direction = Direction.LONG
                        else:
                            targets = [pivot.price for pivot in active_lows if pivot.price < close[i]]
                            target = max(targets) if targets else None
                            stop = setup.sweep_extreme
                            direction = Direction.SHORT
                        if target is not None:
                            sign = 1 if direction == Direction.LONG else -1
                            risk = sign * (close[i] - stop)
                            reward = sign * (target - close[i])
                            rr = reward / risk if risk > 0 else 0.0
                            signal = self.make_structural_signal(
                                frame, i, symbol, direction, stop, target, p.minimum_reward_risk,
                                "Confirmed swing liquidity sweep and reclaim; opposing structure close with a "
                                "three-candle fair value gap; first completed-candle FVG retest. Stop beyond "
                                "the sweep extreme; target at the nearest unswept opposing swing.",
                                "ICT liquidity reversal",
                                {"swept_level": float(setup.sweep_extreme),
                                 "mss_level": float(setup.mss_level),
                                 "fvg_low": float(setup.fvg_low), "fvg_high": float(setup.fvg_high),
                                 "fvg_confirmation_delay": float(setup.fvg_index - setup.mss_index),
                                 "target_liquidity": float(target), "reward_risk": float(rr),
                                 "swing_bars": float(p.swing_bars)})
                            if signal is not None:
                                output.append(signal)
                        # The setup is single-use. A failed first retest is not
                        # silently recycled into a later, unrelated entry.
                        setup = None

            if setup is None and not both_sides and i > 0:
                bullish_sweeps = [pivot for pivot in low_sweeps
                                  if high[i] < min((candidate.price for candidate in active_highs), default=float("inf"))]
                bearish_sweeps = [pivot for pivot in high_sweeps
                                  if low[i] > max((candidate.price for candidate in active_lows), default=0.0)]
                if bullish_sweeps:
                    opposing = [pivot for pivot in active_highs if high[i] < pivot.price]
                    if opposing:
                        mss = max(opposing, key=lambda pivot: pivot.confirmed_at)
                        setup = _Setup(Direction.LONG, i, float(low[i]), mss.price)
                elif bearish_sweeps:
                    opposing = [pivot for pivot in active_lows if low[i] > pivot.price]
                    if opposing:
                        mss = max(opposing, key=lambda pivot: pivot.confirmed_at)
                        setup = _Setup(Direction.SHORT, i, float(high[i]), mss.price)

        return output
