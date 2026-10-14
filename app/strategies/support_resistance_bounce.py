import math
from dataclasses import dataclass
from typing import cast

import numpy as np
import pandas as pd

from app.data.validation import TIMEFRAMES
from app.domain import Direction, Signal
from app.strategies.base import Strategy, SupportResistanceParameters


@dataclass
class _LevelState:
    center: float
    gamma: float
    bounces: int = 0
    pending: bool = False


class SupportResistanceBounce(Strategy):
    """Causal rolling-extrema support/resistance bounce rule."""

    name = "support_resistance_bounce"
    description = (
        "Find rolling close-price minima/maxima over a configurable window with at least 16 candles by default. "
        "Build zones using the trailing mean absolute close change; freeze a zone while price is inside it; "
        "count a bounce only when price exits the entry side without crossing the far zone edge. "
        "By default, trade only after one earlier bounce at a nearby level. Stop is beyond the far zone edge; "
        "target is the opposing rolling zone."
    )
    parameters_model = SupportResistanceParameters
    rules_version = 3

    def signals(self, frames: dict[str, pd.DataFrame], symbol: str) -> list[Signal]:
        timeframe = self.parameters.entry_timeframe
        frame = frames.get(timeframe)
        if frame is None or len(frame) < 5:
            return []

        p = cast(SupportResistanceParameters, self.parameters)
        minutes = TIMEFRAMES[timeframe]
        window_bars = max(p.minimum_window_bars, math.ceil(p.level_window_minutes / minutes))
        close = frame.close.to_numpy(dtype=float)
        high = frame.high.to_numpy(dtype=float)
        low = frame.low.to_numpy(dtype=float)
        timestamps = cast(pd.DatetimeIndex, frame.index).as_unit("ns").to_numpy(dtype="int64")
        lookback_ns = pd.Timedelta(minutes=window_bars * minutes).value
        support: _LevelState | None = None
        resistance: _LevelState | None = None
        output: list[Signal] = []

        def refresh(state: _LevelState | None, center: float, gamma: float,
                    prior: np.ndarray, direction: Direction) -> _LevelState:
            if state is not None and state.pending:
                return state
            # Count completed historical bounces on this exact rolling zone.
            # A new level can already have prior touches in the trailing sample;
            # transferring counters between drifting nearby levels loses that information.
            boundary = center + gamma if direction == Direction.LONG else center - gamma
            inside = prior <= boundary if direction == Direction.LONG else prior >= boundary
            entries = np.flatnonzero(~inside[:-1] & inside[1:])
            exits = np.flatnonzero(inside[:-1] & ~inside[1:])
            bounces = int(np.count_nonzero(exits > entries[0])) if len(entries) else 0
            pending = bool(len(entries) and (not len(exits) or entries[-1] > exits[-1]))
            return _LevelState(center, gamma, bounces=bounces, pending=pending)

        def maybe_signal(index: int, direction: Direction, zone_low: float, zone_high: float,
                         opposite_center: float, opposite_gamma: float,
                         prior_bounces: int, gamma: float, support_center: float,
                         resistance_center: float) -> Signal | None:
            if prior_bounces < p.minimum_prior_bounces:
                return None
            if direction == Direction.LONG:
                stop, target = zone_low, opposite_center - opposite_gamma
            else:
                stop, target = zone_high, opposite_center + opposite_gamma
            return self.make_structural_signal(
                frame, index, symbol, direction, stop, target, p.minimum_reward_risk,
                "Price entered a causal rolling support/resistance zone and closed back out the same side "
                "without trading through the far boundary. The prior-bounce filter was satisfied.",
                "support/resistance bounce",
                {"support_center": float(support_center),
                 "resistance_center": float(resistance_center),
                 "zone_low": float(zone_low), "zone_high": float(zone_high),
                 "gamma": float(gamma), "prior_bounces": float(prior_bounces),
                 "lookback_bars": float(window_bars), "lookback_minutes": float(window_bars * minutes)})

        for i in range(window_bars, len(frame)):
            # The rolling period contains complete, contiguous candles. A market
            # closure or missing history resets pending zones and the warm-up.
            if timestamps[i] - timestamps[i - window_bars] > lookback_ns:
                support = resistance = None
                continue
            # Levels and their width use only observations strictly before this
            # candle. The paper estimates width on a complete sample; this trailing
            # estimate is a deliberate no-lookahead adaptation for real-time use.
            prior = close[i - window_bars:i]
            increments = np.abs(np.diff(prior))
            gamma = float(increments.mean()) if len(increments) else 0.0
            if not math.isfinite(gamma) or gamma <= 0:
                continue
            support_center = float(prior.min())
            resistance_center = float(prior.max())
            if not (math.isfinite(support_center) and math.isfinite(resistance_center)):
                continue
            support = refresh(support, support_center, gamma, prior, Direction.LONG)
            resistance = refresh(resistance, resistance_center, gamma, prior, Direction.SHORT)

            support_low = support.center - support.gamma
            support_high = support.center + support.gamma
            resistance_low = resistance.center - resistance.gamma
            resistance_high = resistance.center + resistance.gamma
            previous_close = close[i - 1]
            support_signal = None
            resistance_signal = None

            if support.pending:
                if low[i] < support_low:
                    support = None  # Penetration or ambiguous same-candle path.
                elif close[i] > support_high:
                    prior_bounces = support.bounces
                    support.bounces += 1
                    support.pending = False
                    support_signal = maybe_signal(i, Direction.LONG, support_low, support_high,
                                                  resistance.center, resistance.gamma,
                                                  prior_bounces, support.gamma,
                                                  support.center, resistance.center)
            elif previous_close > support_high and low[i] <= support_high:
                if low[i] < support_low:
                    support = None
                elif close[i] > support_high:
                    prior_bounces = support.bounces
                    support.bounces += 1
                    support_signal = maybe_signal(i, Direction.LONG, support_low, support_high,
                                                  resistance.center, resistance.gamma,
                                                  prior_bounces, support.gamma,
                                                  support.center, resistance.center)
                else:
                    support.pending = True

            target_support_center = support.center if support is not None else support_center
            target_support_gamma = support.gamma if support is not None else gamma
            if resistance is not None:
                resistance_low = resistance.center - resistance.gamma
                resistance_high = resistance.center + resistance.gamma
                if resistance.pending:
                    if high[i] > resistance_high:
                        resistance = None
                    elif close[i] < resistance_low:
                        prior_bounces = resistance.bounces
                        resistance.bounces += 1
                        resistance.pending = False
                        resistance_signal = maybe_signal(i, Direction.SHORT, resistance_low,
                                                         resistance_high, target_support_center,
                                                         target_support_gamma, prior_bounces,
                                                         resistance.gamma, target_support_center,
                                                         resistance.center)
                elif previous_close < resistance_low and high[i] >= resistance_low:
                    if high[i] > resistance_high:
                        resistance = None
                    elif close[i] < resistance_low:
                        prior_bounces = resistance.bounces
                        resistance.bounces += 1
                        resistance_signal = maybe_signal(i, Direction.SHORT, resistance_low,
                                                         resistance_high, target_support_center,
                                                         target_support_gamma, prior_bounces,
                                                         resistance.gamma, target_support_center,
                                                         resistance.center)
                    else:
                        resistance.pending = True

            if support_signal is not None and resistance_signal is None:
                output.append(support_signal)
            elif resistance_signal is not None and support_signal is None:
                output.append(resistance_signal)

        return output
