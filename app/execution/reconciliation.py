from datetime import datetime, timedelta

from sqlalchemy import select, update

from app.broker.mt5_client import verify_account
from app.database.models import intents
from app.domain import Reconciliation, utcnow
from app.risk.circuit_breaker import CircuitBreaker


def owned_positions(broker, store, settings, positions) -> set[int]:
    account = broker.account()
    entries = [r for r in store.list("order_intents", 100000, account.key)
               if r["payload"].get("signal") and r["state"] in
               ("SUBMITTING", "UNKNOWN", "FILLED", "PARTIAL", "PARTIAL_RESOLVED", "ACCEPTED")]
    comments = {r["payload"]["comment"] for r in entries}
    owned = {p.ticket for p in positions if p.magic == settings.magic and p.comment in comments}
    if any(p.magic == settings.magic and p.ticket not in owned for p in positions):
        identifiers = {r["payload"].get("result", {}).get("deal") for r in entries}
        order_ids = {r["payload"].get("result", {}).get("order") for r in entries}
        for deal in broker.history(utcnow() - timedelta(days=35), utcnow()):
            if deal.get("magic") == settings.magic and (deal.get("comment") in comments
                   or (deal.get("ticket") is not None and deal.get("ticket") in identifiers)
                   or (deal.get("order") is not None and deal.get("order") in order_ids)):
                owned.add(deal.get("position_id"))
    return owned


def reconcile(broker, store, settings, exclude_prepared_key=None) -> Reconciliation:
    account = broker.account()
    verify_account(account, settings)
    positions, orders = broker.positions(), broker.orders()
    reasons = []
    ownership = owned_positions(broker, store, settings, positions)
    if any(p.ticket not in ownership for p in positions):
        reasons.append("Position ownership cannot be tied to a submitted bot intent")
    expected_symbol = settings.symbol or store.get("active_symbol")
    if expected_symbol and any(p.symbol != expected_symbol for p in positions):
        reasons.append("Unexpected symbol exposure")
    if any(p.magic != settings.magic for p in positions) or any(o.get("magic") != settings.magic for o in orders):
        reasons.append("Unexpected account exposure on dedicated demo account")
    if orders:
        reasons.append("Pending broker orders block market entries")
    if any(p.stop_loss <= 0 for p in positions):
        reasons.append("Position missing required stop-loss")
    if len(positions) > settings.max_positions:
        reasons.append("Unexpected position count")
    exposure = sum(max(0, -broker.profit(p.symbol, p.direction, p.volume, p.entry, p.stop_loss))
                   for p in positions if p.stop_loss > 0)
    if exposure > account.equity * settings.portfolio_risk_limit + 1e-8:
        reasons.append("Broker exposure exceeds portfolio risk limit")
    with store.engine.connect() as conn:
        unresolved = list(conn.execute(select(intents).where(intents.c.account == account.key,
                                                           intents.c.state.in_(["PREPARED", "SUBMITTING", "UNKNOWN",
                                                                               "ACCEPTED", "PARTIAL"]))).mappings())
    if unresolved:
        earliest = min(datetime.fromisoformat(i["timestamp"]) for i in unresolved) - timedelta(minutes=5)
        deals = broker.history(earliest, utcnow())
        history_orders = broker.order_history(earliest, utcnow()) if hasattr(broker, "order_history") else []
        for intent in unresolved:
            if intent["key"] == exclude_prepared_key and intent["state"] == "PREPARED":
                continue
            token = intent["payload"]["comment"]
            matching = [d for d in deals if d.get("magic") == settings.magic and d.get("comment") == token]
            related = [o for o in history_orders if o.get("magic") == settings.magic and o.get("comment") == token]
            # Broker can rewrite comments. Use recorded result identifiers when available.
            result = intent["payload"].get("result", {})
            if result.get("deal"):
                matching += [d for d in deals if d.get("ticket") == result["deal"]]
            if "position" in intent["payload"]:
                ticket = intent["payload"]["position"]["ticket"]
                exit_deals = [d for d in deals if d.get("position_id") == ticket and d.get("entry") in (1, 2, 3)]
                still_open = any(p.ticket == ticket for p in positions)
                if exit_deals and not still_open:
                    with store.engine.begin() as conn:
                        conn.execute(update(intents).where(intents.c.key == intent["key"]).values(state="CLOSED"))
                    continue
                reasons.append(f"Unresolved closure for position {ticket}")
                continue
            position_ids = {d.get("position_id") for d in matching}
            protected = [p for p in positions if p.ticket in position_ids and p.stop_loss > 0]
            entry_deals = [d for d in matching if d.get("entry") in (0, 2)]
            exit_deals = [d for d in deals if d.get("position_id") in position_ids and d.get("entry") in (1, 2, 3)]
            pending = any(o.get("comment") == token for o in orders)
            volume = sum(float(d.get("volume", 0)) for d in {d["ticket"]: d for d in entry_deals}.values())
            if entry_deals and not pending and (protected or exit_deals):
                state = "FILLED" if volume >= intent["payload"]["decision"]["volume"] - 1e-8 else "PARTIAL_RESOLVED"
            elif related and not pending and all(o.get("state") in (2, 5, 6) for o in related) and not entry_deals:
                state = "REJECTED"
            elif intent["state"] == "PREPARED":
                # Proven pre-submission crash; expire the intent rather than resend an old signal.
                state = "EXPIRED"
            else:
                reasons.append(f"Unresolved execution intent {intent['key'][:12]}")
                continue
            payload = dict(intent["payload"], reconciled_at=utcnow().isoformat(), observed_deals=matching)
            with store.engine.begin() as conn:
                conn.execute(update(intents).where(intents.c.key == intent["key"]).values(state=state, payload=payload))
            for deal in matching:
                store.record("broker_deals", deal, account.key, f"{account.key}:{deal['ticket']}")
    for p in positions:
        store.record("position_snapshots", p, account.key)
    for order in orders:
        store.record("broker_orders", order, account.key)
    result = Reconciliation(safe=not reasons, reasons=reasons, positions=positions)
    store.set(f"reconciliation:{account.key}", dict(result.model_dump(mode="json"), timestamp=utcnow().isoformat()))
    if reasons:
        CircuitBreaker(store, account.key).trip("; ".join(reasons))
    return result
