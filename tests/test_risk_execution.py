from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.broker.mt5_client import verify_account
from app.database.models import intents
from app.domain import Direction, Reconciliation, utcnow
from app.execution.engine import ExecutionEngine
from app.execution.reconciliation import reconcile
from app.risk.circuit_breaker import CircuitBreaker
from app.risk.limits import EquityBook, consecutive_losses
from app.risk.manager import RiskManager
from app.risk.sizing import round_volume_down, size
from app.service import AccountMutex, normalize_signal


def test_broker_contract_and_floor(broker):
    spec = broker.spec("XAUUSD")
    volume, risk = size(broker, spec, Direction.LONG, 2000, 1990, 25, 0)
    assert volume == .02 and risk == 20
    assert round_volume_down(.019999, spec) == .01
    different = spec.model_copy(update={"contract_size": 10, "volume_step": .1, "volume_min": .1})
    broker._spec = different
    volume, risk = size(broker, different, Direction.LONG, 2000, 1990, 25, 0)
    assert volume == .2 and risk == 20
    with pytest.raises(ValueError, match="Minimum"):
        size(broker, different, Direction.LONG, 2000, 1990, 1, 0)


@pytest.mark.parametrize("change", [{"login": 99}, {"server": "Other"}, {"environment": "LIVE"},
                                     {"company": "Other"}, {"expert_allowed": False}])
def test_account_identity(settings, broker, change):
    with pytest.raises(RuntimeError):
        verify_account(broker.account().model_copy(update=change), settings)


def test_risk_defaults_disarmed(settings, store, broker, signal, book, schedule):
    state = Reconciliation(safe=True, reasons=[], positions=[])
    decision = RiskManager(settings, store, broker).evaluate(signal, utcnow(), book, state, schedule)
    assert not decision.approved


def test_risk_approval_and_daily_halt(settings, store, broker, signal, book, schedule, permission):
    manager = RiskManager(settings, store, broker)
    state = Reconciliation(safe=True, reasons=[], positions=[])
    assert manager.evaluate(signal, utcnow(), book, state, schedule).approved
    book["daily_loss"] = .01
    assert not manager.evaluate(signal, utcnow(), book, state, schedule).approved
    assert CircuitBreaker(store, broker.account().key).current()["active"]


@pytest.mark.parametrize("fault", ["stale", "spread", "stop", "margin", "baseline", "minimum_lot"])
def test_unsafe_orders_rejected(settings, store, broker, signal, book, schedule, permission, fault):
    if fault == "stale":
        broker._quote = broker._quote.model_copy(update={"timestamp": utcnow() - timedelta(seconds=60)})
    elif fault == "spread":
        broker._quote = broker._quote.model_copy(update={"ask": 2002})
    elif fault == "stop":
        signal = signal.model_copy(update={"stop_loss": 2001})
    elif fault == "margin":
        broker._account = broker._account.model_copy(update={"free_margin": .01})
    elif fault == "baseline":
        book["baselines_verified"] = False
    elif fault == "minimum_lot":
        broker._account = broker._account.model_copy(update={"equity": 10})
    decision = RiskManager(settings, store, broker).evaluate(
        signal, utcnow(), book, Reconciliation(safe=True, reasons=[], positions=[]), schedule)
    assert not decision.approved
    assert broker.send_count == 0


def test_execute_fill_and_duplicate(settings, store, broker, signal, book, schedule, permission):
    engine = ExecutionEngine(settings, store, broker)
    assert engine.execute(signal, utcnow(), book, schedule).state == "FILLED"
    assert engine.execute(signal, utcnow(), book, schedule).state == "REJECTED"
    assert broker.send_count == 1
    assert broker.positions()[0].stop_loss > 0


@pytest.mark.parametrize("behavior,expected", [("reject", "REJECTED"), ("preflight_reject", "REJECTED"),
                                               ("partial", "PARTIAL"), ("disconnect", "UNKNOWN"),
                                               ("unknown_after_fill", "UNKNOWN")])
def test_faults_never_blindly_resend(settings, store, broker, signal, book, schedule, permission, behavior, expected):
    broker.behavior = behavior
    engine = ExecutionEngine(settings, store, broker)
    assert engine.execute(signal, utcnow(), book, schedule).state == expected
    # Simulated process restart: construct a new execution engine over the same persistence.
    restarted = ExecutionEngine(settings, store, broker)
    assert restarted.execute(signal, utcnow(), book, schedule).state == "REJECTED"
    assert broker.send_count <= 1
    if behavior in ("partial", "disconnect", "unknown_after_fill"):
        assert CircuitBreaker(store, broker.account().key).current()["active"]
    if behavior == "unknown_after_fill":
        assert reconcile(broker, store, settings).safe
        with store.engine.connect() as conn:
            assert conn.execute(select(intents.c.state)).scalar() == "FILLED"


def test_missing_stop_and_external_position_halt(settings, store, broker, signal, book, schedule, permission):
    ExecutionEngine(settings, store, broker).execute(signal, utcnow(), book, schedule)
    broker._positions[0] = broker._positions[0].model_copy(update={"stop_loss": 0, "magic": 999})
    assert not reconcile(broker, store, settings).safe
    assert CircuitBreaker(store, broker.account().key).current()["active"]


def test_midperiod_baseline_unverified_and_cash_adjustment(store, broker):
    book = EquityBook(store, broker.account().key)
    now = datetime(2026, 10, 12, 12, tzinfo=UTC)
    assert not book.observe(broker.account(), [], now)["baselines_verified"]
    lower = broker.account().model_copy(update={"equity": 9900})
    observed = book.observe(lower, [], now + timedelta(seconds=5))
    assert observed["daily_loss"] == pytest.approx(.01)
    funded = lower.model_copy(update={"equity": 10900})
    observed = book.observe(funded, [{"ticket": 1, "type": 2, "profit": 1000}], now + timedelta(seconds=10))
    assert observed["daily_pnl"] == -100


def test_monitored_utc_week_reset(store, broker):
    book = EquityBook(store, broker.account().key)
    book.observe(broker.account(), [], datetime(2026, 10, 11, 23, 59, 58, tzinfo=UTC))
    observed = book.observe(broker.account(), [], datetime(2026, 10, 12, 0, 0, 3, tzinfo=UTC))
    assert observed["baselines_verified"]


def test_loss_streak_includes_entry_costs():
    deals = []
    for pid in range(1, 4):
        deals += [{"ticket": pid * 2, "position_id": pid, "magic": 7, "entry": 0, "commission": -2,
                   "time": pid * 10}, {"ticket": pid * 2 + 1, "position_id": pid, "magic": 7, "entry": 1,
                                       "profit": 1, "time": pid * 10 + 5}]
    assert consecutive_losses(deals, 7) == 3


def test_account_mutex_prevents_second_instance():
    with AccountMutex("SYNTHETIC_TEST:123"):
        with pytest.raises(RuntimeError):
            with AccountMutex("SYNTHETIC_TEST:123"):
                pass


def test_protective_price_normalization(signal, broker):
    changed = signal.model_copy(update={"stop_loss": 1989.999, "take_profit": 2020.009})
    normalized = normalize_signal(changed, broker.spec("XAUUSD"))
    assert normalized.stop_loss == 1989.99 and normalized.take_profit == 2020


def test_position_appearing_during_preflight_blocks_send(settings, store, broker, signal, book, schedule, permission):
    from app.domain import Position
    def preflight(request):
        broker._positions.append(Position(ticket=9, symbol="XAUUSD", direction=Direction.LONG, volume=.01,
                                          entry=2000, stop_loss=1990, take_profit=2020, profit=0, magic=999,
                                          timestamp=utcnow()))
        return True, "Synthetic preflight success"
    broker.check = preflight
    result = ExecutionEngine(settings, store, broker).execute(signal, utcnow(), book, schedule)
    assert result.state == "REJECTED" and broker.send_count == 0


def test_same_magic_without_persisted_ownership_is_not_closed(settings, store, broker):
    from app.domain import Position
    position = Position(ticket=88, symbol="XAUUSD", direction=Direction.LONG, volume=.01, entry=2000,
                        stop_loss=0, take_profit=2020, profit=0, magic=settings.magic, timestamp=utcnow())
    broker._positions = [position]
    assert not reconcile(broker, store, settings).safe
    with pytest.raises(ValueError, match="ownership"):
        ExecutionEngine(settings, store, broker).close_position(position, emergency=True)
    assert broker.send_count == 0


def test_broker_missing_target_triggers_persistent_halt(settings, store, broker, signal, book, schedule, permission):
    actual_send = broker.send
    def altered_fill(request):
        result = actual_send(request)
        broker._positions[0] = broker._positions[0].model_copy(update={"take_profit": 0})
        return result
    broker.send = altered_fill
    assert ExecutionEngine(settings, store, broker).execute(signal, utcnow(), book, schedule).state == "FILLED"
    assert CircuitBreaker(store, broker.account().key).current()["active"]


def test_unknown_closure_is_reconciled_without_resend(settings, store, broker, signal, book, schedule, permission):
    engine = ExecutionEngine(settings, store, broker)
    engine.execute(signal, utcnow(), book, schedule)
    position = broker.positions()[0]
    broker.behavior = "unknown_after_fill"
    assert engine.close_position(position).state == "UNKNOWN"
    assert broker.send_count == 2
    assert reconcile(broker, store, settings).safe
    assert not broker.positions()


def test_emergency_closure_rejections_are_bounded_across_restart(
        settings, store, broker, signal, book, schedule, permission):
    engine = ExecutionEngine(settings, store, broker)
    engine.execute(signal, utcnow(), book, schedule)
    position = broker.positions()[0].model_copy(update={"stop_loss": 0})
    broker._positions = [position]
    broker.behavior = "reject"
    reconcile(broker, store, settings)
    for _ in range(settings.emergency_close_attempts + 2):
        result = ExecutionEngine(settings, store, broker).close_position(position, emergency=True)
    assert "attempt limit" in result.message
    assert broker.send_count == 1 + settings.emergency_close_attempts
    assert broker.positions()[0].stop_loss == 0
    assert CircuitBreaker(store, broker.account().key).current()["active"]


def test_arming_refreshes_equity_before_authorization(settings, store, broker, monkeypatch):
    from argparse import Namespace

    from app.domain import digest
    from app.main import arm_demo
    from app.strategies.registry import build
    strategy = build(settings.active_strategy)
    cost = {"spread_price": settings.max_spread, "slippage_price": settings.slippage_price,
            "commission_per_lot": settings.commission_per_lot,
            "swap_long_per_lot_day": 0, "swap_short_per_lot_day": 0}
    store.set(f"approval:{strategy.config_hash}", {"approved": True, "source": broker.account().server,
               "symbol": "XAUUSD", "cost": cost, "cost_hash": digest(cost),
               "risk_fraction": settings.risk_per_trade,
               "spec_hash": digest(broker.spec("XAUUSD").model_dump(exclude={"timestamp"}))})
    now = utcnow()
    equity = EquityBook(store, broker.account().key)
    previous = equity.observe(broker.account(), [], now)
    previous.update(day_verified=True, week_verified=True)
    store.set(equity.key, previous)
    store.set(f"history_start:{broker.account().key}", (now - timedelta(days=35)).isoformat())
    broker._account = broker._account.model_copy(update={"equity": 9800})
    monkeypatch.setattr("app.broker.mt5_client.MT5Broker", lambda _: broker)
    with pytest.raises(ValueError, match="daily_loss"):
        arm_demo(settings, store, Namespace(confirm_account=settings.account_login, confirm_first_order=True))
    assert not store.get(f"armed:{broker._account.key}", {}).get("active")
    assert broker.send_count == 0


@pytest.mark.parametrize("boundary,expected,safe", [("PREPARED", "EXPIRED", True), ("SUBMITTING", "SUBMITTING", False)])
def test_crash_boundaries_never_resend_old_intents(settings, store, broker, signal, book, schedule, boundary, expected, safe):
    from sqlalchemy.dialects.sqlite import insert
    key = signal.key(broker.account().key)
    with store.engine.begin() as conn:
        conn.execute(insert(intents).values(key=key, account=broker.account().key, state=boundary,
                     timestamp=utcnow().isoformat(), payload={"comment": "xau:" + key[:24],
                     "signal": signal.model_dump(mode="json"), "decision": {"volume": .02}}))
    assert reconcile(broker, store, settings).safe is safe
    with store.engine.connect() as conn:
        assert conn.execute(select(intents.c.state)).scalar() == expected
    result = ExecutionEngine(settings, store, broker).execute(signal, utcnow(), book, schedule)
    assert result.state == "REJECTED" and broker.send_count == 0


def test_account_mutex_blocks_a_competing_process():
    import subprocess
    import sys
    key = "SYNTHETIC_PROCESS_TEST:123"
    program = "from app.service import AccountMutex\ntry:\n with AccountMutex('SYNTHETIC_PROCESS_TEST:123'):\n  raise SystemExit(1)\nexcept RuntimeError:\n raise SystemExit(42)\n"
    with AccountMutex(key):
        child = subprocess.run([sys.executable, "-c", program], capture_output=True, timeout=30)
    assert child.returncode == 42, child.stderr.decode()


def test_missed_utc_reset_does_not_invent_baseline(store, broker):
    equity = EquityBook(store, broker.account().key)
    equity.observe(broker.account(), [], datetime(2026, 10, 11, 23, 59, 55, tzinfo=UTC))
    resumed = equity.observe(broker.account(), [], datetime(2026, 10, 12, 0, 2, tzinfo=UTC))
    assert not resumed["day_verified"] and not resumed["week_verified"]


def test_sizing_budgets_slippage_on_stop_exit(settings, store, broker, signal, book, schedule, permission):
    # 10.50 risk budget covers minimum-lot entry slippage (10.40), but not exit slippage (10.60).
    broker._account = broker._account.model_copy(update={"equity": 4200})
    decision = RiskManager(settings, store, broker).evaluate(
        signal, utcnow(), book, Reconciliation(safe=True, reasons=[], positions=[]), schedule)
    assert not decision.approved and "Minimum" in decision.reason
