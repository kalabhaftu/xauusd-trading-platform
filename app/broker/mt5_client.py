import importlib
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from app.config.settings import Settings
from app.domain import (
    Account,
    Direction,
    ExecutionResult,
    ExecutionState,
    Position,
    Quote,
    SymbolSpec,
    TradingEnvironment,
)


class BrokerError(RuntimeError):
    pass


def verify_account(account: Account, settings: Settings, *, require_trading: bool = True) -> None:
    settings.require_execution_environment()
    if account.login != settings.account_login or account.server != settings.account_server:
        raise BrokerError("Configured account identity does not match the terminal")
    if settings.expected_company.lower() not in account.company.lower():
        raise BrokerError("Unexpected broker company")
    if account.environment != settings.mode:
        raise BrokerError(f"Configured {settings.mode} mode does not match connected account environment")
    if require_trading and (not account.trade_allowed or not account.expert_allowed):
        raise BrokerError("Account trading or external API permission is disabled")


class MT5Broker:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.mt5: Any = None  # Official compiled extension exposes namedtuples and untyped constants.

    def connect(self) -> None:
        self.settings.require_execution_environment()
        self.mt5 = importlib.import_module("MetaTrader5")
        kwargs = {"login": self.settings.account_login, "server": self.settings.account_server,
                  "timeout": 10000}
        if self.settings.account_password:
            kwargs["password"] = self.settings.account_password.get_secret_value()
        if not self.mt5.initialize(path=self.settings.mt5_path, **kwargs):
            raise BrokerError(f"MT5 initialization failed ({self.mt5.last_error()[0]})")
        try:
            # Initialization is also used by doctor and historical research. Those paths only
            # read account/market data and must remain available while terminal trading is off.
            verify_account(self.account(), self.settings, require_trading=False)
        except Exception:
            self.shutdown()
            raise

    def shutdown(self) -> None:
        if self.mt5:
            self.mt5.shutdown()

    def _required(self, value, label: str):
        if value is None:
            raise BrokerError(f"MT5 {label} unavailable ({self.mt5.last_error()[0]})")
        return value

    def account(self) -> Account:
        info = self._required(self.mt5.account_info(), "account")
        terminal = self._required(self.mt5.terminal_info(), "terminal")
        if not terminal.connected:
            raise BrokerError("MT5 terminal disconnected")
        env: dict[int, TradingEnvironment] = {self.mt5.ACCOUNT_TRADE_MODE_DEMO: "DEMO",
                                             self.mt5.ACCOUNT_TRADE_MODE_REAL: "LIVE"}
        return Account(login=info.login, server=info.server, company=info.company,
                       environment=env.get(info.trade_mode, "CONTEST"), currency=info.currency,
                       balance=info.balance, equity=info.equity, margin=info.margin,
                       free_margin=info.margin_free, trade_allowed=bool(info.trade_allowed),
                       expert_allowed=bool(info.trade_expert and terminal.trade_allowed
                                           and not terminal.tradeapi_disabled),
                       terminal_trade_allowed=bool(terminal.trade_allowed),
                       external_api_allowed=not bool(terminal.tradeapi_disabled))

    def discover(self) -> str:
        symbols = self._required(self.mt5.symbols_get(), "symbols")
        if self.settings.symbol:
            candidates = [s.name for s in symbols if s.name == self.settings.symbol]
        else:
            candidates = [s.name for s in symbols if s.name.upper().startswith("XAUUSD")]
        if len(candidates) != 1:
            raise BrokerError(f"Configure an explicit gold symbol; candidates: {candidates}")
        symbol = candidates[0]
        if not self.mt5.symbol_select(symbol, True):
            raise BrokerError("Cannot enable configured symbol")
        return symbol

    def spec(self, symbol: str) -> SymbolSpec:
        info = self._required(self.mt5.symbol_info(symbol), "symbol specification")
        return SymbolSpec(symbol=symbol, point=info.point, digits=info.digits,
                          tick_size=info.trade_tick_size, tick_value=info.trade_tick_value,
                          contract_size=info.trade_contract_size, volume_min=info.volume_min,
                          volume_max=info.volume_max, volume_step=info.volume_step,
                          stops_level=info.trade_stops_level, freeze_level=info.trade_freeze_level,
                          filling_mode=info.filling_mode, trade_mode=info.trade_mode,
                          currency_profit=info.currency_profit, currency_margin=info.currency_margin,
                          price_basis="bid" if info.chart_mode == 0 else "last")

    def quote(self, symbol: str) -> Quote:
        info = self._required(self.mt5.symbol_info_tick(symbol), "quote")
        return Quote(symbol=symbol, bid=info.bid, ask=info.ask,
                     timestamp=datetime.fromtimestamp(info.time_msc / 1000, UTC))

    def candles(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> pd.DataFrame:
        rates = self._required(self.mt5.copy_rates_range(symbol, getattr(self.mt5, f"TIMEFRAME_{timeframe}"),
                                                       start.astimezone(UTC), end.astimezone(UTC)), "history")
        frame = pd.DataFrame(rates)
        if not frame.empty:
            frame.index = pd.DatetimeIndex(pd.to_datetime(frame.pop("time"), unit="s", utc=True))
        return frame

    def ticks(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        ticks = self._required(self.mt5.copy_ticks_range(symbol, start, end, self.mt5.COPY_TICKS_ALL), "ticks")
        return pd.DataFrame(ticks)

    def positions(self) -> list[Position]:
        values = self._required(self.mt5.positions_get(), "positions")
        return [Position(ticket=p.ticket, symbol=p.symbol,
                         direction=Direction.LONG if p.type == self.mt5.POSITION_TYPE_BUY else Direction.SHORT,
                         volume=p.volume, entry=p.price_open, stop_loss=p.sl, take_profit=p.tp,
                         profit=p.profit, magic=p.magic, timestamp=datetime.fromtimestamp(p.time, UTC), comment=p.comment)
                for p in values]

    def orders(self) -> list[dict]:
        return [o._asdict() for o in self._required(self.mt5.orders_get(), "orders")]

    def history(self, start: datetime, end: datetime) -> list[dict]:
        deals = self._required(self.mt5.history_deals_get(start, end), "deal history")
        return [d._asdict() for d in deals]

    def order_history(self, start: datetime, end: datetime) -> list[dict]:
        return [o._asdict() for o in self._required(self.mt5.history_orders_get(start, end), "order history")]

    def profit(self, symbol: str, direction: Direction, volume: float, entry: float, exit: float) -> float:
        kind = self.mt5.ORDER_TYPE_BUY if direction == Direction.LONG else self.mt5.ORDER_TYPE_SELL
        return float(self._required(self.mt5.order_calc_profit(kind, symbol, volume, entry, exit), "profit calc"))

    def margin(self, symbol: str, direction: Direction, volume: float, entry: float) -> float:
        kind = self.mt5.ORDER_TYPE_BUY if direction == Direction.LONG else self.mt5.ORDER_TYPE_SELL
        return float(self._required(self.mt5.order_calc_margin(kind, symbol, volume, entry), "margin calc"))

    def request(self, symbol: str, direction: Direction, volume: float, price: float,
                sl: float, tp: float, comment: str, position: int | None = None) -> dict:
        spec = self.spec(symbol)
        # FOK avoids deliberate partial entry; IOC is supported if the symbol requires it.
        if spec.filling_mode & 1:
            filling = self.mt5.ORDER_FILLING_FOK
        elif spec.filling_mode & 2:
            filling = self.mt5.ORDER_FILLING_IOC
        else:
            raise BrokerError("No supported FOK/IOC filling policy; refuse optimistic RETURN fallback")
        request = dict(action=self.mt5.TRADE_ACTION_DEAL, symbol=symbol, volume=volume,
                       type=self.mt5.ORDER_TYPE_BUY if direction == Direction.LONG else self.mt5.ORDER_TYPE_SELL,
                       price=price, sl=sl, tp=tp, deviation=self.settings.max_slippage_points or 0,
                       magic=self.settings.magic, comment=comment[:31], type_time=self.mt5.ORDER_TIME_GTC,
                       type_filling=filling)
        if position is not None:
            request["position"] = position
        return request

    def check(self, request: dict) -> tuple[bool, str]:
        verify_account(self.account(), self.settings)
        result = self._required(self.mt5.order_check(request), "order check")
        return result.retcode == 0, f"MT5 preflight retcode={result.retcode}"

    def send(self, request: dict) -> ExecutionResult:
        # Defense in depth: cannot bypass identity checks through this adapter.
        verify_account(self.account(), self.settings)
        if not self.account().expert_allowed:
            raise BrokerError("External trading disabled")
        result = self.mt5.order_send(request)
        if result is None:
            return ExecutionResult(state="UNKNOWN", message="No MT5 execution response")
        states: dict[int, ExecutionState] = {self.mt5.TRADE_RETCODE_DONE: "FILLED", self.mt5.TRADE_RETCODE_DONE_PARTIAL: "PARTIAL",
                 self.mt5.TRADE_RETCODE_PLACED: "ACCEPTED", self.mt5.TRADE_RETCODE_TIMEOUT: "UNKNOWN",
                 self.mt5.TRADE_RETCODE_CONNECTION: "UNKNOWN"}
        state = states.get(result.retcode, "REJECTED")
        return ExecutionResult(state=state, retcode=result.retcode, order=result.order, deal=result.deal,
                               volume=result.volume, price=result.price, message=f"MT5 retcode={result.retcode}")
