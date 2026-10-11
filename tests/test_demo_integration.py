import os
from datetime import timedelta

import pytest

from app.broker.mt5_client import MT5Broker, verify_account
from app.config.settings import Settings
from app.domain import utcnow

pytestmark = pytest.mark.demo


@pytest.mark.skipif(os.environ.get("XAU_RUN_DEMO_CHECKS") != "1", reason="Explicit read-only demo check opt-in required")
def test_verified_demo_connectivity_no_orders():
    settings = Settings()
    broker = MT5Broker(settings)
    try:
        broker.connect()
        verify_account(broker.account(), settings)
        symbol = broker.discover()
        assert broker.spec(symbol).contract_size > 0
        assert broker.quote(symbol).bid > 0
        now = utcnow()
        assert not broker.candles(symbol, "M15", now - timedelta(days=3), now).empty
        broker.positions()
        broker.orders()
    finally:
        broker.shutdown()
