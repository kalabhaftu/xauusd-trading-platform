from app.monitoring.alerts import send_telegram_message


class FakeResponse:
    is_success = True
    status_code = 200

    @staticmethod
    def json():
        return {"ok": True, "result": {"message_id": 5}}


def test_test_message_uses_bounded_telegram_request(monkeypatch):
    calls = []
    monkeypatch.setattr("app.monitoring.alerts.httpx.post",
                        lambda *args, **kwargs: calls.append((args, kwargs)) or FakeResponse())
    ok, message = send_telegram_message("test-token", "123", "Aurum test")
    assert ok and message == "Message delivered"
    assert calls[0][0][0].endswith("/sendMessage")
    assert calls[0][1]["json"] == {"chat_id": "123", "text": "Aurum test"}
    assert calls[0][1]["timeout"] == 5
