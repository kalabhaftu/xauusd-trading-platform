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


def test_switching_execution_mode_requires_fresh_demo_authorization(settings, store, broker, book):
    service = DemoService(settings, store, broker)
    account_key = broker.account().key
    store.set(f"armed:{account_key}", {"active": True, "fingerprint": "old"})
    store.enqueue("set-execution-mode", {"mode": "AUTO_DEMO"})
    service.process_commands(Reconciliation(safe=True, reasons=[], positions=[]), book)
    assert store.get(f"execution_mode:{account_key}") == "AUTO_DEMO"
    assert store.get(f"armed:{account_key}") == {"active": False}
    store.enqueue("set-execution-mode", {"mode": "SIGNAL_ONLY"})
    service.process_commands(Reconciliation(safe=True, reasons=[], positions=[]), book)
    assert store.get(f"execution_mode:{account_key}") == "SIGNAL_ONLY"
    assert store.get(f"armed:{account_key}") == {"active": False}


def test_monitor_strategy_selection_is_validated_and_clears_order_permission(settings, store, broker, book):
    service = DemoService(settings, store, broker)
    account_key = broker.account().key
    store.set(f"armed:{account_key}", {"active": True, "config_hash": "old"})
    store.enqueue("set-monitored-strategies", {"names": ["support_resistance_bounce", "ict_sweep_mss_fvg"]})

    service.process_commands(Reconciliation(safe=True, reasons=[], positions=[]), book)

    assert store.get("monitored_strategies") == ["support_resistance_bounce", "ict_sweep_mss_fvg"]
    assert store.get(f"armed:{account_key}") == {"active": False}
    store.enqueue("set-monitored-strategies", {"names": ["unknown"]})
    service.process_commands(Reconciliation(safe=True, reasons=[], positions=[]), book)
    assert store.get("monitored_strategies") == ["support_resistance_bounce", "ict_sweep_mss_fvg"]


def test_bot_position_alerts_once_then_every_fifteen_minutes(settings, store, broker):
    from datetime import timedelta

    from app.domain import Direction, Position, utcnow
    from app.service import DemoService

    account = broker.account()
    store.set("active_account", account.model_dump(mode="json"))
    service = DemoService(settings, store, broker)
    position = Position(ticket=88, symbol="XAUUSD", direction=Direction.LONG, volume=.02, entry=2000,
                        stop_loss=1990, take_profit=2020, profit=1.25, magic=settings.magic,
                        timestamp=utcnow(), comment="fixture")
    now = utcnow()
    service.position_updates([position], {position.ticket}, account, now)
    assert len(store.list("notifications", 100, account.key)) == 1
    assert "Bot position OPEN" in store.list("notifications", 100, account.key)[0]["payload"]["message"]

    service.position_updates([position], {position.ticket}, account, now + timedelta(minutes=14, seconds=59))
    assert len(store.list("notifications", 100, account.key)) == 1
    service.position_updates([position], {position.ticket}, account, now + timedelta(minutes=15))
    records = store.list("notifications", 100, account.key)
    assert len(records) == 2
    assert "15-minute update" in records[0]["payload"]["message"]
    assert "Floating P&L" in records[0]["payload"]["message"]


def test_mt5_permission_loss_notifies_once_and_keeps_halt_until_explicit_recovery(
        settings, store, broker):
    from app.monitoring.alerts import AlertWorker
    service = DemoService(settings, store, broker)
    account = broker.account()
    blocked = account.model_copy(update={"expert_allowed": False, "terminal_trade_allowed": False})
    service.check_trading_permissions(blocked)
    service.check_trading_permissions(blocked)

    alerts = store.list("notifications", 20, account.key)
    assert sum(row["payload"].get("event") == "trading_permission_lost" for row in alerts) == 1
    assert store.get(f"halt:{account.key}")["active"]
    assert "Tools → Options → Expert Advisors" in next(
        row["payload"]["message"] for row in alerts if row["payload"].get("event") == "trading_permission_lost")
    assert AlertWorker.should_send({"enabled": True, "events": ["signal"]}, "signal")
    assert not AlertWorker.should_send({"enabled": True, "events": ["signal"]}, "position_update")
    assert not AlertWorker.should_send({"enabled": False}, "circuit_breaker")

    service.check_trading_permissions(account)
    assert store.get(f"halt:{account.key}")["active"]
    alerts = store.list("notifications", 20, account.key)
    assert sum(row["payload"].get("event") == "trading_permission_restored" for row in alerts) == 1


def test_manual_position_is_reported_but_never_marked_as_bot_owned(settings, store, broker):
    from datetime import timedelta

    from app.domain import Direction, Position, utcnow

    account = broker.account()
    service = DemoService(settings, store, broker)
    position = Position(ticket=99, symbol="XAUUSD", direction=Direction.SHORT, volume=.01, entry=2000,
                        stop_loss=2010, take_profit=1980, profit=-2.0, magic=0,
                        timestamp=utcnow(), comment="")
    start = utcnow()
    service.position_updates([position], set(), account, start)
    notices = store.list("notifications", 20, account.key)
    assert "Manual or unrecognized" in notices[0]["payload"]["message"]
    assert "will not manage it" in notices[0]["payload"]["message"]

    service.position_updates([position], set(), account, start + timedelta(minutes=15))
    assert any("15-minute update" in row["payload"]["message"]
               for row in store.list("notifications", 20, account.key))

    service.position_updates([], set(), account, start + timedelta(minutes=16))
    assert any(row["payload"].get("event") == "position_closed"
               for row in store.list("notifications", 20, account.key))

def test_monitoring_service_persists_real_adapter_state_disarmed(settings, store, broker):
    # Test fixtures are explicit and injected; runtime never substitutes this broker.
    now = utcnow()
    for tf, freq, count in (("M1", "min", 1000), ("M15", "15min", 1000)):
        index = pd.date_range(end=pd.Timestamp(now).floor(freq), periods=count, freq=freq)
        broker.frames[tf] = pd.DataFrame({"open": 2000., "high": 2001., "low": 1999., "close": 2000.,
                                         "tick_volume": 1, "spread": 20, "real_volume": 0}, index=index)
    service = DemoService(settings, store, broker)
    service.cycle()
    assert store.get("service")["state"] == "RUNNING"
    assert store.get("service")["strategies"] == ["ict_sweep_mss_fvg", "support_resistance_bounce"]
    assert store.get("active_account")["equity"] == 10000
    assert broker.send_count == 0
    assert not store.load_candles("Exness-Demo", "XAUUSD", "M1").empty


def test_graceful_shutdown_revokes_permission(settings, store, broker):
    service = DemoService(settings, store, broker)
    store.set(f"armed:{broker.account().key}", {"active": True})
    service.stop_event.set()
    service.run()
    assert store.get("service")["state"] == "STOPPED"
    assert not store.get(f"armed:{service.account_key}")["active"]
