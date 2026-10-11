import math
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from functools import partial

import numpy as np
import pandas as pd
from backtesting import Backtest, Strategy
from backtesting.backtesting import _Broker
from pydantic import Field

from app.data.validation import TIMEFRAMES, validate
from app.domain import Direction, Model, Signal, SymbolSpec, digest


class CostProfile(Model):
    commission_per_lot: float = Field(ge=0)
    spread_price: float = Field(ge=0)
    slippage_price: float = Field(ge=0)
    swap_long_per_lot_day: float
    swap_short_per_lot_day: float
    account_currency: str = "USD"
    price_basis: str = "bid"
    margin_fraction: float = Field(default=0.01, gt=0, le=1)
    source: str = "explicit user-supplied assumptions"

    def stressed(self, multiplier: float):
        return self.model_copy(update={"commission_per_lot": self.commission_per_lot * multiplier,
                                       "spread_price": self.spread_price * multiplier,
                                       "slippage_price": self.slippage_price * multiplier,
                                       "swap_long_per_lot_day": min(0, self.swap_long_per_lot_day) * multiplier,
                                       "swap_short_per_lot_day": min(0, self.swap_short_per_lot_day) * multiplier})


class CFDBroker(_Broker):
    """Pinned Backtesting.py extension: each integer unit is one broker volume step.

    OHLC is bid-based. Short stop/target triggers are translated into bid prices.
    Parent framework processes SL before TP, including same-bar market entries.
    """
    def __init__(self, *, cost: CostProfile, spec: SymbolSpec, risk_fraction: float, **kwargs):
        super().__init__(**kwargs)
        self.cost, self.spec, self.risk_fraction = cost, spec, risk_fraction
        self.multiplier = spec.contract_size * spec.volume_step
        self.spread_absolute = cost.spread_price * self.multiplier
        self.slippage_absolute = cost.slippage_price * self.multiplier
        self.financing: dict[int, float] = {}
        self.closed_financing: list[float] = []
        self.last_day = None

    def _adjusted_price(self, size=None, price=None):
        price = price if price is not None else self.last_price
        adjusted = price + self.spread_absolute + self.slippage_absolute if size > 0 \
            else price - self.slippage_absolute
        return self._tick(adjusted, ROUND_CEILING if size > 0 else ROUND_FLOOR)

    def _tick(self, value, rounding):
        tick = Decimal(str(self.spec.tick_size)) * Decimal(str(self.multiplier))
        return float((Decimal(str(round(value, 10))) / tick).to_integral_value(rounding=rounding) * tick)

    def _open_trade(self, price, size, sl, tp, time_index, tag):
        if sl is None or tp is None:
            return  # Mandatory protective levels, including baseline experiments.
        sign = 1 if size > 0 else -1
        rounding = ROUND_FLOOR if sign > 0 else ROUND_CEILING
        sl, tp = self._tick(sl, rounding), self._tick(tp, rounding)
        if sign * (price - sl) <= 0 or sign * (tp - price) <= 0:
            return  # Gap has invalidated the proposed bracket.
        risk_per_unit = abs(price - sl) + self.slippage_absolute \
            + 2 * self.cost.commission_per_lot * self.spec.volume_step
        swap = self.cost.swap_long_per_lot_day if sign == 1 else self.cost.swap_short_per_lot_day
        horizon = max(1, math.ceil((pd.Timestamp(tag["exit_at"]) - self._data.index[-1]).total_seconds() / 86400)) \
            if tag and tag.get("exit_at") else 7
        risk_per_unit += max(0, -swap) * self.spec.volume_step * horizon
        maximum = math.floor(self.equity * self.risk_fraction / risk_per_unit)
        maximum = min(maximum, math.floor(self.spec.volume_max / self.spec.volume_step))
        quantity = min(abs(size), maximum)
        if quantity * self.spec.volume_step < self.spec.volume_min:
            return
        if sign < 0:
            sl -= self.spread_absolute
            tp -= self.spread_absolute
        super()._open_trade(price, sign * quantity, sl, tp, time_index, tag)

    def next(self):
        day = self._data.index[-1].date()
        if self.last_day is not None and day > self.last_day:
            elapsed = (day - self.last_day).days
            for trade in self.trades:
                rate = self.cost.swap_long_per_lot_day if trade.is_long else self.cost.swap_short_per_lot_day
                # Calendar-day funding model; explicit assumptions, not claimed broker-exact swaps.
                amount = rate * abs(trade.size) * self.spec.volume_step * elapsed
                self._cash += amount
                self.financing[id(trade)] = self.financing.get(id(trade), 0) + amount
        self.last_day = day
        super().next()

    def _close_trade(self, trade, price, time_index):
        price = price - self.slippage_absolute if trade.is_long \
            else price + self.spread_absolute + self.slippage_absolute
        price = self._tick(price, ROUND_FLOOR if trade.is_long else ROUND_CEILING)
        self.closed_financing.append(self.financing.pop(id(trade), 0))
        super()._close_trade(trade, price, time_index)


def run_backtest(frames: dict[str, pd.DataFrame], signals: list[Signal], spec: SymbolSpec, cost: CostProfile,
                 capital: float = 10000, risk_fraction: float = 0.0025,
                 start: pd.Timestamp | None = None, end: pd.Timestamp | None = None,
                 timeframe="M15") -> dict:
    if capital <= 0 or not 0 < risk_fraction <= 0.005:
        raise ValueError("Invalid capital or risk fraction")
    if cost.account_currency != "USD" or spec.currency_profit != "USD" or cost.price_basis != "bid" \
            or spec.price_basis != "bid":
        raise ValueError("Initial research supports explicitly confirmed bid-based USD gold CFD valuation only")
    minutes = TIMEFRAMES[timeframe]
    frame = frames[timeframe].copy()
    validate(frame, timeframe)
    if start is not None:
        frame = frame.loc[frame.index >= start - pd.Timedelta(minutes=minutes * 2)]
    if end is not None:
        frame = frame.loc[frame.index < end]
    if len(frame) < 3:
        raise ValueError("Insufficient candles for evaluation")
    multiplier = spec.contract_size * spec.volume_step
    data = frame[["open", "high", "low", "close"]].rename(columns=str.title) * multiplier
    data["Volume"] = frame.get("tick_volume", 0)
    mapping = {pd.Timestamp(s.timestamp) - pd.Timedelta(minutes=minutes): s for s in signals
               if (start is None or pd.Timestamp(s.timestamp) >= start)
               and (end is None or pd.Timestamp(s.timestamp) < end)}
    financing = []

    class SignalAdapter(Strategy):
        def init(self):
            pass

        def next(self):
            stamp = self.data.index[-1]
            for trade in self.trades:
                metadata = trade.tag or {}
                if metadata.get("exit_at") and stamp + pd.Timedelta(minutes=minutes) >= pd.Timestamp(metadata["exit_at"]):
                    trade.close()
            if self.position or self.orders:
                return
            proposed = mapping.get(stamp)
            if proposed is None or (proposed.expires_at and stamp > pd.Timestamp(proposed.expires_at)):
                return
            sign = 1 if proposed.direction == Direction.LONG else -1
            indicative = float(self.data.Close[-1]) + (cost.spread_price + cost.slippage_price) * multiplier \
                if sign == 1 else float(self.data.Close[-1]) - cost.slippage_price * multiplier
            if sign * (indicative - proposed.stop_loss * multiplier) <= 0 \
                    or sign * (proposed.take_profit * multiplier - indicative) <= 0:
                return
            distance = abs(proposed.entry - proposed.stop_loss) * multiplier
            fees = 2 * cost.commission_per_lot * spec.volume_step
            fees += (cost.spread_price + 2 * cost.slippage_price) * multiplier
            quantity = min(math.floor(self.equity * risk_fraction / (distance + fees)),
                           math.floor(spec.volume_max / spec.volume_step))
            if quantity * spec.volume_step < spec.volume_min:
                return
            metadata = {"strategy": proposed.strategy, "session": proposed.session, "regime": proposed.regime,
                        "rationale": proposed.rationale, "risk_unit": distance,
                        "exit_at": proposed.exit_at.isoformat() if proposed.exit_at else None}
            action = self.buy if proposed.direction == Direction.LONG else self.sell
            action(size=quantity, sl=proposed.stop_loss * multiplier, tp=proposed.take_profit * multiplier,
                   tag=metadata)

    test = Backtest(data, SignalAdapter, cash=capital, commission=lambda size, price:
                    abs(size) * spec.volume_step * cost.commission_per_lot,
                    margin=cost.margin_fraction, trade_on_close=False, exclusive_orders=False, finalize_trades=True)
    instances = []

    def broker_factory(**kwargs):
        broker = CFDBroker(cost=cost, spec=spec, risk_fraction=risk_fraction, **kwargs)
        instances.append(broker)
        return broker

    test._broker = partial(broker_factory, **test._broker.keywords)
    result = test.run()
    trades = result["_trades"].copy()
    equity = result["_equity_curve"].copy()
    financing = instances[0].closed_financing
    if len(trades):
        trades["Financing"] = financing
        trades["PnL"] += trades.Financing
        trades["VolumeLots"] = trades.Size.abs() * spec.volume_step
        for column in ("EntryPrice", "ExitPrice", "SL", "TP"):
            if column in trades:
                trades[column] /= multiplier
        trades["R"] = [pnl / (abs(size) * tag["risk_unit"]) if tag["risk_unit"] else np.nan
                       for pnl, size, tag in zip(trades.PnL, trades.Size, trades.Tag)]
        for key in ("strategy", "session", "regime", "rationale"):
            trades[key] = [tag.get(key) for tag in trades.Tag]
    if start is not None:
        equity = equity.loc[equity.index >= start]
    from app.backtesting.metrics import metrics
    summary = metrics(trades, equity, capital, cost, multiplier)
    return {"metrics": summary, "trades": trades, "equity": equity,
            "cost": cost.model_dump(), "spec": spec.model_dump(mode="json"),
            "dataset_hash": digest({tf: f.to_json(date_format="iso") for tf, f in frames.items()}),
            "engine": "backtesting.py 0.6.5 + CFDBroker v1", "entry_timeframe": timeframe}
