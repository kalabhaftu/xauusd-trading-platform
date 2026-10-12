import math
from datetime import datetime
from decimal import Decimal

from app.broker.mt5_client import verify_account
from app.domain import Direction, RiskDecision, Signal
from app.risk.circuit_breaker import CircuitBreaker
from app.risk.sizing import size


class RiskManager:
    def __init__(self, settings, store, broker):
        self.settings, self.store, self.broker = settings, store, broker

    def evaluate(self, signal: Signal, now: datetime, book: dict, reconciliation, schedule) -> RiskDecision:
        try:
            return self._evaluate(signal, now, book, reconciliation, schedule)
        except Exception as exc:
            return RiskDecision(approved=False, reason=str(exc))

    def _evaluate(self, signal, now, book, reconciliation, schedule):
        settings = self.settings
        settings.require_execution_environment()
        settings.require_costs()
        account = self.broker.account()
        verify_account(account, settings)
        if account.currency != "USD":
            raise ValueError("Validated order sizing currently supports USD broker accounts only")
        breaker = CircuitBreaker(self.store, account.key)
        if breaker.current()["active"]:
            raise ValueError("Persistent circuit breaker active")
        armed = self.store.get(f"armed:{account.key}", {"active": False})
        if not armed.get("active") or armed.get("config_hash") != signal.config_hash:
            raise ValueError("Automatic order permission is off or the strategy configuration changed")
        from app.service import permission_fingerprint
        if armed.get("fingerprint") != permission_fingerprint(settings):
            raise ValueError("Execution settings changed since explicit authorization")
        if not reconciliation.safe:
            raise ValueError("Position/order state has not reconciled safely")
        approval = self.store.get(f"approval:{signal.config_hash}", {})
        if not approval.get("approved") or approval.get("symbol") != signal.symbol \
                or approval.get("source") != account.server:
            raise ValueError("No matching evidence-backed strategy approval")
        if not book.get("baselines_verified"):
            raise ValueError("Daily/weekly risk baselines are unverified")
        for key, limit in (("daily_loss", settings.daily_loss_limit), ("weekly_loss", settings.weekly_loss_limit),
                           ("drawdown", settings.drawdown_limit)):
            if book.get(key, 1) >= limit:
                breaker.trip(f"{key} threshold reached")
                raise ValueError(f"{key} threshold reached")
        if book.get("consecutive_losses", 0) >= settings.consecutive_loss_limit:
            breaker.trip("Consecutive-loss limit reached")
            raise ValueError("Consecutive-loss limit reached")
        if len(reconciliation.positions) >= settings.max_positions:
            raise ValueError("Open-position limit reached")
        if settings.max_spread is None or settings.max_slippage_points is None:
            raise ValueError("Explicit spread/slippage tolerances required")
        quote = self.broker.quote(signal.symbol)
        if not 0 <= (now - quote.timestamp).total_seconds() <= settings.quote_max_age_seconds:
            breaker.trip("Stale/future quote")
            raise ValueError("Stale/future quote")
        if quote.spread < 0 or quote.spread > settings.max_spread:
            breaker.trip("Invalid or excessive spread")
            raise ValueError("Spread filter rejected quote")
        if not schedule.is_open(now):
            raise ValueError("Broker trading session closed")
        if signal.timestamp > now or (signal.expires_at and now > signal.expires_at):
            raise ValueError("Signal expired or not yet available")
        if signal.exit_at and now >= signal.exit_at:
            raise ValueError("Signal holding window already expired")
        spec = self.broker.spec(signal.symbol)
        from app.domain import digest
        if approval.get("spec_hash") != digest(spec.model_dump(exclude={"timestamp"})) \
                or approval.get("risk_fraction") != settings.risk_per_trade:
            raise ValueError("Broker specifications or risk fraction changed since research approval")
        if spec.trade_mode != 4:
            raise ValueError("Symbol does not permit unrestricted trading")
        entry = quote.ask if signal.direction == Direction.LONG else quote.bid
        sign = 1 if signal.direction == Direction.LONG else -1
        if sign * (entry - signal.stop_loss) <= 0 or sign * (signal.take_profit - entry) <= 0:
            raise ValueError("Stop/target wrong side of current quote")
        for level in (signal.stop_loss, signal.take_profit):
            ticks = Decimal(str(level)) / Decimal(str(spec.tick_size))
            if abs(float(ticks - ticks.to_integral_value())) > 1e-6:
                raise ValueError("Protective price not aligned to broker tick size")
        reference = quote.bid if signal.direction == Direction.LONG else quote.ask
        if min(abs(reference - signal.stop_loss), abs(reference - signal.take_profit)) \
                < spec.stops_level * spec.point:
            raise ValueError("Broker minimum stop distance violated")
        slippage = max(settings.slippage_price, settings.max_slippage_points * spec.point)
        worst_entry = entry + sign * slippage
        if sign * (signal.take_profit - worst_entry) <= 0:
            raise ValueError("Slippage could invalidate target")
        costs = 2 * settings.commission_per_lot
        # Include known daily financing through the configured strategy's maximum horizon.
        days = max(1, math.ceil((signal.exit_at - now).total_seconds() / 86400)) if signal.exit_at else 7
        swap = settings.swap_long_per_lot_day if sign == 1 else settings.swap_short_per_lot_day
        costs += max(0, -swap) * days
        budget = account.equity * min(settings.risk_per_trade, settings.hard_risk_max)
        worst_stop_fill = signal.stop_loss - sign * slippage
        if worst_stop_fill <= 0:
            raise ValueError("Adverse stop fill assumption is invalid")
        volume, risk = size(self.broker, spec, signal.direction, worst_entry, worst_stop_fill, budget, costs)
        if risk > account.equity * settings.portfolio_risk_limit:
            raise ValueError("Combined portfolio risk limit exceeded")
        margin = self.broker.margin(signal.symbol, signal.direction, volume, worst_entry)
        if not math.isfinite(margin) or margin < 0 or margin > account.free_margin * 0.9:
            raise ValueError("Insufficient margin with 10% reserve")
        session = signal.session if signal.session != "continuous" else now.date().isoformat()
        count = self.store.get(f"session_orders:{account.key}:{signal.strategy}:{session}", 0)
        if count >= settings.max_orders_session:
            raise ValueError("Session entry limit reached")
        return RiskDecision(approved=True, reason="All broker risk checks passed", volume=volume,
                            entry=entry, risk_amount=risk, margin=margin)
