from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert

from app.broker.mt5_client import verify_account
from app.database.models import intents
from app.domain import AuditMetadata, Direction, ExecutionResult, utcnow
from app.execution.reconciliation import reconcile
from app.risk.circuit_breaker import CircuitBreaker
from app.risk.manager import RiskManager


class ExecutionEngine:
    def __init__(self, settings, store, broker):
        self.settings, self.store, self.broker = settings, store, broker
        self.risk = RiskManager(settings, store, broker)

    def execute(self, signal, now: datetime, book, schedule) -> ExecutionResult:
        from app.service import normalize_signal
        signal = normalize_signal(signal, self.broker.spec(signal.symbol))
        account = self.broker.account()
        verify_account(account, self.settings)
        key = signal.key(account.key)
        with self.store.engine.connect() as conn:
            if conn.execute(select(intents.c.key).where(intents.c.key == key)).first():
                return ExecutionResult(state="REJECTED", message="Duplicate signal intent")
        state = reconcile(self.broker, self.store, self.settings)
        decision = self.risk.evaluate(signal, now, book, state, schedule)
        self.store.record("signals", signal, account.key, key)
        self.store.record("risk_decisions", {"signal_key": key, **decision.model_dump()}, account.key)
        if not decision.approved:
            return ExecutionResult(state="REJECTED", message=decision.reason)
        comment = "xau:" + key[:24]
        request = self.broker.request(signal.symbol, signal.direction, decision.volume, decision.entry,
                                      signal.stop_loss, signal.take_profit, comment)
        payload = {"signal": signal.model_dump(mode="json"), "decision": decision.model_dump(mode="json"),
                   "comment": comment, "request": request}
        payload["_meta"] = AuditMetadata(environment=account.environment, source=account.server,
                                        configuration_hash=signal.config_hash,
                                        correlation_id=key).model_dump(mode="json")
        with self.store.engine.begin() as conn:
            result = conn.execute(insert(intents).values(key=key, account=account.key, state="PREPARED",
                                                        timestamp=utcnow().isoformat(), payload=payload)
                                  .on_conflict_do_nothing())
            if result.rowcount != 1:
                return ExecutionResult(state="REJECTED", message="Duplicate concurrent intent")
        try:
            ok, reason = self.broker.check(request)
            if not ok:
                result = ExecutionResult(state="REJECTED", message=reason)
            else:
                final_state = reconcile(self.broker, self.store, self.settings, exclude_prepared_key=key)
                final = self.risk.evaluate(signal, utcnow(), book, final_state, schedule)
                if not final.approved or final.volume < decision.volume or abs(final.entry - decision.entry) \
                        > (self.settings.max_slippage_points or 0) * self.broker.spec(signal.symbol).point:
                    result = ExecutionResult(state="REJECTED", message="Final quote/risk/reconciliation check changed")
                else:
                    result = None
                # Final durable permission check before transitioning to possibly-submitted state.
                if result is None:
                    with self.store.engine.begin() as conn:
                        halted = self.store.get(f"halt:{account.key}", {"active": False}, conn)
                        armed = self.store.get(f"armed:{account.key}", {"active": False}, conn)
                        if halted["active"] or not armed.get("active"):
                            result = ExecutionResult(state="REJECTED", message="Permission revoked before send")
                        else:
                            conn.execute(update(intents).where(intents.c.key == key).values(state="SUBMITTING"))
                    if result is None:
                        result = self.broker.send(request)
        except Exception:
            result = ExecutionResult(state="UNKNOWN", message="Submission interrupted; reconciliation required")
        payload["result"] = result.model_dump(mode="json")
        with self.store.engine.begin() as conn:
            conn.execute(update(intents).where(intents.c.key == key).values(state=result.state, payload=payload))
            session = signal.session if signal.session != "continuous" else now.date().isoformat()
            counter_key = f"session_orders:{account.key}:{signal.strategy}:{session}"
            # Count attempted submissions conservatively, including ambiguous outcomes.
            if result.state != "REJECTED":
                self.store.set(counter_key, self.store.get(counter_key, 0, conn) + 1, conn)
            self.store.record("activity", {"event": "execution", "signal_key": key,
                                           "result": result.model_dump()}, account.key, conn=conn)
            self.store.record("notifications", {"event": "execution", "message": result.state},
                              account.key, conn=conn)
        if result.state in ("UNKNOWN", "ACCEPTED", "PARTIAL"):
            CircuitBreaker(self.store, account.key).trip(f"Execution {result.state}; verify actual broker state")
        if result.state in ("FILLED", "PARTIAL"):
            observed = reconcile(self.broker, self.store, self.settings)
            protected = [p for p in observed.positions if p.symbol == signal.symbol and p.magic == self.settings.magic]
            if not protected or not observed.safe:
                CircuitBreaker(self.store, account.key).trip("Filled response lacks reconciled protective position")
            elif sum(p.volume for p in protected) > decision.volume + 1e-8:
                CircuitBreaker(self.store, account.key).trip("Unexpected executed volume exceeds risk-approved size")
            elif any(abs(p.stop_loss - signal.stop_loss) > self.broker.spec(signal.symbol).tick_size / 2
                     or abs(p.take_profit - signal.take_profit) > self.broker.spec(signal.symbol).tick_size / 2
                     for p in protected):
                CircuitBreaker(self.store, account.key).trip("Broker protective levels differ from approved request")
            elif abs(result.price - decision.entry) > (self.settings.max_slippage_points or 0) \
                    * self.broker.spec(signal.symbol).point + 1e-8:
                CircuitBreaker(self.store, account.key).trip("Actual execution exceeded configured slippage tolerance")
        return result

    def close_position(self, position, emergency: bool = False) -> ExecutionResult:
        account = self.broker.account()
        verify_account(account, self.settings)
        if position.magic != self.settings.magic:
            raise ValueError("Refusing to close a position not owned by this bot")
        from app.execution.reconciliation import owned_positions
        if position.ticket not in owned_positions(self.broker, self.store, self.settings, [position]):
            raise ValueError("Position has no persisted bot execution ownership; operator intervention required")
        opposite = Direction.SHORT if position.direction == Direction.LONG else Direction.LONG
        quote = self.broker.quote(position.symbol)
        age = (utcnow() - quote.timestamp).total_seconds()
        if not 0 <= age <= self.settings.quote_max_age_seconds:
            raise ValueError("Cannot close using stale quote")
        price = quote.bid if opposite == Direction.SHORT else quote.ask
        key = f"close:{account.key}:{position.ticket}"
        with self.store.engine.connect() as conn:
            prior = conn.execute(select(intents).where(intents.c.key == key)).mappings().first()
        if prior and prior["state"] in ("SUBMITTING", "UNKNOWN", "ACCEPTED", "PARTIAL", "FILLED"):
            return ExecutionResult(state="UNKNOWN", message="Prior closure requires reconciliation")
        if emergency:
            attempt_key = f"emergency_closure:{account.key}:{position.ticket}"
            with self.store.engine.begin() as conn:
                attempts = self.store.get(attempt_key, {"attempts": 0}, conn)
                if attempts["attempts"] >= self.settings.emergency_close_attempts:
                    return ExecutionResult(state="REJECTED", message="Emergency closure attempt limit reached; "
                                           "remaining exposure requires operator recovery")
                self.store.set(attempt_key, {"attempts": attempts["attempts"] + 1,
                               "last_attempt": utcnow().isoformat(), "position": position.model_dump(mode="json")}, conn)
        request = self.broker.request(position.symbol, opposite, position.volume, price, 0, 0,
                                      "xau:close:" + str(position.ticket), position.ticket)
        payload = {"request": request, "comment": request["comment"], "decision": {"volume": position.volume},
                   "emergency": emergency, "position": position.model_dump(mode="json")}
        payload["_meta"] = AuditMetadata(environment=account.environment, source=account.server,
                                        correlation_id=key).model_dump(mode="json")
        ok, reason = self.broker.check(request)
        if not ok:
            return ExecutionResult(state="REJECTED", message=reason)
        with self.store.engine.begin() as conn:
            conn.execute(insert(intents).values(key=key, account=account.key, state="SUBMITTING",
                                                timestamp=utcnow().isoformat(), payload=payload)
                         .on_conflict_do_update(index_elements=[intents.c.key],
                                                set_={"state": "SUBMITTING", "payload": payload}))
        try:
            result = self.broker.send(request)
        except Exception:
            result = ExecutionResult(state="UNKNOWN", message="Closure outcome unknown")
        payload["result"] = result.model_dump(mode="json")
        with self.store.engine.begin() as conn:
            conn.execute(update(intents).where(intents.c.key == key).values(state=result.state, payload=payload))
        self.store.record("activity", {"event": "position_closure", "result": result.model_dump()}, account.key)
        if result.state != "FILLED":
            CircuitBreaker(self.store, account.key).trip("Position closure failed or uncertain")
        return result
