import json
import logging
from datetime import timedelta

import pytest
from pydantic import ValidationError

from app.backtesting.engine import BacktestRiskLimits
from app.config.settings import Settings
from app.domain import utcnow
from app.main import configured, parser
from app.monitoring.health import service_health
from app.monitoring.logger import RedactingFormatter
from app.risk.circuit_breaker import CircuitBreaker
from app.strategies.base import Parameters


def test_settings_default_auto_mode_stays_disarmed_and_live_is_configurable():
    settings = Settings(_env_file=None)
    assert settings.mode == "DEMO" and not settings.entries_armed
    assert settings.auto_execution_mode == "AUTO_DEMO"
    assert settings.cost_mode == "AUTO"
    assert settings.daily_loss_limit == .05
    assert settings.consecutive_loss_limit == 5
    assert settings.max_spread == .5 and settings.max_slippage_points == 20 and settings.slippage_price == .2
    settings.require_costs()
    live = Settings(_env_file=None, mode="LIVE", account_login=123, account_server="Exness-Real",
                    mt5_path="C:/Program Files/MetaTrader 5/terminal64.exe")
    live.require_execution_environment()
    assert live.auto_execution_mode == "AUTO_LIVE"
    with pytest.raises(ValueError, match="requires DEMO"):
        live.require_demo()
    with pytest.raises(ValidationError):
        Settings(_env_file=None, entries_armed=True)
    flexible = Settings(_env_file=None, risk_per_trade=.75, hard_risk_max=1.0,
                        portfolio_risk_limit=1.0)
    assert flexible.risk_per_trade == .75
    with pytest.raises(ValidationError):
        Settings(_env_file=None, risk_per_trade=1.01, hard_risk_max=1.0)


def test_research_defaults_are_one_minute_with_independent_capital():
    args = parser().parse_args(["backtest"])
    assert args.timeframe == "M1"
    assert args.capital == 1000
    assert Parameters().entry_timeframe == "M1"
    assert BacktestRiskLimits().daily_loss_limit == .05
    assert BacktestRiskLimits().consecutive_loss_limit == 5


def test_legacy_half_percent_caps_are_migrated_to_user_configurable(store, monkeypatch):
    monkeypatch.setattr("app.main.Settings", lambda **values: Settings(
        _env_file=None, **values) if values else Settings(
            _env_file=None, hard_risk_max=.005, portfolio_risk_limit=.005))
    store.set("settings_override", {"hard_risk_max": .005, "portfolio_risk_limit": .005})
    settings = configured(store)
    assert settings.hard_risk_max == 1.0
    assert settings.portfolio_risk_limit == 1.0


def test_secrets_redacted():
    formatter = RedactingFormatter(["supersecret"])
    record = logging.LogRecord("test", logging.ERROR, "", 0, "password=supersecret token=abc", (), None)
    text = formatter.format(record)
    assert "supersecret" not in text and "abc" not in text
    assert json.loads(text)["level"] == "ERROR"


def test_migration_restart_and_persistent_halt(store):
    store.migrate()
    CircuitBreaker(store, "demo:123").trip("test halt")
    store.engine.dispose()
    assert CircuitBreaker(store, "demo:123").current()["active"]
    with pytest.raises(ValueError):
        CircuitBreaker(store, "demo:123").reset(False)
    CircuitBreaker(store, "demo:123").reset(True)
    assert not CircuitBreaker(store, "demo:123").current()["active"]


def test_commands_reject_arbitrary_execution(store):
    with pytest.raises(ValueError):
        store.enqueue("run-python", {"code": "malicious"})
    identifier = store.enqueue("halt")
    assert store.pending_commands()[0]["id"] == identifier
    store.finish_command(identifier, True, {})
    assert not store.pending_commands()


def test_stale_heartbeat(store):
    store.set("service", {"state": "RUNNING", "heartbeat": (utcnow() - timedelta(seconds=40)).isoformat()})
    assert service_health(store)["state"] == "STALE"


def test_persisted_audit_context_tracks_source_and_environment(store):
    store.record("signals", {"config_hash": "fixture-rules", "rationale": "synthetic fixture"},
                 "SYNTHETIC_TEST:123", "fixture-correlation")
    context = store.list("signals")[0]["payload"]["_meta"]
    assert context["environment"] == "DEMO" and context["source"] == "SYNTHETIC_TEST"
    assert context["configuration_hash"] == "fixture-rules"
    assert context["correlation_id"] == "fixture-correlation"
    store.record("backtest_results", {"status": "synthetic fixture"})
    assert store.list("backtest_results")[0]["payload"]["_meta"]["environment"] == "BACKTEST"
