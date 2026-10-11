import ctypes
import importlib
import logging
import os
import signal as os_signal
import threading
from datetime import timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import TextIO

from app.broker.mt5_client import MT5Broker, verify_account
from app.config.settings import Settings
from app.data.sessions import SessionSchedule
from app.data.validation import TIMEFRAMES, closed, validate
from app.domain import Direction, digest, utcnow
from app.execution.engine import ExecutionEngine
from app.execution.reconciliation import owned_positions, reconcile
from app.monitoring.alerts import AlertWorker
from app.risk.circuit_breaker import CircuitBreaker
from app.risk.limits import EquityBook, consecutive_losses
from app.strategies.registry import build

logger = logging.getLogger(__name__)


class AccountMutex:
    def __init__(self, key: str):
        self.name = "Global\\XauDemo_" + digest(key)[:32]
        self.handle = None
        self.file: TextIO | None = None

    def __enter__(self):
        if os.name == "nt":
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
            kernel.CreateMutexW.restype = ctypes.c_void_p
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            self.kernel = kernel
            self.handle = kernel.CreateMutexW(None, True, self.name)
            if not self.handle:
                raise OSError("Cannot create account mutex")
            if ctypes.get_last_error() == 183:
                kernel.CloseHandle(self.handle)
                self.handle = None
                raise RuntimeError("Another engine instance controls this account")
        else:
            import tempfile
            fcntl = importlib.import_module("fcntl")
            self.file = open(os.path.join(tempfile.gettempdir(), digest(self.name) + ".lock"), "a")
            try:
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self.file.close()
                raise RuntimeError("Another engine instance controls this account") from None
        return self

    def __exit__(self, *_):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
        if self.file:
            self.file.close()


def normalize_signal(signal, spec):
    step = Decimal(str(spec.tick_size))
    rounding = ROUND_FLOOR if signal.direction == Direction.LONG else ROUND_CEILING
    levels = {key: float((Decimal(str(getattr(signal, key))) / step).to_integral_value(rounding=rounding) * step)
              for key in ("stop_loss", "take_profit")}
    return signal.model_copy(update=levels)


def permission_fingerprint(settings: Settings) -> str:
    return digest({k: v for k, v in settings.public().items()
                   if k not in {"report_dir", "retention_days", "poll_seconds", "session_file", "database_url"}})


class DemoService:
    def __init__(self, settings, store, broker=None):
        self.settings, self.store = settings, store
        self.broker = broker or MT5Broker(settings)
        self.stop_event = threading.Event()
        self.execution = ExecutionEngine(settings, store, self.broker)
        self.account_key = f"{settings.account_server}:{settings.account_login}"
        self.last_frames = None
        self.last_bar = None

    def event(self, event: str, message: str) -> None:
        self.store.record("service_events", {"event": event, "message": message}, self.account_key)
        self.store.record("notifications", {"event": event, "message": message}, self.account_key)
        if event.startswith("connection_"):
            self.store.record("connection_events", {"event": event, "message": message}, self.account_key)

    def run(self) -> None:
        self.settings.require_demo()
        with AccountMutex(self.account_key):
            alerts = AlertWorker(self.settings, self.store)
            alerts.start()
            for kind in (os_signal.SIGINT, os_signal.SIGTERM):
                os_signal.signal(kind, lambda *_: self.stop_event.set())
            self.event("startup", "Demo monitoring service started; entries require audited permission")
            connected = False
            error_count = 0
            try:
                while not self.stop_event.is_set():
                    try:
                        if not connected:
                            self.broker.connect()
                            connected = True
                            self.event("connection_recovery", "Verified demo terminal connected")
                        self.cycle()
                        error_count = 0
                    except Exception as exc:
                        error_count += 1
                        logger.exception("Demo cycle failed")
                        self.store.record("errors", {"type": type(exc).__name__, "message": str(exc)}, self.account_key)
                        self.store.set("service", {"state": "DISCONNECTED", "account": self.account_key,
                                                   "heartbeat": utcnow().isoformat(), "error": type(exc).__name__})
                        self.event("connection_loss", "Demo cycle failed; new entries suspended")
                        CircuitBreaker(self.store, self.account_key).trip("Broker or data verification failed")
                        self.broker.shutdown()
                        connected = False
                    self.stop_event.wait(min(60, self.settings.poll_seconds * 2 ** min(error_count, 3)))
            finally:
                self.store.set(f"armed:{self.account_key}", {"active": False})
                self.broker.shutdown()
                self.store.set("service", {"state": "STOPPED", "account": self.account_key,
                                           "heartbeat": utcnow().isoformat()})
                self.event("shutdown", "Graceful service shutdown; broker protective orders remain active")
                alerts.stop()

    def cycle(self) -> None:
        now = utcnow()
        account = self.broker.account()
        verify_account(account, self.settings)
        symbol = self.broker.discover()
        self.store.set("active_account", account.model_dump(mode="json"))
        self.store.set("active_symbol", symbol)
        self.store.set(f"spec:{account.server}:{symbol}", self.broker.spec(symbol))
        state = reconcile(self.broker, self.store, self.settings)
        exposure = sum(max(0, -self.broker.profit(p.symbol, p.direction, p.volume, p.entry, p.stop_loss))
                       for p in state.positions if p.stop_loss > 0)
        self.store.set(f"exposure:{account.key}", {"stop_risk_amount": exposure,
                                                   "fraction_of_equity": exposure / account.equity
                                                   if account.equity > 0 else None,
                                                   "unprotected_positions": sum(p.stop_loss <= 0 for p in state.positions),
                                                   "timestamp": now.isoformat()})
        history_start = self.store.get(f"history_start:{account.key}")
        if history_start is None:
            history_start = (now - timedelta(days=35)).isoformat()
            self.store.set(f"history_start:{account.key}", history_start)
        from datetime import datetime
        deals = self.broker.history(datetime.fromisoformat(history_start), now)
        for deal in deals:
            new = self.store.record("broker_deals", deal, account.key, f"{account.key}:{deal['ticket']}")
            if new and deal.get("magic") == self.settings.magic:
                if deal.get("entry") == 0:
                    self.event("trade_opened", f"{deal.get('symbol')} • {deal.get('volume')} lots • broker-confirmed entry")
                elif deal.get("entry") in (1, 3):
                    reason = {4: "stop_loss", 5: "take_profit"}.get(int(deal.get("reason") or -1), "trade_exit")
                    self.event(reason, f"{deal.get('symbol')} • exit deal net P&L "
                               f"{sum(float(deal.get(k, 0)) for k in ('profit', 'commission', 'swap', 'fee')):.2f} "
                               f"{account.currency}")
        self.store.record("equity_snapshots", account, account.key)
        book = EquityBook(self.store, account.key).observe(account, deals, now)
        loss_reset = self.store.get(f"loss_reset:{account.key}", 0)
        recent_deals = [d for d in deals if d.get("time", 0) > loss_reset]
        book["consecutive_losses"] = consecutive_losses(recent_deals, self.settings.magic)
        book["last_exit_time"] = max((d.get("time", 0) for d in recent_deals
                                      if d.get("entry") in (1, 2, 3)), default=0)
        self.store.set(f"risk:{account.key}", book)
        breaker = CircuitBreaker(self.store, account.key)
        for field, limit in (("daily_loss", self.settings.daily_loss_limit),
                             ("weekly_loss", self.settings.weekly_loss_limit),
                             ("drawdown", self.settings.drawdown_limit),
                             ("consecutive_losses", self.settings.consecutive_loss_limit)):
            if book.get(field, 0) >= limit:
                breaker.trip(f"{field} threshold reached")
        self.process_commands(state, book)
        ownership = owned_positions(self.broker, self.store, self.settings, state.positions)
        for position in state.positions:
            if position.ticket in ownership and position.stop_loss <= 0:
                self.execution.close_position(position, emergency=True)
        self.time_exits(state.positions, deals, now)
        # Monitoring remains useful without a fresh session export; execution requires it.
        parameters = self.store.get(f"strategy_parameters:{self.settings.active_strategy}", {})
        strategy = build(self.settings.active_strategy, parameters)
        entry_timeframe, trend_timeframe = strategy.parameters.entry_timeframe, strategy.parameters.trend_timeframe
        current_bar = (int(now.timestamp()) // (TIMEFRAMES[entry_timeframe] * 60), strategy.config_hash)
        if self.last_frames is None or current_bar != self.last_bar:
            frames = {}
            for timeframe, days in ((entry_timeframe, 20), (trend_timeframe, 60)):
                frame = self.broker.candles(symbol, timeframe, now - timedelta(days=days), now)
                quality = validate(frame, timeframe)
                if not quality["valid"]:
                    raise ValueError("Invalid or unavailable broker candles")
                frames[timeframe] = closed(frame, timeframe, now)
                self.store.save_candles(frames[timeframe], account.server, symbol, timeframe,
                                       self.broker.spec(symbol).price_basis)
            self.last_frames, self.last_bar = frames, current_bar
        frames = self.last_frames
        quote = self.broker.quote(symbol)
        self.store.set("latest_quote", quote)
        if not 0 <= (now - quote.timestamp).total_seconds() <= self.settings.quote_max_age_seconds:
            breaker.trip("Stale/future market quote")
        if quote.spread < 0 or (self.settings.max_spread is not None and quote.spread > self.settings.max_spread):
            breaker.trip("Invalid or excessive spread")
        last = frames[entry_timeframe].index[-1] + timedelta(minutes=TIMEFRAMES[entry_timeframe])
        cursor_key = f"cursor:{account.key}:{strategy.config_hash}:{symbol}"
        if self.store.get(cursor_key) != last.isoformat():
            signals = strategy.signals(frames, symbol)
            current = [s for s in signals if s.timestamp == last.to_pydatetime()]
            armed = self.store.get(f"armed:{account.key}", {"active": False})
            if armed.get("fingerprint") != permission_fingerprint(self.settings):
                self.store.set(f"armed:{account.key}", {"active": False})
                armed = {"active": False}
            for proposed in current:
                proposed = normalize_signal(proposed, self.broker.spec(symbol))
                self.store.record("signals", proposed, account.key, proposed.key(account.key))
                self.store.set("latest_signal", proposed)
                if armed.get("active") and not breaker.current()["active"]:
                    schedule = SessionSchedule(self.settings.session_file, symbol, account.server)
                    self.execution.execute(proposed, now, book, schedule)
            self.store.set(cursor_key, last.isoformat())
        status = "HALTED" if breaker.current()["active"] else "RUNNING"
        self.store.set("service", {"state": status, "account": account.key, "strategy": strategy.name,
                                   "config_hash": strategy.config_hash, "heartbeat": now.isoformat(),
                                   "data_refresh": now.isoformat(), "pid": os.getpid()})
        day = now.date().isoformat()
        if self.store.get(f"daily_summary:{account.key}") != day:
            self.event("daily_summary", f"UTC daily P&L {book['daily_pnl']:.2f}; weekly {book['weekly_pnl']:.2f}")
            self.store.set(f"daily_summary:{account.key}", day)

    def process_commands(self, state, book) -> None:
        for command in self.store.pending_commands():
            try:
                payload, kind = command["payload"], command["kind"]
                payload = dict(payload)
                target_account = payload.pop("account", None)
                if target_account and target_account != self.account_key:
                    raise ValueError("Control command targets a different account")
                breaker = CircuitBreaker(self.store, self.account_key)
                if kind == "stop":
                    self.stop_event.set()
                    self.store.set(f"armed:{self.account_key}", {"active": False})
                elif kind == "halt":
                    breaker.trip("Operator emergency halt")
                elif kind == "reset-halt":
                    quote = self.broker.quote(self.store.get("active_symbol"))
                    fresh = 0 <= (utcnow() - quote.timestamp).total_seconds() <= self.settings.quote_max_age_seconds
                    safe = state.safe and book.get("baselines_verified", False) and fresh \
                        and book["daily_loss"] < self.settings.daily_loss_limit \
                        and book["weekly_loss"] < self.settings.weekly_loss_limit \
                        and book["drawdown"] < self.settings.drawdown_limit \
                        and self.settings.max_spread is not None and quote.spread <= self.settings.max_spread
                    cooldown_passed = utcnow().timestamp() - book.get("last_exit_time", 0) \
                        >= self.settings.loss_cooldown_minutes * 60
                    if book["consecutive_losses"] >= self.settings.consecutive_loss_limit:
                        safe = safe and cooldown_passed
                    breaker.reset(safe)
                    if book["consecutive_losses"] >= self.settings.consecutive_loss_limit:
                        self.store.set(f"loss_reset:{self.account_key}", utcnow().timestamp())
                elif kind == "configure":
                    allowed = {"risk_per_trade", "daily_loss_limit", "weekly_loss_limit", "drawdown_limit",
                               "max_spread", "max_slippage_points", "poll_seconds", "retention_days"}
                    if set(payload) - allowed:
                        raise ValueError("Settings require restart or are not editable through dashboard")
                    values = self.settings.model_dump()
                    values.update(payload)
                    self.settings = Settings(**values)
                    self.execution.settings = self.settings
                    self.execution.risk.settings = self.settings
                    self.broker.settings = self.settings
                    self.store.set("settings_override", {**self.store.get("settings_override", {}), **payload})
                    self.store.set(f"armed:{self.account_key}", {"active": False})
                elif kind == "configure-broker":
                    allowed = {"mt5_path", "account_login", "account_server", "symbol"}
                    if set(payload) - allowed:
                        raise ValueError("Only nonsecret demo broker fields can be changed here")
                    candidate = Settings(**{**self.settings.model_dump(), **payload})
                    candidate.require_demo()
                    self.store.set("broker_override", payload)
                    self.store.set(f"armed:{self.account_key}", {"active": False})
                    self.stop_event.set()
                elif kind == "select-strategy":
                    strategy = build(payload["name"], payload.get("parameters"))
                    self.store.set(f"strategy_parameters:{strategy.name}", strategy.parameters.model_dump())
                    self.store.record("strategy_configurations", {"name": strategy.name,
                                      "hash": strategy.config_hash, "parameters": strategy.parameters.model_dump()})
                    self.settings = Settings(**dict(self.settings.model_dump(), active_strategy=strategy.name))
                    self.execution.settings = self.settings
                    self.execution.risk.settings = self.settings
                    self.broker.settings = self.settings
                    self.store.set("selected_strategy", strategy.name)
                    self.store.set(f"armed:{self.account_key}", {"active": False})
                elif kind == "close-position":
                    if target_account != self.account_key:
                        raise ValueError("Position closure must identify the intended account")
                    if payload.get("confirmed") is not True:
                        raise ValueError("Position closure requires explicit confirmation")
                    current = self.broker.positions()
                    position = next((p for p in current if p.ticket == int(payload["ticket"])), None)
                    if position is None:
                        raise ValueError("Position no longer exists")
                    self.execution.close_position(position)
                self.store.finish_command(command["id"], True, {"request": payload})
            except Exception as exc:
                self.store.finish_command(command["id"], False, {"error": str(exc)})

    def time_exits(self, positions, deals, now):
        from datetime import datetime
        for intent in self.store.list("order_intents", 1000, self.account_key):
            signal = intent["payload"].get("signal", {})
            exit_at = signal.get("exit_at")
            if not exit_at or datetime.fromisoformat(exit_at) > now:
                continue
            pids = {d["position_id"] for d in deals if d.get("comment") == intent["payload"]["comment"]}
            for position in positions:
                if position.ticket in pids:
                    self.execution.close_position(position)
