from datetime import UTC, datetime

import pandas as pd

from app.domain import Direction, Signal
from app.execution.signal_tracker import SignalOutcomeTracker


def test_signal_only_tracker_uses_next_bar_and_conservative_stop_first(settings, store, broker):
    timestamp = datetime(2026, 1, 5, 10, 0, tzinfo=UTC)
    signal = Signal(symbol="XAUUSD", strategy="ict_sweep_mss_fvg", config_hash="fixture-config",
                    direction=Direction.LONG, timestamp=timestamp, entry=2000, stop_loss=1990,
                    take_profit=2020, rationale="deterministic unit fixture", regime="fixture",
                    indicators={"atr": 5}, expires_at=datetime(2026, 1, 5, 10, 15, tzinfo=UTC))
    tracker = SignalOutcomeTracker(store, settings)
    tracker.start(signal, broker.account().key, broker.spec("XAUUSD"), 10_000)
    bars = pd.DataFrame([{"open": 2000., "high": 2021., "low": 1989., "close": 2001.,
                          "spread": 2, "tick_volume": 1, "real_volume": 0}],
                        index=pd.DatetimeIndex([pd.Timestamp(timestamp)]))

    tracker.update(broker.account().key, "XAUUSD", "M5", bars, broker.spec("XAUUSD"))

    record = store.get(f"signal_outcome:{broker.account().key}:{signal.key(broker.account().key)}")
    assert record["status"] == "CLOSED"
    assert record["exit_reason"] == "AMBIGUOUS_STOP_FIRST"
    assert record["volume_lots"] >= broker.spec("XAUUSD").volume_min
    assert record["realized_net_pnl"] < 0
    assert broker.send_count == 0


def test_signal_only_tracker_does_not_assume_missing_costs_are_zero(store, broker):
    from app.config.settings import Settings

    settings = Settings(_env_file=None, account_login=1, account_server="Exness-Demo", mt5_path="fixture")
    timestamp = datetime(2026, 1, 5, 10, 0, tzinfo=UTC)
    signal = Signal(symbol="XAUUSD", strategy="test", config_hash="fixture-config", direction=Direction.LONG,
                    timestamp=timestamp, entry=2000, stop_loss=1990, take_profit=2020,
                    rationale="deterministic unit fixture", regime="fixture", indicators={})
    SignalOutcomeTracker(store, settings).start(signal, broker.account().key, broker.spec("XAUUSD"), 10_000)
    record = store.get(f"signal_outcome:{broker.account().key}:{signal.key(broker.account().key)}")
    assert record["status"] == "COSTS_UNAVAILABLE"
