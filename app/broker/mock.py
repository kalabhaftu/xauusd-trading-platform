"""Deterministic broker for software tests only. Never selected by runtime configuration."""
from datetime import datetime

import pandas as pd

from app.domain import Account, Direction, ExecutionResult, Position, Quote, SymbolSpec, utcnow


class MockBroker:
    def __init__(self, settings, behavior="filled"):
        self.settings, self.behavior = settings, behavior
        self.connected = True
        self.send_count = 0
        self._positions = []
        self._orders = []
        self._deals = []
        self._history_orders = []
        self._account = Account(login=settings.account_login or 12345, server=settings.account_server or "Exness-Demo",
                                company="Exness", environment="DEMO", currency="USD", balance=10000,
                                equity=10000, margin=0, free_margin=10000, trade_allowed=True, expert_allowed=True)
        self._spec = SymbolSpec(symbol="XAUUSD", point=.01, digits=2, tick_size=.01, tick_value=1,
                                contract_size=100, volume_min=.01, volume_max=10, volume_step=.01,
                                stops_level=10, filling_mode=1, price_basis="bid")
        self._quote = Quote(symbol="XAUUSD", bid=2000, ask=2000.2, timestamp=utcnow())
        self.frames = {}

    def connect(self):
        self.connected = True

    def shutdown(self):
        self.connected = False

    def account(self):
        if not self.connected:
            raise RuntimeError("Synthetic test disconnection")
        return self._account

    def discover(self):
        return "XAUUSD"

    def spec(self, symbol):
        return self._spec

    def quote(self, symbol):
        return self._quote

    def positions(self):
        return list(self._positions)

    def orders(self):
        return list(self._orders)

    def history(self, start: datetime, end: datetime):
        return list(self._deals)

    def order_history(self, start, end):
        return list(self._history_orders)

    def candles(self, symbol, timeframe, start, end):
        frame = self.frames.get(timeframe, pd.DataFrame())
        return frame.loc[(frame.index >= start) & (frame.index <= end)] if not frame.empty else frame

    def profit(self, symbol, direction, volume, entry, exit):
        sign = 1 if direction == Direction.LONG else -1
        return sign * (exit - entry) * self._spec.contract_size * volume

    def margin(self, symbol, direction, volume, entry):
        return entry * self._spec.contract_size * volume / 100

    def request(self, symbol, direction, volume, price, sl, tp, comment, position=None):
        return {"symbol": symbol, "direction": direction.value, "volume": volume, "price": price,
                "sl": sl, "tp": tp, "comment": comment, "position": position, "magic": self.settings.magic}

    def check(self, request):
        return self.behavior != "preflight_reject", "Synthetic preflight outcome"

    def send(self, request):
        self.send_count += 1
        if self.behavior == "disconnect":
            raise RuntimeError("Synthetic timeout")
        if self.behavior == "reject":
            return ExecutionResult(state="REJECTED", retcode=10006, message="Synthetic rejection")
        pid = request.get("position") or self.send_count + 1000
        if request.get("position"):
            self._positions = [p for p in self._positions if p.ticket != pid]
            entry_kind = 1
        else:
            volume = request["volume"] / 2 if self.behavior == "partial" else request["volume"]
            self._positions.append(Position(ticket=pid, symbol=request["symbol"],
                                            direction=request["direction"], volume=volume,
                                            entry=request["price"], stop_loss=request["sl"],
                                            take_profit=request["tp"], profit=0, magic=self.settings.magic,
                                            timestamp=utcnow(), comment=request["comment"]))
            entry_kind = 0
        self._deals.append({"ticket": self.send_count, "order": self.send_count, "position_id": pid,
                            "symbol": request["symbol"], "magic": self.settings.magic,
                            "comment": request["comment"], "entry": entry_kind,
                            "volume": request["volume"] / 2 if self.behavior == "partial" else request["volume"],
                            "price": request["price"], "time": int(utcnow().timestamp()), "type": 0,
                            "profit": 0, "commission": 0, "swap": 0, "fee": 0})
        if self.behavior == "unknown_after_fill":
            return ExecutionResult(state="UNKNOWN", message="Synthetic lost response after accepted fill")
        return ExecutionResult(state="PARTIAL" if self.behavior == "partial" else "FILLED", retcode=10009,
                               order=self.send_count, deal=self.send_count,
                               volume=request["volume"] / 2 if self.behavior == "partial" else request["volume"],
                               price=request["price"])
