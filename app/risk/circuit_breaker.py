from app.domain import utcnow


class CircuitBreaker:
    def __init__(self, store, account: str):
        self.store, self.account = store, account
        self.key = f"halt:{account}"

    def current(self) -> dict:
        return self.store.get(self.key, {"active": False})

    def trip(self, reason: str) -> None:
        with self.store.engine.begin() as conn:
            existing = self.store.get(self.key, {"active": False}, conn)
            if not existing["active"]:
                value = {"active": True, "reason": reason, "timestamp": utcnow().isoformat()}
                self.store.set(self.key, value, conn)
                self.store.record("circuit_breaker_events", value, self.account, conn=conn)
                self.store.record("notifications", {"event": "circuit_breaker", "message": reason},
                                  self.account, conn=conn)
            self.store.set(f"armed:{self.account}", {"active": False}, conn)

    def reset(self, safe: bool) -> None:
        if not safe:
            raise ValueError("Recovery checks failed; halt cannot be reset")
        with self.store.engine.begin() as conn:
            self.store.set(self.key, {"active": False, "reset_at": utcnow().isoformat()}, conn)
            self.store.record("circuit_breaker_events", {"event": "explicit_reset"}, self.account, conn=conn)
