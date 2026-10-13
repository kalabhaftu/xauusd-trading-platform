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
    utcnow,
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
        # Omitting path lets the official package discover and start the installed terminal.
        # Passing path=None can be rejected by the native extension as invalid parameters.
        initialized = (self.mt5.initialize(self.settings.mt5_path, **kwargs)
                       if self.settings.mt5_path else self.mt5.initialize(**kwargs))
        if not initialized:
            error = self.mt5.last_error()
            code = error[0] if error else "unknown"
            if code == -2:
                raise BrokerError(
                    "MT5 rejected its connection settings (-2: invalid parameters). Check that the account "
                    "number is numeric, the server name exactly matches MT5, and any selected app file is "
                    "the real terminal64.exe. Clear the app location to let MT5 find it automatically."
                )
            description = error[1] if error and len(error) > 1 else ""
            detail = f": {description}" if description else ""
            raise BrokerError(f"MT5 initialization failed ({code}){detail}")
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

    def automatic_cost_profile(self, symbol: str, account_currency: str) -> dict:
        """Read swap values MT5 exposes; commission remains explicitly unverified."""
        info = self._required(self.mt5.symbol_info(symbol), "symbol cost specification")
        spec = self.spec(symbol)
        quote = self.quote(symbol)
        try:
            mode = int(info.swap_mode)
            long_swap = float(info.swap_long)
            short_swap = float(info.swap_short)
        except (TypeError, ValueError):
            mode, long_swap, short_swap = -1, 0.0, 0.0
        disabled = int(getattr(self.mt5, "SYMBOL_SWAP_MODE_DISABLED", 0))
        points = int(getattr(self.mt5, "SYMBOL_SWAP_MODE_POINTS", 1))
        deposit = int(getattr(self.mt5, "SYMBOL_SWAP_MODE_CURRENCY_DEPOSIT", 4))
        profit_currency = int(getattr(self.mt5, "SYMBOL_SWAP_MODE_CURRENCY_PROFIT", -1))
        margin_currency = int(getattr(self.mt5, "SYMBOL_SWAP_MODE_CURRENCY_MARGIN", 3))
        if mode == disabled:
            long_value, short_value, swaps_verified = 0.0, 0.0, True
            swap_source = "MT5 reports swaps disabled"
        elif mode == points:
            mid = (quote.bid + quote.ask) / 2
            point_value = abs(self.profit(symbol, Direction.LONG, 1.0, mid, mid + spec.point))
            long_value, short_value = long_swap * point_value, short_swap * point_value
            swaps_verified = point_value > 0
            swap_source = "MT5 point-based swap converted with broker profit calculation"
        elif mode == deposit:
            long_value, short_value = long_swap, short_swap
            swaps_verified = bool(account_currency)
            swap_source = "MT5 deposit-currency swap values"
        elif mode == profit_currency and spec.currency_profit == account_currency:
            long_value, short_value = long_swap, short_swap
            swaps_verified = True
            swap_source = "MT5 profit-currency swap values"
        elif mode == margin_currency and spec.currency_margin == account_currency:
            long_value, short_value = long_swap, short_swap
            swaps_verified = True
            swap_source = "MT5 margin-currency swap values"
        else:
            long_value, short_value, swaps_verified = 0.0, 0.0, False
            swap_source = "MT5 swap unit requires manual review"
        return {
            "values": {"commission_per_lot": 0.0, "slippage_price": 0.20,
                       "swap_long_per_lot_day": long_value, "swap_short_per_lot_day": short_value},
            "verified": False,
            "commission_verified": False,
            "commission_source": "MT5 Python API does not provide the account commission schedule",
            "swaps_verified": swaps_verified,
            "swap_source": swap_source,
            "observed_spread": quote.spread,
            "point": spec.point,
            "checked_at": utcnow().isoformat(),
        }

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
