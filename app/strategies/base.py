from datetime import timedelta
from typing import Literal

import numpy as np
import pandas as pd
from pydantic import Field

from app.data.validation import TIMEFRAMES
from app.domain import Direction, Model, Signal, digest


class Parameters(Model):
    entry_timeframe: Literal["M5", "M15", "M30"] = "M15"
    trend_timeframe: Literal["H1", "H4"] = "H1"
    atr_period: int = Field(default=14, ge=2, le=200)
    stop_atr: float = Field(default=1.5, gt=0, le=10)
    reward_risk: float = Field(default=2, gt=0, le=10)
    ema_fast: int = Field(default=50, ge=2, le=500)
    ema_slow: int = Field(default=200, ge=3, le=1000)
    pullback_ema: int = Field(default=20, ge=2, le=200)
    pullback_atr: float = Field(default=0.5, gt=0, le=5)
    band_period: int = Field(default=20, ge=3, le=200)
    band_std: float = Field(default=2, gt=0, le=5)
    rsi_period: int = Field(default=14, ge=2, le=100)
    rsi_low: float = Field(default=30, gt=0, lt=50)
    rsi_high: float = Field(default=70, gt=50, lt=100)
    adx_period: int = Field(default=14, ge=2, le=100)
    adx_max: float = Field(default=20, gt=0, le=60)
    breakout_buffer_atr: float = Field(default=0.1, ge=0, le=2)
    london_open_hour: int = Field(default=8, ge=0, le=23)
    new_york_open_hour: int = Field(default=8, ge=0, le=23)
    pre_session_minutes: int = Field(default=60, ge=30, le=360)
    entry_window_minutes: int = Field(default=120, ge=30, le=360)
    breakout_holding_hours: int = Field(default=4, ge=1, le=24)
    retest_max_bars: int = Field(default=3, ge=1, le=20)
    retest: bool = False
    max_holding_hours: int = Field(default=168, ge=1, le=720)
    volatility_min_ratio: float = Field(default=0.5, gt=0)
    volatility_max_ratio: float = Field(default=2, gt=0)


class Strategy:
    name = "base"
    description = ""

    def __init__(self, parameters: Parameters | None = None):
        self.parameters = parameters or Parameters()
        if self.parameters.ema_fast >= self.parameters.ema_slow:
            raise ValueError("Fast EMA must be shorter than slow EMA")
        if self.parameters.volatility_min_ratio >= self.parameters.volatility_max_ratio:
            raise ValueError("Invalid volatility bounds")
        if self.parameters.pre_session_minutes % TIMEFRAMES[self.parameters.entry_timeframe]:
            raise ValueError("Pre-session range must contain a whole number of entry candles")
        self.config_hash = digest({"strategy": self.name, "rules_version": 1,
                                   "parameters": self.parameters.model_dump()})

    def signals(self, frames: dict[str, pd.DataFrame], symbol: str) -> list[Signal]:
        raise NotImplementedError

    def make_signal(self, frame, index: int, symbol: str, direction: Direction, volatility: float,
                    rationale: str, regime: str, indicators: dict, target: float | None = None,
                    session: str = "continuous", exit_at=None) -> Signal | None:
        p = self.parameters
        entry = float(frame.close.iloc[index])
        distance = volatility * p.stop_atr
        sign = 1 if direction == Direction.LONG else -1
        stop = entry - sign * distance
        take = target if target is not None else entry + sign * distance * p.reward_risk
        if not np.isfinite([entry, distance, take, stop, *indicators.values()]).all() or distance <= 0:
            return None
        if sign * (take - entry) < distance:
            return None
        minutes = TIMEFRAMES[p.entry_timeframe]
        timestamp = (frame.index[index] + pd.Timedelta(minutes=minutes)).to_pydatetime()
        return Signal(symbol=symbol, strategy=self.name, config_hash=self.config_hash,
                      direction=direction, timestamp=timestamp, entry=entry, stop_loss=stop, take_profit=take,
                      rationale=rationale, regime=regime, indicators=indicators, session=session,
                      expires_at=timestamp + timedelta(minutes=minutes),
                      exit_at=exit_at or timestamp + timedelta(hours=p.max_holding_hours))
