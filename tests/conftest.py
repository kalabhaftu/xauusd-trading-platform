from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from app.broker.mock import MockBroker
from app.config.settings import Settings
from app.database.storage import Store
from app.domain import Direction, Signal, digest, utcnow


@pytest.fixture
def settings():
    return Settings(_env_file=None, account_login=12345, account_server="Exness-Demo", mt5_path="test-terminal",
                    max_spread=1, max_slippage_points=20, commission_per_lot=0, slippage_price=.2,
                    swap_long_per_lot_day=0, swap_short_per_lot_day=0)


@pytest.fixture
def store(tmp_path):
    database = Store(f"sqlite:///{tmp_path / 'test.db'}")
    database.migrate()
    yield database
    database.engine.dispose()


@pytest.fixture
def broker(settings):
    return MockBroker(settings)


@pytest.fixture
def signal():
    now = utcnow()
    return Signal(symbol="XAUUSD", strategy="ict_sweep_mss_fvg", config_hash="test-config", direction=Direction.LONG,
                  timestamp=now - timedelta(seconds=1), entry=2000, stop_loss=1990, take_profit=2020,
                  rationale="Synthetic software fixture; not research evidence", regime="test", indicators={"atr": 5},
                  expires_at=now + timedelta(minutes=15))


@pytest.fixture
def book():
    return {"baselines_verified": True, "daily_loss": 0, "weekly_loss": 0, "drawdown": 0,
            "consecutive_losses": 0}


class OpenSchedule:
    def is_open(self, now):
        return True


@pytest.fixture
def schedule():
    return OpenSchedule()


@pytest.fixture
def permission(store, broker, signal):
    from app.service import permission_fingerprint
    store.set(f"armed:{broker.account().key}", {"active": True, "config_hash": signal.config_hash,
                                               "fingerprint": permission_fingerprint(broker.settings)})
    store.set(f"approval:{signal.config_hash}", {"approved": True, "source": broker.account().server,
                                                "symbol": signal.symbol,
                                                "cost": {"costs_verified": True},
                                                "risk_fraction": broker.settings.risk_per_trade,
                                                "spec_hash": digest(broker.spec(signal.symbol).model_dump(
                                                    exclude={"timestamp"}))})


@pytest.fixture
def synthetic_frames():
    # Deterministic synthetic prices exist only in credential-free test fixtures.
    rng = np.random.default_rng(42)
    index = pd.date_range("2023-01-01", periods=5000, freq="15min", tz="UTC")
    price = 2000 + np.cumsum(rng.normal(.02, 1, len(index)))
    frame = pd.DataFrame({"open": price, "high": price + 1, "low": price - 1,
                          "close": price + .2, "tick_volume": 100, "spread": 20, "real_volume": 0}, index=index)
    higher = frame.resample("1h").agg({"open": "first", "high": "max", "low": "min", "close": "last",
                                      "tick_volume": "sum", "spread": "last", "real_volume": "sum"})
    return {"M15": frame, "H1": higher}
