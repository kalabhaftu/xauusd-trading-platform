import math
import warnings
from datetime import timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from functools import partial
from typing import Literal, cast

import numpy as np
import pandas as pd
from backtesting import Backtest, Strategy
from backtesting._stats import compute_stats
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
    consecutive_loss_limit: int = Field(default=5, ge=1)
    consecutive_loss_policy: Literal["halt", "cooldown"] = "halt"
    loss_cooldown_minutes: int = Field(default=60, ge=1)
    # Account replay quota: maximum filled trades per UTC calendar day.
    max_orders_session: int = Field(default=10, ge=1)
    max_positions: int = Field(default=1, ge=1)
    portfolio_risk_limit: float = Field(default=1.0, gt=0, le=1)


class CFDBroker(_Broker):
    """Pinned Backtesting.py extension: each integer unit is one broker volume step.

    OHLC is bid-based. Short stop/target triggers are translated into bid prices.
    Parent framework processes SL before TP, including same-bar market entries.
    """
    def __init__(self, *, cost: CostProfile, spec: SymbolSpec, risk_fraction: float,
                 portfolio_risk_limit: float, decisions: dict, risk_events: dict,
                 setup_study: bool, reference_capital: float, max_entries_day: int, timestamps: tuple, **kwargs):
        self._processing_orders = 0
        super().__init__(**kwargs)
        self.cost, self.spec, self.risk_fraction = cost, spec, risk_fraction
        self.portfolio_risk_limit = portfolio_risk_limit
        self.setup_study, self.reference_capital = setup_study, reference_capital
        self.max_entries_day = max_entries_day
        self.timestamps = timestamps
        self.multiplier = spec.contract_size * spec.volume_step
        self.spread_absolute = cost.spread_price * self.multiplier
        self.slippage_absolute = cost.slippage_price * self.multiplier
        self.financing: dict[int, float] = {}
        self.closed_financing: list[float] = []
        self.last_day = None
        self.decisions, self.risk_events = decisions, risk_events
        self.daily_fills: dict[str, int] = {}

    @property
    def current_timestamp(self):
        return self.timestamps[len(self._data) - 1]

    @property
    def last_price(self):
        return float(self._data.Open[-1]) if self._processing_orders else super().last_price

    @property
    def equity(self):
        if self._processing_orders:
            return self._fill_equity
        # Open short positions are liquidated on ask, while OHLC is bid-based.
        return self._cash + sum(trade.pl - (self.spread_absolute * abs(trade.size) if trade.is_short else 0)
                                for trade in self.trades)

    @property
    def margin_available(self):
        return self._fill_margin if self._processing_orders else super().margin_available

    def reject(self, decision_id, reason):
        row = self.decisions[decision_id]
        if row["status"] == "rejected":
            return
        row.update(status="rejected", reason=reason)
        self.risk_events[reason] = self.risk_events.get(reason, 0) + 1

    def stop_risk(self, price, stop, sign, timestamp, exit_at):
        return _estimate_stop_risk(price, stop, sign, timestamp, exit_at, self.cost, self.spec,
                                   self.slippage_absolute)

    def _process_orders(self):
        if not self._processing_orders:
            bid_open = float(self._data.Open[-1])
            self._fill_equity = self._cash + sum(
                trade.size * (bid_open + (self.spread_absolute if trade.is_short else 0) - trade.entry_price)
                for trade in self.trades)
            self._fill_margin = max(0.0, self._fill_equity - sum(
                abs(trade.size) * bid_open / self._leverage for trade in self.trades))
            self._fill_open_risk = sum(float((trade.tag or {}).get("estimated_stop_risk_usd", 0))
                                      for trade in self.trades)
        self._processing_orders += 1
        try:
            self._process_entries()
        finally:
            self._processing_orders -= 1

    def _process_entries(self):
        entries = [order for order in self.orders if order.parent_trade is None]
        for order in entries:
            metadata = order.tag or {}
            if metadata.get("expires_at") and self.current_timestamp > pd.Timestamp(metadata["expires_at"]):
                self.reject(metadata["decision_id"], "expired_signal")
                order.cancel()
                continue
            if order.sl is None or order.tp is None:
                self.reject(metadata["decision_id"], "missing_protection")
                order.cancel()
                continue
            sign = 1 if order.size > 0 else -1
            price = self._adjusted_price(order.size, float(self._data.Open[-1]))
            rounding = ROUND_FLOOR if sign > 0 else ROUND_CEILING
            sl, tp = self._tick(order.sl, rounding), self._tick(order.tp, rounding)
            if sign * (price - sl) <= 0 or sign * (tp - price) <= 0:
                self.reject(metadata["decision_id"], "invalid_bracket_after_gap")
                order.cancel()
                continue
            risk = self.stop_risk(price, sl, sign, self.current_timestamp, metadata.get("exit_at"))
            budget = (self.reference_capital if self.setup_study else self._fill_equity) * self.risk_fraction
            self.decisions[metadata["decision_id"]].update(
                risk_budget_usd=budget, estimated_unit_risk_usd=risk,
                estimated_minimum_lot_risk_usd=risk * math.ceil(self.spec.volume_min / self.spec.volume_step),
                minimum_capital=risk * math.ceil(self.spec.volume_min / self.spec.volume_step) / self.risk_fraction)
            quantity = abs(order.size) if self.setup_study else min(
                abs(order.size), math.floor(self._fill_equity * self.risk_fraction / risk))
            if quantity * self.spec.volume_step < self.spec.volume_min:
                self.reject(metadata["decision_id"], "minimum_lot_risk_at_fill")
                order.cancel()
                continue
            commission_unit = self.cost.commission_per_lot * self.spec.volume_step
            quantity = min(quantity, math.floor(self.margin_available * self._leverage / (price + commission_unit)))
            if quantity * self.spec.volume_step < self.spec.volume_min:
                self.reject(metadata["decision_id"], "insufficient_margin")
                order.cancel()
                continue
            order._replace(size=sign * quantity)
        super()._process_orders()
        for order in entries:
            decision_id = (order.tag or {}).get("decision_id")
            if decision_id is not None and order not in self.orders \
                    and self.decisions[decision_id]["status"] == "submitted":
                # All entry orders here are integer market orders; hedging/position limits
                # exclude netting. The pinned parent cancels these when margin is insufficient.
                self.reject(decision_id, "insufficient_margin")

    def _adjusted_price(self, size=None, price=None):
        price = price if price is not None else self.last_price
        adjusted = price + self.spread_absolute + self.slippage_absolute if size > 0 \
            else price - self.slippage_absolute
        return self._tick(adjusted, ROUND_CEILING if size > 0 else ROUND_FLOOR)

    def _tick(self, value, rounding):
        tick = Decimal(str(self.spec.tick_size)) * Decimal(str(self.multiplier))
        return float((Decimal(str(round(value, 10))) / tick).to_integral_value(rounding=rounding) * tick)

    def _open_trade(self, price, size, sl, tp, time_index, tag):
        day = self.current_timestamp.date().isoformat()
        if not self.setup_study and self.daily_fills.get(day, 0) >= self.max_entries_day:
            self.reject(tag["decision_id"], "max_orders_session_at_fill")
            return
        if sl is None or tp is None:
            self.reject(tag["decision_id"], "missing_protection")
            return  # Mandatory protective levels, including baseline experiments.
        sign = 1 if size > 0 else -1
        rounding = ROUND_FLOOR if sign > 0 else ROUND_CEILING
        sl, tp = self._tick(sl, rounding), self._tick(tp, rounding)
        if sign * (price - sl) <= 0 or sign * (tp - price) <= 0:
            self.reject(tag["decision_id"], "invalid_bracket_after_gap")
            return  # Gap has invalidated the proposed bracket.
        risk_per_unit = self.stop_risk(price, sl, sign, self.current_timestamp, tag.get("exit_at"))
        row = self.decisions[tag["decision_id"]]
        row["minimum_capital"] = risk_per_unit * math.ceil(self.spec.volume_min / self.spec.volume_step) \
            / self.risk_fraction
        budget = (self.reference_capital if self.setup_study else self.equity) * self.risk_fraction
        row.update(risk_budget_usd=budget, estimated_unit_risk_usd=risk_per_unit,
                   estimated_minimum_lot_risk_usd=risk_per_unit * math.ceil(
                       self.spec.volume_min / self.spec.volume_step))
        maximum = math.ceil(self.spec.volume_min / self.spec.volume_step) if self.setup_study \
            else math.floor(budget / risk_per_unit)
        maximum = min(maximum, math.floor(self.spec.volume_max / self.spec.volume_step))
        quantity = min(abs(size), maximum)
        if quantity * self.spec.volume_step < self.spec.volume_min:
            self.reject(tag["decision_id"], "minimum_lot_risk_at_fill")
            return
        if not self.setup_study:
            open_risk = self._fill_open_risk
            portfolio_budget = max(0.0, self.equity * self.portfolio_risk_limit - open_risk)
            portfolio_maximum = math.floor(portfolio_budget / risk_per_unit)
            quantity = min(quantity, portfolio_maximum)
            if quantity * self.spec.volume_step < self.spec.volume_min:
                self.reject(tag["decision_id"], "portfolio_risk_limit_at_fill")
                return
        if sign < 0:
            sl -= self.spread_absolute
            tp -= self.spread_absolute
        if isinstance(tag, dict):
            tag = {**tag, "estimated_stop_risk_usd": risk_per_unit * quantity,
                   "over_reference_risk_budget": risk_per_unit * quantity > self.reference_capital * self.risk_fraction}
        row.update(status="filled", reason=None, fill_time=self.current_timestamp.isoformat(),
                   volume_lots=quantity * self.spec.volume_step, initial_risk_usd=risk_per_unit * quantity,
                   over_reference_risk_budget=tag["over_reference_risk_budget"])
        super()._open_trade(price, sign * quantity, sl, tp, time_index, tag)
        day = self.current_timestamp.date().isoformat()
        self.daily_fills[day] = self.daily_fills.get(day, 0) + 1
        bid_open = float(self._data.Open[-1])
        immediate_cost = quantity * (price - bid_open if sign > 0 else bid_open + self.spread_absolute - price) \
            + quantity * self.spec.volume_step * self.cost.commission_per_lot
        self._fill_equity -= immediate_cost
        self._fill_margin = max(0.0, self._fill_margin - immediate_cost - quantity * bid_open / self._leverage)
        self._fill_open_risk += risk_per_unit * quantity

    def rollover(self, day):
        if self.last_day is not None and day > self.last_day:
            elapsed = (day - self.last_day).days
            for trade in self.trades:
                rate = self.cost.swap_long_per_lot_day if trade.is_long else self.cost.swap_short_per_lot_day
                # Calendar-day funding model; explicit assumptions, not claimed broker-exact swaps.
                amount = rate * abs(trade.size) * self.spec.volume_step * elapsed
                self._cash += amount
                self.financing[id(trade)] = self.financing.get(id(trade), 0) + amount
        self.last_day = day

    def next(self):
        self.rollover(self.current_timestamp.date())
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
                 configuration: dict | None = None,
                 mode: Literal["account", "setup_study"] = "account") -> dict:
    if mode not in ("account", "setup_study"):
        raise ValueError("Unsupported backtest mode")
    setup_study = mode == "setup_study"
    if capital <= 0 or not 0 < risk_fraction <= 1:
        raise ValueError("Invalid capital or risk fraction")
    if cost.account_currency != "USD" or spec.currency_profit != "USD" or cost.price_basis != "bid" \
            or spec.price_basis != "bid":
        raise ValueError("Initial research supports explicitly confirmed bid-based USD gold CFD valuation only")
    if timeframe is None:
        timeframe = next((candidate for candidate in TIMEFRAMES
                          if candidate in frames and not frames[candidate].empty), None)
        if timeframe is None:
            raise ValueError("No supported candle timeframe was provided")
    if timeframe not in TIMEFRAMES:
        raise ValueError("Unsupported candle timeframe")
    if start is not None:
        start = pd.Timestamp(start)
        if start.tzinfo is None:
            raise ValueError("Backtest start must be timezone aware")
        start = start.tz_convert("UTC")
    if end is not None:
        end = pd.Timestamp(end)
        if end.tzinfo is None:
            raise ValueError("Backtest end must be timezone aware")
        end = end.tz_convert("UTC")
    if start is not None and end is not None and end <= start:
        raise ValueError("Backtest end must be after start")
    minutes = TIMEFRAMES[timeframe]
    if timeframe not in frames or frames[timeframe].empty:
        raise ValueError(f"No candles are available for {timeframe}")
    frame = frames[timeframe].copy()
    validate(frame, timeframe)
    frame.index = cast(pd.DatetimeIndex, frame.index).tz_convert("UTC")
    if start is not None:
        frame = frame.loc[frame.index >= start - pd.Timedelta(minutes=minutes * 2)]
    if end is not None:
        frame = frame.loc[frame.index < end]
    evaluated = frame
    if start is not None:
        evaluated = evaluated.loc[evaluated.index >= start]
    if end is not None:
        evaluated = evaluated.loc[evaluated.index < end]
    if evaluated.empty:
        raise ValueError("No candles fall inside the requested evaluation interval")
    if len(frame) < 3:
        raise ValueError("Insufficient candles for evaluation")
    multiplier = spec.contract_size * spec.volume_step
    data = frame[["open", "high", "low", "close"]].rename(columns=str.title) * multiplier
    data["Volume"] = frame.get("tick_volume", 0)
    eligible = [s for s in signals if (start is None or pd.Timestamp(s.timestamp) >= start)
                and (end is None or pd.Timestamp(s.timestamp) < end)]
    min_volume_steps = math.ceil(spec.volume_min / spec.volume_step)

    def estimate_setup(s, budget):
        sign = 1 if s.direction == Direction.LONG else -1
        # Signal entry is only an estimate until the next-bar open is known.
        price = s.entry * multiplier + (cost.spread_price + cost.slippage_price) * multiplier \
            if sign > 0 else s.entry * multiplier - cost.slippage_price * multiplier
        stop = s.stop_loss * multiplier
        unit_risk = _estimate_stop_risk(price, stop, sign, pd.Timestamp(s.timestamp), s.exit_at, cost, spec,
                                        cost.slippage_price * multiplier)
        per_lot_risk = unit_risk / spec.volume_step
        raw_lots = budget / per_lot_risk if per_lot_risk > 0 else 0.0
        broker_lots = min(spec.volume_max, math.floor(raw_lots / spec.volume_step) * spec.volume_step)
        return {
            "risk_budget_usd": budget,
            "estimated_unit_risk_usd": unit_risk,
            "estimated_minimum_lot_risk_usd": unit_risk * min_volume_steps,
            "estimated_risk_lots_before_broker_max": raw_lots,
            "estimated_risk_lots": broker_lots,
            "minimum_capital": unit_risk * min_volume_steps / risk_fraction,
        }

    decisions = {i: {"decision_id": i, "signal_time": s.timestamp.isoformat(), "strategy": s.strategy,
                     "direction": str(s.direction), "entry": s.entry, "stop_loss": s.stop_loss,
                     "take_profit": s.take_profit, "status": "unprocessed", "reason": None,
                     "submitted": False, **estimate_setup(s, capital * risk_fraction), "fill_time": None,
                     "volume_lots": None, "initial_risk_usd": None, "over_reference_risk_budget": None}
                 for i, s in enumerate(eligible)}
    mapping = {}
    financing = []
    limits = risk_limits or BacktestRiskLimits()
    risk_events: dict[str, int] = {}
    for i, signal in enumerate(eligible):
        stamp = pd.Timestamp(signal.timestamp) - pd.Timedelta(minutes=minutes)
        if stamp in mapping:
            decisions[i].update(status="rejected", reason="duplicate_completed_candle")
            risk_events["duplicate_completed_candle"] = risk_events.get("duplicate_completed_candle", 0) + 1
        else:
            mapping[stamp] = (signal, i)
    loss_streak_resets = []
    timestamps = tuple(frame.index)
    deadlines = {i: pd.Timestamp(s.exit_at) for i, s in enumerate(eligible) if s.exit_at}
    candle_duration = timedelta(minutes=minutes)
    # An independent setup study cannot exhaust a real account: it sizes every
    # setup at the broker minimum and reports P&L/R, with account returns disabled.
    # Reserve covers maximum positive-price notional and loss on every setup.
    simulation_cash = capital + 4 * len(eligible) * min_volume_steps * float(data.High.max()) \
        + 2 * sum(row["estimated_minimum_lot_risk_usd"] for row in decisions.values()) \
        if setup_study else capital

    class SignalAdapter(Strategy):
        def init(self):
            self._risk_day = None
            self._risk_week = None
            self._day_equity = capital
            self._week_equity = capital
            self._high_water = capital
            self._previous_equity = capital
            self._loss_streak = 0
            self._seen_closes = 0
            self._cooldown_until = None

        def next(self):
            broker = self._broker
            stamp = broker.current_timestamp
            current_equity = capital if setup_study else float(self.equity)
            if not setup_study:
                day_key = stamp.date()
                monday_key = day_key.toordinal() - day_key.weekday()
                if self._risk_day != day_key:
                    self._risk_day, self._day_equity = day_key, self._previous_equity
                if self._risk_week != monday_key:
                    self._risk_week, self._week_equity = monday_key, self._previous_equity
                # Use prior close so first-bar losses survive UTC period resets.
                self._previous_equity = current_equity
                self._high_water = max(self._high_water, current_equity)
                closed = broker.closed_trades
                while self._seen_closes < len(closed):
                    trade = closed[self._seen_closes]
                    swap = broker.closed_financing[self._seen_closes] if self._seen_closes < len(
                        broker.closed_financing) else 0.0
                    net = float(trade.pl) + float(swap)
                    self._loss_streak = self._loss_streak + 1 if net < 0 else 0
                    if net < 0 and self._loss_streak >= limits.consecutive_loss_limit:
                        self._cooldown_until = pd.Timestamp(trade.exit_time) \
                            + timedelta(minutes=limits.loss_cooldown_minutes)
                    self._seen_closes += 1
                if limits.consecutive_loss_policy == "cooldown" and self._cooldown_until is not None \
                        and stamp >= self._cooldown_until:
                    self._loss_streak = 0
                    loss_streak_resets.append(stamp.isoformat())
                    self._cooldown_until = None
            for trade in broker.trades:
                metadata = trade.tag or {}
                deadline = deadlines.get(metadata.get("decision_id", -1))
                if deadline is not None and stamp + candle_duration >= deadline:
                    trade.close()
            candidate = mapping.get(stamp)
            if candidate is None:
                return
            proposed, decision_id = candidate
            row = decisions[decision_id]
            row.update(estimate_setup(proposed, current_equity * risk_fraction))
            if proposed.expires_at and stamp + pd.Timedelta(minutes=minutes) > pd.Timestamp(proposed.expires_at):
                broker.reject(decision_id, "expired_signal")
                return
            if not setup_study and len(broker.trades) >= limits.max_positions:
                broker.reject(decision_id, "max_positions")
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
            signal_day = pd.Timestamp(proposed.timestamp).tz_convert("UTC").date().isoformat()
            if blocked is None and broker.daily_fills.get(signal_day, 0) >= limits.max_orders_session:
                blocked = "max_orders_session"
            if blocked and not setup_study:
                broker.reject(decision_id, blocked)
                return
            sign = 1 if proposed.direction == Direction.LONG else -1
            indicative = float(self.data.Close[-1]) + (cost.spread_price + cost.slippage_price) * multiplier \
                if sign == 1 else float(self.data.Close[-1]) - cost.slippage_price * multiplier
            if sign * (indicative - proposed.stop_loss * multiplier) <= 0 \
                    or sign * (proposed.take_profit * multiplier - indicative) <= 0:
                broker.reject(decision_id, "invalid_bracket_after_costs")
                return
            distance = abs(proposed.entry - proposed.stop_loss) * multiplier
            projected_unit_risk = broker.stop_risk(indicative, proposed.stop_loss * multiplier, sign,
                                                  pd.Timestamp(proposed.timestamp), proposed.exit_at)
            row["minimum_capital"] = projected_unit_risk * math.ceil(spec.volume_min / spec.volume_step) \
                / risk_fraction
            row["estimated_unit_risk_usd"] = projected_unit_risk
            row["estimated_minimum_lot_risk_usd"] = projected_unit_risk * min_volume_steps
            row["risk_budget_usd"] = current_equity * risk_fraction
            raw_lots = row["risk_budget_usd"] / (projected_unit_risk / spec.volume_step)
            row["estimated_risk_lots_before_broker_max"] = raw_lots
            row["estimated_risk_lots"] = min(spec.volume_max,
                math.floor(raw_lots / spec.volume_step) * spec.volume_step)
            quantity = min_volume_steps if setup_study else min(
                math.floor(current_equity * risk_fraction / projected_unit_risk),
                math.floor(spec.volume_max / spec.volume_step))
            if quantity * spec.volume_step < spec.volume_min:
                broker.reject(decision_id, "minimum_lot_risk")
                return
            open_risk = sum(float((trade.tag or {}).get("estimated_stop_risk_usd", 0)) for trade in broker.trades) \
                if not setup_study else 0.0
            if not setup_study:
                remaining = max(0.0, current_equity * limits.portfolio_risk_limit - open_risk)
                quantity = min(quantity, math.floor(remaining / projected_unit_risk))
                if quantity * spec.volume_step < spec.volume_min:
                    broker.reject(decision_id, "portfolio_risk_limit")
                    return
            metadata = {"strategy": proposed.strategy, "session": proposed.session, "regime": proposed.regime,
                        "rationale": proposed.rationale, "risk_unit": distance,
                        "exit_at": proposed.exit_at.isoformat() if proposed.exit_at else None,
                        "expires_at": proposed.expires_at.isoformat() if proposed.expires_at else None,
                        "decision_id": decision_id}
            action = self.buy if proposed.direction == Direction.LONG else self.sell
            action(size=quantity, sl=proposed.stop_loss * multiplier, tp=proposed.take_profit * multiplier,
                   tag=metadata)
            row.update(status="submitted", submitted=True)

    test = Backtest(data, SignalAdapter, cash=simulation_cash, commission=lambda size, price:
                    abs(size) * spec.volume_step * cost.commission_per_lot,
                    margin=cost.margin_fraction, trade_on_close=False, exclusive_orders=False,
                    hedging=setup_study or limits.max_positions > 1, finalize_trades=False)
    instances = []

    def broker_factory(**kwargs):
        broker = CFDBroker(cost=cost, spec=spec, risk_fraction=risk_fraction,
                           portfolio_risk_limit=limits.portfolio_risk_limit,
                           decisions=decisions, risk_events=risk_events,
                           setup_study=setup_study, reference_capital=capital,
                           max_entries_day=limits.max_orders_session, timestamps=timestamps, **kwargs)
        instances.append(broker)
        return broker

    test._broker = partial(broker_factory, **test._broker.keywords)
    # Parent finalize_trades replays the last candle: a newly submitted order
    # can then fill at an open that predates its signal. Finalize explicitly at
    # the last observed close instead, after cancelling all unfilled entries.
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Some trades remain open at the end of backtest.*", category=UserWarning)
        result = test.run()
    broker = instances[0]
    for order in list(broker.orders):
        if order.parent_trade is None:
            broker.reject(order.tag["decision_id"], "unfilled_at_end")
            order.cancel()
    if broker.trades:
        closing_time = frame.index[-1] + candle_duration
        broker.rollover(closing_time.date())
        for trade in list(broker.trades):
            trade.tag["finalized_at_close"] = True
            broker._close_trade(trade, float(data.Close.iloc[-1]), len(data) - 1)
        broker._equity[-1] = broker.equity
        result = compute_stats(trades=broker.closed_trades,
                               equity=pd.Series(broker._equity).bfill().fillna(broker._cash).to_numpy(),
                               ohlc_data=data, strategy_instance=result["_strategy"])
    for decision_id, row in decisions.items():
        if row["status"] in ("unprocessed", "submitted"):
            instances[0].reject(decision_id, "unfilled_at_end" if row["submitted"] else "no_executable_candle")
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
        trades["InitialRiskUSD"] = [tag.get("estimated_stop_risk_usd") for tag in trades.Tag]
        trades["R"] = trades.PnL / trades.InitialRiskUSD.replace(0, np.nan)
        trades["OverReferenceRiskBudget"] = [tag.get("over_reference_risk_budget", False) for tag in trades.Tag]
        trades["FinalizedAtClose"] = [tag.get("finalized_at_close", False) for tag in trades.Tag]
        trades.loc[trades.FinalizedAtClose, "ExitTime"] = frame.index[-1] + candle_duration
        trades["Duration"] = trades.ExitTime - trades.EntryTime
        for key in ("strategy", "session", "regime", "rationale"):
            trades[key] = [tag.get(key) for tag in trades.Tag]
        trades.drop(columns=["Tag"], inplace=True)
    if start is not None:
        equity = equity.loc[equity.index >= start]
    from app.backtesting.metrics import metrics
    summary = metrics(trades, equity, simulation_cash, cost, multiplier)
    summary["net_pnl"] = float(trades.PnL.sum()) if len(trades) else 0.0
    if setup_study:
        pnl = equity.Equity - simulation_cash
        summary["maximum_drawdown_usd"] = float((pnl.cummax().clip(lower=0) - pnl).max())
        for key in ("initial_capital", "final_equity", "total_net_return", "annualized_return",
                    "maximum_drawdown", "recovery_factor", "sharpe", "sortino", "exposure_time"):
            summary[key] = None
        summary["monthly_returns"] = {}
        summary["reference_capital"] = capital
        equity = pd.DataFrame({"CumulativePnL": pnl})
    quality = validate(evaluated, timeframe)
    data_quality = {key: value for key, value in quality.items() if key != "gaps"}
    data_quality.update(timeframe=timeframe, unresolved_gap_count=len(quality["gaps"]),
                        calendar_days=(evaluated.index[-1] - evaluated.index[0]).total_seconds() / 86400,
                        requested_start=str(start) if start is not None else None,
                        requested_end_exclusive=str(end) if end is not None else None)
    audit = {"candidates": len(decisions), "submitted": sum(bool(r["submitted"]) for r in decisions.values()),
             "filled": sum(r["status"] == "filled" for r in decisions.values()),
             "rejected": sum(r["status"] == "rejected" for r in decisions.values()),
             "filled_over_reference_risk_budget": sum(r["status"] == "filled" and
                 bool(r["over_reference_risk_budget"]) for r in decisions.values()),
             "loss_streak_resets": len(loss_streak_resets)}
    return {"metrics": summary, "trades": trades, "equity": equity,
            "cost": cost.model_dump(), "spec": spec.model_dump(mode="json"),
            "dataset_hash": digest({tf: f.to_json(date_format="iso") for tf, f in frames.items()}),
            "engine": "backtesting.py 0.6.5 + CFDBroker v6", "entry_timeframe": timeframe,
            "mode": mode, "account_limits_applied": not setup_study,
            "reference_capital": capital, "risk_fraction": risk_fraction,
            "risk_limits": limits.model_dump(), "risk_blocks": risk_events,
            "candidate_signal_count": len(decisions), "execution_audit": audit,
            "entry_decisions": pd.DataFrame(list(decisions.values()), columns=[
                "decision_id", "signal_time", "strategy", "direction", "entry", "stop_loss", "take_profit",
                "status", "reason", "submitted", "risk_budget_usd", "estimated_unit_risk_usd",
                "estimated_minimum_lot_risk_usd", "estimated_risk_lots_before_broker_max",
                "estimated_risk_lots", "minimum_capital", "fill_time", "volume_lots", "initial_risk_usd",
                "over_reference_risk_budget"
            ]), "data_quality": data_quality,
            "loss_streak_resets": loss_streak_resets,
            "configuration": configuration or {"risk_fraction": risk_fraction, "timeframe": timeframe}}


def _estimate_stop_risk(price, stop, sign, timestamp, exit_at, cost, spec, slippage_absolute):
    """All-in risk for one broker volume step, using the engine's price scaling."""
    horizon = max(1, math.ceil((pd.Timestamp(exit_at) - pd.Timestamp(timestamp)).total_seconds() / 86400)) \
        if exit_at else 7
    swap = cost.swap_long_per_lot_day if sign > 0 else cost.swap_short_per_lot_day
    return (abs(price - stop) + slippage_absolute
            + 2 * cost.commission_per_lot * spec.volume_step
            + max(0, -swap) * spec.volume_step * horizon)
