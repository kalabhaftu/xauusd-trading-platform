import json
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from app.data.sessions import SessionSchedule
from app.domain import Reconciliation, utcnow
from app.service import DemoService


def test_session_overnight_and_staleness(tmp_path):
    now = datetime(2026, 10, 12, 1, tzinfo=UTC)
    path = tmp_path / "sessions.json"
    path.write_text(json.dumps({"symbol": "XAUUSD", "server": "Exness-Demo", "generated_utc": now.timestamp(),
                               "server_offset_seconds": 0, "sessions": [{"day": 0, "start": 22 * 3600,
                                                                         "end": 2 * 3600}]}))
    schedule = SessionSchedule(path, "XAUUSD", "Exness-Demo")
    assert schedule.is_open(now)
    assert not schedule.is_open(now + timedelta(hours=2))
    with pytest.raises(ValueError, match="stale"):
        schedule.is_open(now + timedelta(days=2))
    with pytest.raises(ValueError, match="match"):
        SessionSchedule(path, "OTHER", "Exness-Demo")


def test_configure_rejects_broker_changes_and_disarms(settings, store, broker, book):
    service = DemoService(settings, store, broker)
    state = Reconciliation(safe=True, reasons=[], positions=[])
    identifier = store.enqueue("configure", {"account_login": 999})
    service.process_commands(state, book)
    assert next(c for c in store.list("commands") if c["id"] == identifier)["state"] == "REJECTED"
    store.set(f"armed:{broker.account().key}", {"active": True})
    store.enqueue("configure", {"risk_per_trade": .001})
    service.process_commands(state, book)
    assert service.settings.risk_per_trade == .001
    assert not store.get(f"armed:{broker.account().key}")["active"]


def test_stop_disarms_before_cycle_continues(settings, store, broker, book):
    service = DemoService(settings, store, broker)
    store.set(f"armed:{broker.account().key}", {"active": True})
    store.enqueue("stop")
    service.process_commands(Reconciliation(safe=True, reasons=[], positions=[]), book)
    assert service.stop_event.is_set()
    assert not store.get(f"armed:{broker.account().key}")["active"]


def test_monitoring_service_persists_real_adapter_state_disarmed(settings, store, broker):
    # Test fixtures are explicit and injected; runtime never substitutes this broker.
    now = utcnow()
    for tf, freq, count in (("M15", "15min", 1000), ("H1", "1h", 1000)):
        index = pd.date_range(end=pd.Timestamp(now).floor(freq), periods=count, freq=freq)
        broker.frames[tf] = pd.DataFrame({"open": 2000., "high": 2001., "low": 1999., "close": 2000.,
                                         "tick_volume": 1, "spread": 20, "real_volume": 0}, index=index)
    service = DemoService(settings, store, broker)
    service.cycle()
    assert store.get("service")["state"] == "RUNNING"
    assert store.get("active_account")["equity"] == 10000
    assert broker.send_count == 0
    assert not store.load_candles("Exness-Demo", "XAUUSD", "M15").empty


def test_graceful_shutdown_revokes_permission(settings, store, broker):
    service = DemoService(settings, store, broker)
    store.set(f"armed:{broker.account().key}", {"active": True})
    service.stop_event.set()
    service.run()
    assert store.get("service")["state"] == "STOPPED"
    assert not store.get(f"armed:{service.account_key}")["active"]
