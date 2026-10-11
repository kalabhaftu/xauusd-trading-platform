from datetime import UTC, datetime
from typing import Any


def trade_journal(store, account: str, magic: int) -> list[dict]:
    deals = [r["payload"] for r in store.list("broker_deals", 100000, account)]
    intentions = [r["payload"] for r in store.list("order_intents", 100000, account)]
    signals_by_comment = {i.get("comment"): i.get("signal", {}) for i in intentions if i.get("signal")}
    signals_by_order = {i.get("result", {}).get("order"): i["signal"] for i in intentions
                        if i.get("signal") and i.get("result", {}).get("order")}
    groups: dict[int, list[dict[str, Any]]] = {}
    for deal in deals:
        if deal.get("magic") == magic and deal.get("position_id"):
            groups.setdefault(deal["position_id"], []).append(deal)
    rows = []
    for pid, lifecycle in groups.items():
        entries = [d for d in lifecycle if d.get("entry") in (0, 2)]
        exits = [d for d in lifecycle if d.get("entry") in (1, 3)]
        if not entries:
            continue
        first = min(entries, key=lambda d: d["time"])
        metadata = signals_by_comment.get(first.get("comment")) or signals_by_order.get(first.get("order"), {})
        entered = sum(d["volume"] for d in entries)
        exited = sum(d["volume"] for d in exits)
        complete = exited >= entered - 1e-8
        final_time = max((d["time"] for d in exits), default=None)
        net = sum(sum(float(d.get(k, 0)) for k in ("profit", "commission", "swap", "fee")) for d in lifecycle)
        row = {
            "position": pid, "symbol": first.get("symbol"), "strategy": metadata.get("strategy", "unattributed"),
            "direction": "LONG" if first.get("type") == 0 else "SHORT", "volume_lots": entered,
            "entry_price": sum(d["price"] * d["volume"] for d in entries) / entered,
            "exit_price": sum(d["price"] * d["volume"] for d in exits) / exited if exited else None,
            "stop_loss": metadata.get("stop_loss"), "take_profit": metadata.get("take_profit"),
            "realized_net_pnl": net, "commission": sum(float(d.get("commission", 0)) for d in lifecycle),
            "swap": sum(float(d.get("swap", 0)) for d in lifecycle),
            "fees": sum(float(d.get("fee", 0)) for d in lifecycle),
            "entry_time": datetime.fromtimestamp(first["time"], UTC).isoformat(),
            "exit_time": datetime.fromtimestamp(final_time, UTC).isoformat() if final_time and complete else None,
            "holding_seconds": final_time - first["time"] if final_time and complete else None,
            "status": "CLOSED" if complete else "PARTIALLY_CLOSED" if exits else "OPEN",
            "rationale": metadata.get("rationale"), "source": "broker demo deals",
        }
        rows.append(row)
    return sorted(rows, key=lambda r: r["entry_time"], reverse=True)
