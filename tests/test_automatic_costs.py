from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.broker.mt5_client import MT5Broker
from app.domain import Direction, Quote, SymbolSpec


class FakeCostAdapter:
    mt5 = SimpleNamespace(
        SYMBOL_SWAP_MODE_DISABLED=0,
        SYMBOL_SWAP_MODE_POINTS=1,
        SYMBOL_SWAP_MODE_CURRENCY_MARGIN=3,
        SYMBOL_SWAP_MODE_CURRENCY_DEPOSIT=4,
        SYMBOL_SWAP_MODE_CURRENCY_PROFIT=5,
    )

    def _required(self, value, _label):
        return value

    def spec(self, symbol):
        return SymbolSpec(symbol=symbol, point=.01, digits=2, tick_size=.01, tick_value=1,
                          contract_size=100, volume_min=.01, volume_max=10, volume_step=.01,
                          stops_level=0, currency_profit="USD", currency_margin="USD")

    def quote(self, symbol):
        return Quote(symbol=symbol, bid=2000, ask=2000.2, timestamp=datetime.now(UTC))

    def profit(self, symbol, direction, volume, entry, exit_price):
        assert direction == Direction.LONG and volume == 1
        return (exit_price - entry) * self.spec(symbol).contract_size


def test_auto_costs_convert_point_swaps_and_leave_commission_unverified():
    adapter = FakeCostAdapter()
    adapter.mt5.symbol_info = lambda _symbol: SimpleNamespace(
        swap_mode=adapter.mt5.SYMBOL_SWAP_MODE_POINTS, swap_long=-3, swap_short=1)

    result = MT5Broker.automatic_cost_profile(adapter, "XAUUSD", "USD")

    assert result["values"]["swap_long_per_lot_day"] == pytest.approx(-3)
    assert result["values"]["swap_short_per_lot_day"] == pytest.approx(1)
    assert result["swaps_verified"] is True
    assert result["commission_verified"] is False
    assert result["verified"] is False


def test_auto_costs_keep_unknown_swap_units_as_estimates():
    adapter = FakeCostAdapter()
    adapter.mt5.symbol_info = lambda _symbol: SimpleNamespace(swap_mode=99, swap_long=-3, swap_short=-2)

    result = MT5Broker.automatic_cost_profile(adapter, "XAUUSD", "USD")

    assert result["values"]["swap_long_per_lot_day"] == 0
    assert result["swaps_verified"] is False
    assert result["verified"] is False
