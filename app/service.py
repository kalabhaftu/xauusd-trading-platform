import ctypes
import importlib
import logging
import os
import signal as os_signal
import threading
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import TextIO

import pandas as pd

from app.broker.mt5_client import MT5Broker, verify_account
from app.config.settings import Settings
from app.data.sessions import SessionSchedule
from app.data.validation import TIMEFRAMES, closed, validate
from app.domain import Direction, digest, utcnow
from app.execution.engine import ExecutionEngine
from app.execution.reconciliation import owned_positions, reconcile
from app.monitoring.alerts import AlertWorker
from app.risk.circuit_breaker import CircuitBreaker
from app.risk.limits import update_risk_snapshot
from app.strategies.registry import REGISTRY, build

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
        self.last_frames: dict[str, pd.DataFrame] = {}
        self.last_bars: dict[str, int] = {}
        self.cache_symbol: str | None = None

    def event(self, event: str, message: str) -> None:
        self.store.record("service_events", {"event": event, "message": message}, self.account_key)
        self.store.record("notifications", {"event": event, "message": message}, self.account_key)
        if event.startswith("connection_"):
            self.store.record("connection_events", {"event": event, "message": message}, self.account_key)

    def run(self) -> None:
        self.settings.require_execution_environment()
        with AccountMutex(self.account_key):
            alerts = AlertWorker(self.settings, self.store)
            alerts.start()
            for kind in (os_signal.SIGINT, os_signal.SIGTERM):
                os_signal.signal(kind, lambda *_: self.stop_event.set())
            watched = self.store.get("monitored_strategies", list(REGISTRY))
            self.event("startup", f"{self.settings.mode} monitor started; scanning {', '.join(watched)}; "
                       "broker orders require strategy approval and audited account permission")
            connected = False
            error_count = 0
            try:
                while not self.stop_event.is_set():
                    try:
                        if not connected:
                            self.broker.connect()
                            connected = True
                        self.cycle()
                        if error_count:
                            self.event("connection_recovery", "Broker and market-data checks recovered")
                        error_count = 0
                    except Exception as exc:
                        error_count += 1
                        logger.exception("Trading service cycle failed")
                        self.store.record("errors", {"type": type(exc).__name__, "message": str(exc)}, self.account_key)
                        self.store.set("service", {"state": "DISCONNECTED", "account": self.account_key,
                                                   "heartbeat": utcnow().isoformat(), "error": type(exc).__name__})
                        # Notify once for each outage, while retaining every failed cycle in the
                        # durable errors log. Repeating the same Telegram alert every poll is noise.
                        if error_count == 1:
                            self.event("connection_loss", f"{self.settings.mode} cycle failed; new entries suspended")
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
        # Market monitoring needs identity verification, not order permission.
        # ExecutionEngine still rechecks all permissions before every order.
        verify_account(account, self.settings, require_trading=False)
        self.check_trading_permissions(account)
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
        hidden_before = self.store.get(f"history_hidden_before:{account.key}")
        hidden_epoch = datetime.fromisoformat(hidden_before).timestamp() if hidden_before else None
        for deal in deals:
            # Users can hide old local journal rows without changing broker-side risk history.
            if hidden_epoch is not None and deal.get("time", 0) < hidden_epoch:
                continue
            new = self.store.record("broker_deals", deal, account.key, f"{account.key}:{deal['ticket']}")
            if new and deal.get("magic") == self.settings.magic:
                if deal.get("entry") in (1, 3):
                    reason = {4: "stop_loss", 5: "take_profit"}.get(int(deal.get("reason") or -1), "trade_exit")
                    self.event(reason, f"{deal.get('symbol')} • exit deal net P&L "
                               f"{sum(float(deal.get(k, 0)) for k in ('profit', 'commission', 'swap', 'fee')):.2f} "
                               f"{account.currency}")
        self.store.record("equity_snapshots", account, account.key)
        book = update_risk_snapshot(self.store, account, deals, now, self.settings.magic)
        breaker = CircuitBreaker(self.store, account.key)
        for field, limit in (("daily_loss", self.settings.daily_loss_limit),
                             ("weekly_loss", self.settings.weekly_loss_limit),
                             ("drawdown", self.settings.drawdown_limit),
                             ("consecutive_losses", self.settings.consecutive_loss_limit)):
            if book.get(field, 0) >= limit:
                breaker.trip(f"{field} threshold reached")
        self.process_commands(state, book)
        ownership = owned_positions(self.broker, self.store, self.settings, state.positions)
        self.position_updates(state.positions, ownership, account, now)
        for position in state.positions:
            if position.ticket in ownership and position.stop_loss <= 0:
                self.execution.close_position(position, emergency=True)
        self.time_exits(state.positions, deals, now)
        # The monitor checks all registered strategies unless the user narrows the list.
        # Each strategy keeps its own saved rule set and completed-candle cursor.
        requested = self.store.get("monitored_strategies", list(REGISTRY))
        if not isinstance(requested, list):
            raise ValueError("Monitored strategy selection must be a list")
        strategy_names = [name for name in REGISTRY if name in requested]
        if not strategy_names:
            # Ignore names persisted by versions that used the old prototype rules.
            strategy_names = ([self.settings.active_strategy] if self.settings.active_strategy in REGISTRY
                              else list(REGISTRY))
        if self.cache_symbol != symbol:
            self.last_frames.clear()
            self.last_bars.clear()
            self.cache_symbol = symbol
        strategies = [build(name, self.store.get(f"strategy_parameters:{name}", {}))
                      for name in strategy_names]
        needed_timeframes: dict[str, int] = {}
        for strategy in strategies:
            needed_timeframes[strategy.parameters.entry_timeframe] = 20
        for timeframe, days in needed_timeframes.items():
            current_bar = int(now.timestamp()) // (TIMEFRAMES[timeframe] * 60)
            if timeframe not in self.last_frames or current_bar != self.last_bars.get(timeframe):
                frame = self.broker.candles(symbol, timeframe, now - timedelta(days=days), now)
                quality = validate(frame, timeframe)
                if not quality["valid"]:
                    raise ValueError(f"Invalid or unavailable broker {timeframe} candles")
                completed = closed(frame, timeframe, now)
                if completed.empty:
                    raise ValueError(f"No completed {timeframe} candle is available")
                self.last_frames[timeframe] = completed
                self.last_bars[timeframe] = current_bar
                self.store.save_candles(completed, account.server, symbol, timeframe,
                                       self.broker.spec(symbol).price_basis)
        quote = self.broker.quote(symbol)
        self.store.set("latest_quote", quote)
        if not 0 <= (now - quote.timestamp).total_seconds() <= self.settings.quote_max_age_seconds:
            breaker.trip("Stale/future market quote")
        if quote.spread < 0 or (self.settings.max_spread is not None and quote.spread > self.settings.max_spread):
            breaker.trip("Invalid or excessive spread")
        mode_key = f"execution_mode:{account.key}"
        execution_mode = self.store.get(mode_key, self.settings.auto_execution_mode)
        if execution_mode not in ("SIGNAL_ONLY", self.settings.auto_execution_mode):
            # A mode saved for another account environment must never carry authorization across.
            self.store.set(f"armed:{account.key}", {"active": False})
            execution_mode = "SIGNAL_ONLY"
        armed = self.store.get(f"armed:{account.key}", {"active": False})
        if armed.get("fingerprint") != permission_fingerprint(self.settings):
            self.store.set(f"armed:{account.key}", {"active": False})
            armed = {"active": False}
        entry_timeframes = set()
        for strategy in strategies:
            entry_timeframe = strategy.parameters.entry_timeframe
            frames = {entry_timeframe: self.last_frames[entry_timeframe]}
            entry_frame = frames[entry_timeframe]
            entry_timeframes.add(entry_timeframe)
            last = entry_frame.index[-1] + timedelta(minutes=TIMEFRAMES[entry_timeframe])
            cursor_key = f"cursor:{account.key}:{strategy.config_hash}:{symbol}"
            if self.store.get(cursor_key) == last.isoformat():
                continue
            signals = strategy.signals(frames, symbol)
            current = [signal for signal in signals if signal.timestamp == last.to_pydatetime()]
            for proposed in current:
                proposed = normalize_signal(proposed, self.broker.spec(symbol))
                is_new = self.store.record("signals", proposed, account.key, proposed.key(account.key))
                self.store.set(f"latest_signal:{account.key}:{strategy.name}", proposed)
                self.store.set("latest_signal", proposed)
                if is_new:
                    if execution_mode == "SIGNAL_ONLY":
                        prefix = "SIGNAL ONLY — no broker order"
                    elif not armed.get("active") or breaker.current()["active"]:
                        prefix = f"AUTO {account.environment} selected — signal only; order permission is off"
                    else:
                        prefix = "AUTO LIVE — real money; all risk checks apply" if account.environment == "LIVE" \
                            else "AUTO DEMO — all risk checks apply"
                    self.event("signal", f"{prefix}\n{proposed.symbol} {proposed.direction.value} • "
                               f"{proposed.strategy}\n"
                               f"Entry {proposed.entry:.3f} • stop {proposed.stop_loss:.3f} • "
                               f"target {proposed.take_profit:.3f}\n{proposed.rationale}")
                if execution_mode == "SIGNAL_ONLY" or not armed.get("active"):
                    from app.execution.signal_tracker import SignalOutcomeTracker
                    SignalOutcomeTracker(self.store, self.settings).start(
                        proposed, account.key, self.broker.spec(symbol), account.equity)
                if execution_mode == self.settings.auto_execution_mode and armed.get("active") \
                        and not breaker.current()["active"]:
                    schedule = SessionSchedule(self.settings.session_file, symbol, account.server)
                    self.execution.execute(proposed, now, book, schedule)
            self.store.set(cursor_key, last.isoformat())
        from app.execution.signal_tracker import SignalOutcomeTracker
        tracker = SignalOutcomeTracker(self.store, self.settings)
        for entry_timeframe in entry_timeframes:
            tracker.update(account.key, symbol, entry_timeframe, self.last_frames[entry_timeframe],
                           self.broker.spec(symbol))
        status = "HALTED" if breaker.current()["active"] else "RUNNING"
        self.store.set("service", {"state": status, "account": account.key,
                                   "strategy": ", ".join(strategy_names), "strategies": strategy_names,
                                   "execution_mode": execution_mode,
                                   "config_hashes": [strategy.config_hash for strategy in strategies],
                                   "heartbeat": now.isoformat(),
                                   "data_refresh": now.isoformat(), "pid": os.getpid()})
        day = now.date().isoformat()
        if self.store.get(f"daily_summary:{account.key}") != day:
            self.event("daily_summary", f"UTC daily P&L {book['daily_pnl']:.2f}; weekly {book['weekly_pnl']:.2f}")
            self.store.set(f"daily_summary:{account.key}", day)

    def check_trading_permissions(self, account) -> None:
        """Persist and notify permission changes without repeating alerts every poll."""
        blockers = []
        if not account.trade_allowed:
            blockers.append("Trading permission for this MT5 account is off.")
        if not account.expert_allowed:
            if not account.terminal_trade_allowed:
                blockers.append("MT5 Algo Trading is off. In MT5, enable the top-toolbar Algo Trading button and Tools → Options → Expert Advisors → Allow algorithmic trading.")
            if not account.external_api_allowed:
                blockers.append("MT5 blocks Python trading. In Tools → Options → Expert Advisors, clear ‘Disable algorithmic trading via external Python API’.")
            if account.terminal_trade_allowed and account.external_api_allowed:
                blockers.append("Expert trading is not allowed for this MT5 account.")
        key = f"trading_permissions:{account.key}"
        previous = self.store.get(key, {"blocked": False})
        current = {"blocked": bool(blockers), "blockers": blockers,
                   "checked_at": utcnow().isoformat(),
                   "trade_allowed": account.trade_allowed,
                   "expert_allowed": account.expert_allowed,
                   "terminal_trade_allowed": account.terminal_trade_allowed,
                   "external_api_allowed": account.external_api_allowed}
        if blockers:
            CircuitBreaker(self.store, account.key).trip("MT5 trading permission is disabled")
            if not previous.get("blocked") or previous.get("blockers") != blockers:
                self.event("trading_permission_lost", " ".join(blockers) + " New automatic entries are blocked.")
        elif previous.get("blocked"):
            self.event("trading_permission_restored",
                       "MT5 reports account, terminal, and Python API trading permissions enabled. "
                       "The safety halt remains until recovery checks pass and it is explicitly reset.")
        self.store.set(key, current)

    def position_updates(self, positions, owned, account, now) -> None:
        """Notify for bot and manual positions; only bot-owned positions are managed."""
        visible = {position.ticket for position in positions}
        prefix = f"position_notice:{account.key}:"
        for row in self.store.list_state(prefix):
            ticket = int(row["key"].rsplit(":", 1)[1])
            if row["value"].get("active") and ticket not in visible:
                owner = row["value"].get("owner", "account")
                message = (f"{owner} position closed or no longer visible • ticket {ticket} • "
                           f"last floating P&L {row['value'].get('profit', 0):.2f} {account.currency}. "
                           "Check broker history for final realized P&L.")
                self.event("position_closed", message)
                self.store.set(row["key"], {**row["value"], "active": False, "closed_at": now.isoformat()})
        for position in positions:
            key = f"{prefix}{position.ticket}"
            previous = self.store.get(key, {})
            last_update = previous.get("last_update")
            elapsed = (now - datetime.fromisoformat(last_update)).total_seconds() if last_update else None
            interval = max(1, int(self.store.get("telegram_preferences", {}).get("position_update_minutes", 15)))
            event = "position_update" if previous.get("active") and elapsed is not None \
                and elapsed >= interval * 60 \
                else "position_open" if not previous.get("active") else None
            if event is None:
                continue
            side = position.direction.value
            owner = "Bot" if position.ticket in owned else "Manual or unrecognized account"
            update_label = "OPEN" if event == "position_open" else f"{interval}-minute update"
            message = (f"{owner} position {update_label} • "
                       f"{position.symbol} {side} • {position.volume:g} lots\n"
                       f"Entry {position.entry:.3f} • stop {position.stop_loss:.3f} • "
                       f"target {position.take_profit:.3f}\n"
                       f"Floating P&L {position.profit:.2f} {account.currency} • ticket {position.ticket}" +
                       ("\nThis account position is not tied to a bot order; the bot will not manage it."
                        if position.ticket not in owned else ""))
            self.event(event, message)
            self.store.set(key, {"active": True, "last_update": now.isoformat(), "environment": account.environment,
                                 "owner": owner, "profit": position.profit})

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
                    allowed = {"mt5_path", "account_login", "account_server", "symbol", "mode"}
                    if set(payload) - allowed:
                        raise ValueError("Only nonsecret broker identity and environment fields can be changed here")
                    candidate = Settings(**{**self.settings.model_dump(), **payload})
                    candidate.require_execution_environment()
                    self.store.set("broker_override", payload)
                    self.store.set(f"armed:{self.account_key}", {"active": False})
                    self.stop_event.set()
                elif kind == "set-execution-mode":
                    mode = payload.get("mode")
                    if mode not in ("SIGNAL_ONLY", self.settings.auto_execution_mode):
                        raise ValueError(f"Execution mode for {self.settings.mode} must be SIGNAL_ONLY or "
                                         f"{self.settings.auto_execution_mode}")
                    # Every mode switch requires a fresh audited authorization before orders.
                    self.store.set(f"armed:{self.account_key}", {"active": False})
                    self.store.set(f"execution_mode:{self.account_key}", mode)
                    self.store.record("activity", {"event": "execution_mode_changed", "mode": mode},
                                      self.account_key)
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
                elif kind == "set-monitored-strategies":
                    names = payload.get("names")
                    if not isinstance(names, list) or not names or any(name not in REGISTRY for name in names):
                        raise ValueError("Choose one or more registered strategies for monitoring")
                    names = list(dict.fromkeys(names))
                    self.store.set("monitored_strategies", names)
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
