"""Cost-aware forward outcome tracking for signals that are not routed to a broker."""

import math
from datetime import datetime

import pandas as pd

from app.data.validation import TIMEFRAMES
from app.domain import Direction, utcnow


class SignalOutcomeTracker:
    """Track signal-only ideas as explicitly simulated positions, never broker fills."""

    def __init__(self, store, settings):
        self.store, self.settings = store, settings

    def start(self, signal, account_key, spec, equity):
        key = signal.key(account_key)
        state_key = f"signal_outcome:{account_key}:{key}"
        if self.store.get(state_key):
            return
        missing_costs = [name for name in ("commission_per_lot", "slippage_price", "swap_long_per_lot_day",
                                           "swap_short_per_lot_day") if getattr(self.settings, name) is None]
        self.store.set(state_key, {
            "signal_key": key,
            "account": account_key,
            "symbol": signal.symbol,
            "strategy": signal.strategy,
            "config_hash": signal.config_hash,
            "direction": signal.direction.value,
            "signal_time": signal.timestamp.isoformat(),
            "entry_condition": signal.entry_condition,
            "planned_entry": signal.entry,
            "stop_loss": signal.stop_loss,
            "take_profit": signal.take_profit,
            "rationale": signal.rationale,
            "regime": signal.regime,
            "indicators": signal.indicators,
            "session": signal.session,
            "expires_at": signal.expires_at.isoformat() if signal.expires_at else None,
            "exit_at": signal.exit_at.isoformat() if signal.exit_at else None,
            "equity_at_signal": float(equity),
            "risk_fraction": self.settings.risk_per_trade,
            "cost_assumptions": {
                "max_spread": self.settings.max_spread,
                "commission_per_lot": self.settings.commission_per_lot,
                "slippage_price": self.settings.slippage_price,
                "swap_long_per_lot_day": self.settings.swap_long_per_lot_day,
                "swap_short_per_lot_day": self.settings.swap_short_per_lot_day,
                "financing_model": "calendar-day estimate; broker rollover multipliers are not reconstructed",
            },
            "symbol_specification": {
                "point": spec.point,
                "contract_size": spec.contract_size,
                "volume_min": spec.volume_min,
                "volume_max": spec.volume_max,
                "volume_step": spec.volume_step,
                "tick_size": spec.tick_size,
                "currency_profit": spec.currency_profit,
            },
            "status": "PENDING_ENTRY" if not missing_costs else "COSTS_UNAVAILABLE",
            "outcome_note": None if not missing_costs else f"Missing modeled inputs: {', '.join(missing_costs)}",
            "entry_time": None,
            "exit_time": None,
            "entry_price": None,
            "exit_price": None,
            "volume_lots": 0.0,
            "realized_net_pnl": None,
            "r_multiple": None,
            "exit_reason": None,
            "model": "next broker candle open; simulated, not an MT5 order",
            "recorded_at": utcnow().isoformat(),
        })

    def update(self, account_key, symbol, timeframe, frame, spec):
        if frame.empty:
            return
        prefix = f"signal_outcome:{account_key}:"
        minutes = TIMEFRAMES[timeframe]
        for record in self.store.list_state(prefix):
            trade = record["value"]
            if trade.get("status") not in ("PENDING_ENTRY", "OPEN") or trade.get("symbol") != symbol:
                continue
            costs = trade["cost_assumptions"]
            specification = trade["symbol_specification"]
            point_value = float(specification["point"])
            contract_size = float(specification["contract_size"])
            step = float(specification["volume_step"])
            signal_time = pd.Timestamp(trade["signal_time"])
            after = trade.get("last_processed_candle")
            eligible = frame.loc[frame.index >= signal_time]
            if after:
                eligible = eligible.loc[eligible.index > pd.Timestamp(after)]
            for stamp, bar in eligible.iterrows():
                spread_points = float(bar.get("spread", 0) or 0)
                spread = spread_points * point_value
                if spread <= 0:
                    spread = float(costs["max_spread"] or 0)
                slip = float(costs["slippage_price"] or 0)
                long = trade["direction"] == Direction.LONG.value
                bar_open = float(bar.open)
                if trade["status"] == "PENDING_ENTRY":
                    expires = pd.Timestamp(trade["expires_at"]) if trade.get("expires_at") else None
                    if expires is not None and stamp > expires:
                        trade.update(status="EXPIRED", last_processed_candle=stamp.isoformat(),
                                     outcome_note="No next-candle entry before signal expiry")
                        break
                    entry = bar_open + spread + slip if long else bar_open - slip
                    stop = float(trade["stop_loss"])
                    target = float(trade["take_profit"])
                    stop_distance = abs(entry - stop)
                    if (long and (entry <= stop or target <= entry)) or (not long and (entry >= stop or target >= entry)):
                        trade.update(status="INVALID_BRACKET", last_processed_candle=stamp.isoformat(),
                                     outcome_note="Next-candle open invalidated the planned stop/target")
                        break
                    commission = float(costs["commission_per_lot"] or 0)
                    per_lot_risk = stop_distance * contract_size + (spread + 2 * slip) * contract_size \
                        + 2 * commission
                    risk_cash = max(0.0, float(trade["equity_at_signal"])) * trade["risk_fraction"]
                    lots = math.floor((risk_cash / per_lot_risk) / step) * step if per_lot_risk > 0 else 0
                    if lots + 1e-10 < float(specification["volume_min"]):
                        trade.update(status="BELOW_MINIMUM_VOLUME", last_processed_candle=stamp.isoformat(),
                                     outcome_note="Broker minimum lot would exceed the configured risk fraction")
                        break
                    trade.update(status="OPEN", entry_time=stamp.isoformat(), entry_price=entry,
                                 volume_lots=round(lots, 8), risk_cash=lots * per_lot_risk,
                                 entry_spread=spread, last_processed_candle=stamp.isoformat())
                else:
                    trade["last_processed_candle"] = stamp.isoformat()

                exit_price, reason = self._bar_exit(trade, bar, stamp, minutes, spread, slip, long)
                if exit_price is None:
                    lots = float(trade["volume_lots"])
                    entry = float(trade["entry_price"])
                    mark = float(bar.close) - slip if long else float(bar.close) + spread + slip
                    gross = (mark - entry if long else entry - mark) * lots * contract_size
                    commission_cost = 2 * float(costs["commission_per_lot"] or 0) * lots
                    entry_time = datetime.fromisoformat(trade["entry_time"])
                    elapsed_days = max(0, (stamp + pd.Timedelta(minutes=minutes)
                                           - pd.Timestamp(entry_time)).days)
                    swap = costs["swap_long_per_lot_day"] if long else costs["swap_short_per_lot_day"]
                    financing = float(swap or 0) * lots * elapsed_days
                    trade["unrealized_net_pnl"] = gross - commission_cost + financing
                    trade["mark_price"] = mark
                    continue
                exit_time = stamp + pd.Timedelta(minutes=minutes)
                lots = float(trade["volume_lots"])
                entry = float(trade["entry_price"])
                gross = (exit_price - entry if long else entry - exit_price) * lots * contract_size
                commission_cost = 2 * float(costs["commission_per_lot"] or 0) * lots
                entry_slippage = slip * lots * contract_size
                exit_slippage = slip * lots * contract_size
                elapsed_days = max(0, (exit_time.to_pydatetime() - datetime.fromisoformat(
                    trade["entry_time"]).astimezone(exit_time.tzinfo)).days)
                swap = costs["swap_long_per_lot_day"] if long else costs["swap_short_per_lot_day"]
                financing = float(swap or 0) * lots * elapsed_days
                net = gross - commission_cost + financing
                risk_cash = float(trade.get("risk_cash") or 0)
                trade.update(status="CLOSED", exit_time=exit_time.isoformat(), exit_price=exit_price,
                             exit_reason=reason, gross_pnl=gross, commission=commission_cost,
                             financing=financing, modeled_entry_slippage=entry_slippage,
                             modeled_exit_slippage=exit_slippage, realized_net_pnl=net,
                             r_multiple=net / risk_cash if risk_cash else None,
                             last_processed_candle=stamp.isoformat())
                outcome = "WIN" if net > 0 else "LOSS" if net < 0 else "BREAKEVEN"
                self.store.record("notifications", {
                    "event": "signal_outcome",
                    "message": f"{trade['symbol']} {trade['direction']} • {trade['strategy']} • "
                               f"simulated {outcome} • net {net:.2f} USD • "
                               f"{trade['r_multiple']:.2f}R • {reason}",
                }, account_key, key=f"signal-outcome:{account_key}:{trade['signal_key']}")
                break
            trade["updated_at"] = utcnow().isoformat()
            self.store.set(record["key"], trade)

    @staticmethod
    def _bar_exit(trade, bar, stamp, minutes, spread, slip, long):
        bar_open = float(bar.open)
        low, high, close = float(bar.low), float(bar.high), float(bar.close)
        stop, target = float(trade["stop_loss"]), float(trade["take_profit"])
        if long:
            hit_stop, hit_target = low <= stop, high >= target
            stop_fill = min(bar_open, stop) - slip
            target_fill = target - slip
        else:
            ask_open, ask_low, ask_high = bar_open + spread, low + spread, high + spread
            hit_stop, hit_target = ask_high >= stop, ask_low <= target
            stop_fill = max(ask_open, stop) + slip
            target_fill = target + slip
        if hit_stop:
            return stop_fill, "STOP_LOSS" if not hit_target else "AMBIGUOUS_STOP_FIRST"
        if hit_target:
            return target_fill, "TAKE_PROFIT"
        exit_at = pd.Timestamp(trade["exit_at"]) if trade.get("exit_at") else None
        if exit_at is not None and stamp + pd.Timedelta(minutes=minutes) >= exit_at:
            return close - slip if long else close + spread + slip, "TIME_EXIT"
        return None, None
