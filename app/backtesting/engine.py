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
    costs_verified: bool = True

    def stressed(self, multiplier: float):
        return self.model_copy(update={"commission_per_lot": self.commission_per_lot * multiplier,
                                       "spread_price": self.spread_price * multiplier,
                                       "slippage_price": self.slippage_price * multiplier,
                                       "swap_long_per_lot_day": min(0, self.swap_long_per_lot_day) * multiplier,
                                       "swap_short_per_lot_day": min(0, self.swap_short_per_lot_day) * multiplier})


class BacktestRiskLimits(Model):
    """Portfolio limits applied to every historical entry decision."""

    daily_loss_limit: float = Field(default=0.05, gt=0, le=1)
    weekly_loss_limit: float = Field(default=0.03, gt=0, le=1)
    drawdown_limit: float = Field(default=0.10, gt=0, le=1)
    consecutive_loss_limit: int = Field(default=3, ge=1)
    max_orders_session: int = Field(default=3, ge=1)
    max_positions: int = Field(default=1, ge=1)
    portfolio_risk_limit: float = Field(default=1.0, gt=0, le=1)


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
        if isinstance(tag, dict):
            tag = {**tag, "estimated_stop_risk_usd": risk_per_unit * quantity}
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
                 timeframe: str | None = None, risk_limits: BacktestRiskLimits | None = None,
                 configuration: dict | None = None) -> dict:
    if capital <= 0 or not 0 < risk_fraction <= 1:
        raise ValueError("Invalid capital or risk fraction")
    if cost.account_currency != "USD" or spec.currency_profit != "USD" or cost.price_basis != "bid" \
            or spec.price_basis != "bid":
        raise ValueError("Initial research supports explicitly confirmed bid-based USD gold CFD valuation only")
    if timeframe is None:
        timeframe = next((candidate for candidate in TIMEFRAMES if candidate in frames), None)
        if timeframe is None:
            raise ValueError("No supported candle timeframe was provided")
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
    limits = risk_limits or BacktestRiskLimits()
    risk_events: dict[str, int] = {}

    class SignalAdapter(Strategy):
        def init(self):
            self._risk_day = None
            self._risk_week = None
            self._day_equity = capital
            self._week_equity = capital
            self._high_water = capital
            self._loss_streak = 0
            self._seen_closes = 0
            self._session_entries: dict[str, int] = {}

        def next(self):
            stamp = self.data.index[-1]
            utc = pd.Timestamp(stamp).tz_convert("UTC")
            day_key = utc.date()
            monday_key = day_key - pd.Timedelta(days=day_key.weekday())
            current_equity = float(self.equity)
            if self._risk_day != day_key:
                self._risk_day, self._day_equity = day_key, current_equity
            if self._risk_week != monday_key:
                self._risk_week, self._week_equity = monday_key, current_equity
            self._high_water = max(self._high_water, current_equity)
            broker = self._broker
            closed = self.closed_trades
            while self._seen_closes < len(closed):
                trade = closed[self._seen_closes]
                swap = broker.closed_financing[self._seen_closes] if self._seen_closes < len(
                    broker.closed_financing) else 0.0
                net = float(trade.pl) + float(swap)
                self._loss_streak = self._loss_streak + 1 if net < 0 else 0
                self._seen_closes += 1
            for trade in self.trades:
                metadata = trade.tag or {}
                if metadata.get("exit_at") and stamp + pd.Timedelta(minutes=minutes) >= pd.Timestamp(metadata["exit_at"]):
                    trade.close()
            proposed = mapping.get(stamp)
            if proposed is None or (proposed.expires_at and stamp > pd.Timestamp(proposed.expires_at)):
                return
            if len(self.trades) >= limits.max_positions:
                risk_events["max_positions"] = risk_events.get("max_positions", 0) + 1
                return
            day_loss = max(0.0, (self._day_equity - current_equity) / max(self._day_equity, 1.0))
            week_loss = max(0.0, (self._week_equity - current_equity) / max(self._week_equity, 1.0))
            drawdown = max(0.0, (self._high_water - current_equity) / max(self._high_water, 1.0))
            blocked = None
            if day_loss >= limits.daily_loss_limit:
                blocked = "daily_loss_limit"
            elif week_loss >= limits.weekly_loss_limit:
                blocked = "weekly_loss_limit"
            elif drawdown >= limits.drawdown_limit:
                blocked = "drawdown_limit"
            elif self._loss_streak >= limits.consecutive_loss_limit:
                blocked = "consecutive_loss_limit"
            session_key = proposed.session if proposed.session != "continuous" else utc.date().isoformat()
            if blocked is None and self._session_entries.get(session_key, 0) >= limits.max_orders_session:
                blocked = "max_orders_session"
            if blocked:
                risk_events[blocked] = risk_events.get(blocked, 0) + 1
                return
            sign = 1 if proposed.direction == Direction.LONG else -1
            indicative = float(self.data.Close[-1]) + (cost.spread_price + cost.slippage_price) * multiplier \
                if sign == 1 else float(self.data.Close[-1]) - cost.slippage_price * multiplier
            if sign * (indicative - proposed.stop_loss * multiplier) <= 0 \
                    or sign * (proposed.take_profit * multiplier - indicative) <= 0:
                risk_events["invalid_bracket_after_costs"] = risk_events.get("invalid_bracket_after_costs", 0) + 1
                return
            distance = abs(proposed.entry - proposed.stop_loss) * multiplier
            fees = 2 * cost.commission_per_lot * spec.volume_step
            fees += (cost.spread_price + 2 * cost.slippage_price) * multiplier
            quantity = min(math.floor(self.equity * risk_fraction / (distance + fees)),
                           math.floor(spec.volume_max / spec.volume_step))
            if quantity * spec.volume_step < spec.volume_min:
                risk_events["minimum_lot_risk"] = risk_events.get("minimum_lot_risk", 0) + 1
                return
            horizon_days = max(1, math.ceil((pd.Timestamp(proposed.exit_at) - utc).total_seconds() / 86400)) \
                if proposed.exit_at else 7
            swap = cost.swap_long_per_lot_day if sign > 0 else cost.swap_short_per_lot_day
            projected_unit_risk = distance + fees + max(0, -swap) * spec.volume_step * horizon_days
            open_risk = sum(float((trade.tag or {}).get("estimated_stop_risk_usd", 0)) for trade in self.trades)
            if open_risk + quantity * projected_unit_risk > current_equity * limits.portfolio_risk_limit:
                risk_events["portfolio_risk_limit"] = risk_events.get("portfolio_risk_limit", 0) + 1
                return
            metadata = {"strategy": proposed.strategy, "session": proposed.session, "regime": proposed.regime,
                        "rationale": proposed.rationale, "risk_unit": distance,
                        "exit_at": proposed.exit_at.isoformat() if proposed.exit_at else None}
            action = self.buy if proposed.direction == Direction.LONG else self.sell
            action(size=quantity, sl=proposed.stop_loss * multiplier, tp=proposed.take_profit * multiplier,
                   tag=metadata)
            # Count requested entries conservatively, including broker-simulation rejections.
            self._session_entries[session_key] = self._session_entries.get(session_key, 0) + 1

    test = Backtest(data, SignalAdapter, cash=capital, commission=lambda size, price:
                    abs(size) * spec.volume_step * cost.commission_per_lot,
                    margin=cost.margin_fraction, trade_on_close=False, exclusive_orders=False,
                    hedging=limits.max_positions > 1, finalize_trades=True)
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
        trades["InitialRiskUSD"] = [tag.get("estimated_stop_risk_usd") for tag in trades.Tag]
        for key in ("strategy", "session", "regime", "rationale"):
            trades[key] = [tag.get(key) for tag in trades.Tag]
        trades.drop(columns=["Tag"], inplace=True)
    if start is not None:
        equity = equity.loc[equity.index >= start]
    from app.backtesting.metrics import metrics
    summary = metrics(trades, equity, capital, cost, multiplier)
    return {"metrics": summary, "trades": trades, "equity": equity,
            "cost": cost.model_dump(), "spec": spec.model_dump(mode="json"),
            "dataset_hash": digest({tf: f.to_json(date_format="iso") for tf, f in frames.items()}),
            "engine": "backtesting.py 0.6.5 + CFDBroker v1", "entry_timeframe": timeframe,
            "risk_limits": limits.model_dump(), "risk_blocks": risk_events,
            "candidate_signal_count": len(mapping),
            "configuration": configuration or {"risk_fraction": risk_fraction, "timeframe": timeframe}}
