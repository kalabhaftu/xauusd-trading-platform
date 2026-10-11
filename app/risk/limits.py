from datetime import UTC, datetime, timedelta

from app.domain import Account


def period_keys(now: datetime) -> tuple[str, str]:
    day = now.astimezone(UTC).date()
    monday = day - timedelta(days=day.weekday())
    return day.isoformat(), monday.isoformat()


class EquityBook:
    """Cash-flow-adjusted equity limits, checkpointed across UTC boundaries.

    Mid-period first startup cannot reconstruct historical floating P&L. Such
    periods remain unverified until a monitored reset, rather than fabricating a baseline.
    """
    def __init__(self, store, account: str):
        self.store, self.account = store, account
        self.key = f"equity_book:{account}"

    def observe(self, account: Account, deals: list[dict], now: datetime) -> dict:
        day, week = period_keys(now)
        cashflows = {str(d["ticket"]): float(d.get("profit", 0)) for d in deals if d.get("type") in (2, 3)}
        total_cash = sum(cashflows.values())
        previous = self.store.get(self.key)
        near_reset = now.hour == 0 and now.minute == 0 and now.second <= 15
        if previous is None:
            previous = {"day": day, "week": week, "day_equity": account.equity,
                        "week_equity": account.equity, "day_cash": total_cash, "week_cash": total_cash,
                        "high_water": account.equity, "cash_reference": total_cash, "day_verified": near_reset,
                        "week_verified": near_reset and now.weekday() == 0,
                        "last_seen": now.isoformat()}
        else:
            last = datetime.fromisoformat(previous["last_seen"])
            continuous = 0 <= (now - last).total_seconds() <= 15
            if previous["day"] != day:
                previous.update(day=day, day_equity=account.equity, day_cash=total_cash,
                                day_verified=near_reset and continuous)
            if previous["week"] != week:
                previous.update(week=week, week_equity=account.equity, week_cash=total_cash,
                                week_verified=near_reset and continuous)
        adjusted_equity = account.equity - (total_cash - previous["cash_reference"])
        previous["high_water"] = max(previous["high_water"], adjusted_equity)
        previous["last_seen"] = now.isoformat()
        previous["daily_pnl"] = account.equity - previous["day_equity"] - (total_cash - previous["day_cash"])
        previous["weekly_pnl"] = account.equity - previous["week_equity"] - (total_cash - previous["week_cash"])
        previous["daily_loss"] = max(0, -previous["daily_pnl"] / max(previous["day_equity"], 1))
        previous["weekly_loss"] = max(0, -previous["weekly_pnl"] / max(previous["week_equity"], 1))
        high = previous["high_water"]
        previous["drawdown"] = max(0, 1 - adjusted_equity / high) if high > 0 else 0
        previous["baselines_verified"] = previous["day_verified"] and previous["week_verified"]
        self.store.set(self.key, previous)
        return previous


def consecutive_losses(deals: list[dict], magic: int) -> int:
    # Aggregate position lifecycle costs, including entry commission and swaps.
    totals: dict[int, float] = {}
    closed: dict[int, int] = {}
    for deal in sorted(deals, key=lambda d: (d.get("time_msc", d.get("time", 0) * 1000), d["ticket"])):
        if deal.get("magic") != magic or not deal.get("position_id"):
            continue
        pid = deal["position_id"]
        totals[pid] = totals.get(pid, 0) + sum(float(deal.get(k, 0)) for k in ("profit", "commission", "swap", "fee"))
        if deal.get("entry") in (1, 2, 3):
            closed[pid] = deal.get("time", 0)
    losses = 0
    for pid in sorted(closed, key=lambda pid: closed[pid], reverse=True):
        if totals[pid] < 0:
            losses += 1
        else:
            break
    return losses
