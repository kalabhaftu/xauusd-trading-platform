import threading

import httpx


class AlertWorker:
    """Best-effort durable notification outbox, independent of execution latency."""
    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, name="telegram-outbox", daemon=True)

    def start(self) -> None:
        if self.settings.telegram_token and self.settings.telegram_chat_id:
            self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=1)

    def run(self) -> None:
        while not self.stop_event.wait(5):
            for record in reversed(self.store.list("notifications", 100)):
                key = f"notification_delivery:{record['id']}"
                delivery = self.store.get(key, {"attempts": 0})
                if delivery.get("delivered") or delivery["attempts"] >= 5:
                    continue
                text = f"XAU demo • {record['payload'].get('event', 'event')}\n{record['payload'].get('message', '')}"
                for value in (self.settings.account_password, self.settings.telegram_token,
                              self.settings.telegram_chat_id):
                    if value:
                        text = text.replace(value.get_secret_value(), "[REDACTED]")
                try:
                    response = httpx.post(
                        f"https://api.telegram.org/bot{self.settings.telegram_token.get_secret_value()}/sendMessage",
                        json={"chat_id": self.settings.telegram_chat_id.get_secret_value(), "text": text[:3500]},
                        timeout=5,
                    )
                    delivered = response.is_success and response.json().get("ok", False)
                except Exception:
                    delivered = False
                self.store.set(key, {"attempts": delivery["attempts"] + 1, "delivered": delivered})
