from datetime import timedelta
from typing import Literal

import numpy as np
import pandas as pd
from pydantic import Field

from app.data.validation import TIMEFRAMES
from app.domain import Direction, Model, Signal, digest


class Parameters(Model):
    """Settings shared by the strategy implementations."""

    entry_timeframe: Literal["M1", "M5", "M15", "M30", "H1", "H4", "D1"] = "M1"
    max_holding_hours: int = Field(default=24, ge=1, le=168)


class IctParameters(Parameters):
    # These numeric conventions make discretionary ICT concepts testable. They are
    # configuration choices for this implementation, not universal ICT settings.
    swing_bars: int = Field(default=2, ge=1, le=10)
    max_level_age_bars: int = Field(default=96, ge=2, le=2000)
    setup_expiry_bars: int = Field(default=24, ge=1, le=500)
    fvg_confirmation_bars: int = Field(default=2, ge=0, le=5)
    minimum_reward_risk: float = Field(default=1.0, gt=0, le=10)


class SupportResistanceParameters(Parameters):
    # The cited study evaluated 60- and 240-minute windows. The default uses 240;
    # the user can test the shorter window and other values on their own feed.
    level_window_minutes: int = Field(default=240, ge=60, le=1440)
    minimum_window_bars: int = Field(default=16, ge=3, le=240)
    minimum_prior_bounces: int = Field(default=1, ge=0, le=20)
    minimum_reward_risk: float = Field(default=1.0, gt=0, le=10)


class Strategy:
    name = "base"
    description = ""
    parameters_model = Parameters
    rules_version = 1

    def __init__(self, parameters: Parameters | dict | None = None):
        if isinstance(parameters, self.parameters_model):
            self.parameters = parameters
        else:
            values = parameters.model_dump() if isinstance(parameters, Model) else parameters or {}
            self.parameters = self.parameters_model.model_validate(values)
        self.config_hash = digest({"strategy": self.name, "rules_version": self.rules_version,
                                   "parameters": self.parameters.model_dump()})

    def signals(self, frames: dict[str, pd.DataFrame], symbol: str) -> list[Signal]:
        raise NotImplementedError

    def make_structural_signal(self, frame: pd.DataFrame, index: int, symbol: str,
                               direction: Direction, stop: float, target: float,
                               minimum_reward_risk: float, rationale: str,
                               regime: str, indicators: dict[str, float]) -> Signal | None:
        """Create a completed-candle signal with a structure-defined bracket."""
        entry = float(frame.close.iloc[index])
        sign = 1 if direction == Direction.LONG else -1
        risk = sign * (entry - stop)
        reward = sign * (target - entry)
        numeric_values = [entry, stop, target, *indicators.values()]
        if not np.isfinite(numeric_values).all() or min(entry, stop, target) <= 0:
            return None
        if risk <= 0 or reward <= 0 or reward / risk < minimum_reward_risk:
            return None
        timeframe = self.parameters.entry_timeframe
        timestamp = (frame.index[index] + pd.Timedelta(minutes=TIMEFRAMES[timeframe])).to_pydatetime()
        return Signal(
            symbol=symbol,
            strategy=self.name,
            config_hash=self.config_hash,
            direction=direction,
            timestamp=timestamp,
            entry=entry,
            stop_loss=float(stop),
            take_profit=float(target),
            rationale=rationale,
            regime=regime,
            indicators=indicators,
            expires_at=timestamp + timedelta(minutes=TIMEFRAMES[timeframe]),
            exit_at=timestamp + timedelta(hours=self.parameters.max_holding_hours),
        )
