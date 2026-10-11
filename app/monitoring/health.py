from datetime import datetime

from app.domain import utcnow


def service_health(store) -> dict:
    status = store.get("service", {"state": "STOPPED"})
    if status.get("heartbeat"):
        age = (utcnow() - datetime.fromisoformat(status["heartbeat"])).total_seconds()
        status = dict(status, heartbeat_age_seconds=age)
        if age > 30 and status["state"] not in ("STOPPED", "FAILED"):
            status["state"] = "STALE"
    return status
