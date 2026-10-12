import builtins
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import pandas as pd
from sqlalchemy import create_engine, delete, event, func, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Connection

from app.database.models import candles, commands, intents, metadata, records, state
from app.domain import AuditMetadata, digest, utcnow


def clean(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return json.loads(json.dumps(value, default=str, allow_nan=False))


class Store:
    def __init__(self, url: str):
        if not url.startswith("sqlite:///"):
            raise ValueError("Only local SQLite databases are supported")
        if url != "sqlite:///:memory:":
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(url, connect_args={"timeout": 15})

        @event.listens_for(self.engine, "connect")
        def pragmas(connection, _):
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=15000")
            connection.execute("PRAGMA synchronous=FULL")

    def migrate(self) -> None:
        from alembic import command
        from alembic.config import Config
        root = Path(__file__).resolve().parents[2]
        cfg = Config(str(root / "alembic.ini"))
        cfg.set_main_option("script_location", str(root / "migrations"))
        cfg.set_main_option("sqlalchemy.url", str(self.engine.url).replace("%", "%%"))
        with self.engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "head")

    def create_for_tests(self) -> None:
        metadata.create_all(self.engine)

    def get(self, key: str, default: Any = None, conn: Connection | None = None) -> Any:
        if conn is None:
            with self.engine.connect() as c:
                return self.get(key, default, c)
        row = conn.execute(select(state.c.value).where(state.c.key == key)).first()
        return row[0] if row else default

    def list_state(self, prefix: str) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(state.c.key, state.c.value).where(state.c.key.startswith(prefix))
                                .order_by(state.c.key)).mappings()
            return [dict(row) for row in rows]

    @staticmethod
    def _report_paths(payload: dict) -> set[str]:
        paths: set[str] = set()
        if isinstance(payload, dict):
            for key, value in payload.items():
                if key in {"report", "summary_report", "holdout_report"} and isinstance(value, str):
                    paths.add(value)
                elif isinstance(value, (dict, list)):
                    values = value.values() if isinstance(value, dict) else value
                    for item in values:
                        if isinstance(item, dict):
                            paths.update(Store._report_paths(item))
        return paths

    def research_cleanup_preview(self) -> dict:
        names = ("backtest_results", "experiments", "approvals")
        counts: dict[str, int] = {}
        reports: set[str] = set()
        with self.engine.connect() as conn:
            for name in names:
                table = records[name]
                counts[name] = int(conn.execute(select(func.count()).select_from(table)).scalar_one())
                for row in conn.execute(select(table.c.payload)).mappings():
                    reports.update(self._report_paths(row["payload"]))
        return {"counts": counts, "report_paths": sorted(reports)}

    def active_research_jobs(self) -> list[dict]:
        """Find live research workers and mark records from exited workers as stale."""
        active = []
        for row in self.list_state("research_job:"):
            value = row["value"]
            if value.get("state") != "RUNNING":
                continue
            try:
                alive = self._process_is_alive(int(value["pid"]))
            except (OSError, ValueError, KeyError):
                alive = False
            if not alive:
                failed = {**value, "state": "FAILED", "finished_at": utcnow().isoformat(),
                          "error": "Worker exited before recording completion"}
                self.set(row["key"], failed)
            else:
                active.append(value)
        return active

    @staticmethod
    def _process_is_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        if os.name != "nt":
            try:
                os.kill(pid, 0)
            except OSError:
                return False
            return True

        # On Windows, os.kill(pid, 0) is not a reliable process-existence probe.
        # Waiting on a SYNCHRONIZE handle distinguishes a running worker from an
        # exited one; access denied is treated as alive so cleanup fails safely.
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
        open_process.restype = ctypes.c_void_p
        handle = open_process(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED
        try:
            wait = kernel32.WaitForSingleObject
            wait.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
            wait.restype = ctypes.c_uint32
            return wait(handle, 0) == 0x00000102  # WAIT_TIMEOUT
        finally:
            close = kernel32.CloseHandle
            close.argtypes = (ctypes.c_void_p,)
            close.restype = ctypes.c_int
            close(handle)

    def clear_research_history(self) -> dict:
        """Clear saved research output, leaving candles and one-time holdout claims intact."""
        preview = self.research_cleanup_preview()
        with self.engine.begin() as conn:
            for name in ("backtest_results", "experiments", "approvals"):
                conn.execute(delete(records[name]))
            approval_keys: list[str] = list(conn.execute(
                select(state.c.key).where(state.c.key.startswith("approval:"))).scalars())
            for key in approval_keys:
                conn.execute(delete(state).where(state.c.key == key))
        return preview

    def count_candles(self, source: str, symbol: str, timeframes: list[str], before: str | None = None) -> int:
        if not source or not symbol or not timeframes:
            return 0
        stmt = select(func.count()).select_from(candles).where(
            candles.c.source == source, candles.c.symbol == symbol, candles.c.timeframe.in_(timeframes))
        if before:
            stmt = stmt.where(candles.c.timestamp < before)
        with self.engine.connect() as conn:
            return int(conn.execute(stmt).scalar_one())

    def clear_candles(self, source: str, symbol: str, timeframes: list[str], before: str | None = None) -> int:
        """Delete only explicitly selected local market bars; broker history is unaffected."""
        if not source or not symbol or not timeframes or len(set(timeframes)) != len(timeframes):
            raise ValueError("Choose a data source, symbol, and one or more distinct timeframes")
        stmt = delete(candles).where(candles.c.source == source, candles.c.symbol == symbol,
                                     candles.c.timeframe.in_(timeframes))
        if before:
            stmt = stmt.where(candles.c.timestamp < before)
        with self.engine.begin() as conn:
            result = conn.execute(stmt)
            # Download checkpoints are hints only; clear them so a later download can fill the gap.
            download_keys: list[str] = list(conn.execute(
                select(state.c.key).where(state.c.key.startswith("download_chunk:"))).scalars())
            for key in download_keys:
                conn.execute(delete(state).where(state.c.key == key))
            return int(result.rowcount or 0)

    @staticmethod
    def _cutoff(value: str | datetime) -> datetime:
        cutoff = datetime.fromisoformat(value) if isinstance(value, str) else value
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=UTC)
        return cutoff.astimezone(UTC)

    def _account_history_ids(self, conn: Connection, account: str, cutoff: datetime) -> dict[str, list[int]]:
        reconciliation = self.get(f"reconciliation:{account}", {}, conn)
        open_positions = {int(p["ticket"]) for p in reconciliation.get("positions", []) if p.get("ticket") is not None}
        deals = list(conn.execute(select(records["broker_deals"]).where(records["broker_deals"].c.account == account))
                     .mappings())
        lifecycle_ids: set[int] = set()
        standalone_ids: set[int] = set()
        position_ids_before_cutoff: set[int] = set()
        for row in deals:
            payload = row["payload"]
            position_id = payload.get("position_id")
            timestamp = payload.get("time")
            deal_time = datetime.fromtimestamp(float(timestamp), UTC) if timestamp is not None else datetime.fromisoformat(row["timestamp"])
            if deal_time < cutoff:
                if position_id:
                    if int(position_id) not in open_positions:
                        position_ids_before_cutoff.add(int(position_id))
                else:
                    standalone_ids.add(int(row["id"]))
        for row in deals:
            payload = row["payload"]
            if payload.get("position_id") and int(payload["position_id"]) in position_ids_before_cutoff:
                lifecycle_ids.add(int(row["id"]))
        result = {"broker_deals": sorted(lifecycle_ids | standalone_ids)}
        for name in ("broker_orders", "position_snapshots", "equity_snapshots", "signals", "risk_decisions",
                     "activity", "connection_events", "errors", "notifications", "service_events"):
            table = records[name]
            rows = conn.execute(select(table.c.id, table.c.timestamp).where(table.c.account == account)).mappings()
            result[name] = [int(row["id"]) for row in rows
                            if datetime.fromisoformat(row["timestamp"]).astimezone(UTC) < cutoff]
        return result

    def account_history_cleanup_preview(self, account: str, before: str | datetime) -> dict:
        cutoff = self._cutoff(before)
        with self.engine.connect() as conn:
            active = self.get("active_account", {}, conn)
            expected = f"{active.get('server')}:{active.get('login')}" if active else None
            if account != expected:
                raise ValueError("For safety, select the currently verified MT5 account before clearing its history")
            ids = self._account_history_ids(conn, account, cutoff)
            final_signals = 0
            outcomes: list[dict[str, Any]] = list(conn.execute(
                select(state.c.value).where(state.c.key.startswith(f"signal_outcome:{account}:"))).scalars())
            for outcome in outcomes:
                if outcome.get("status") in {"CLOSED", "EXPIRED", "BELOW_MINIMUM_VOLUME", "COSTS_UNAVAILABLE", "INVALID_BRACKET"}:
                    when = outcome.get("signal_time") or outcome.get("updated_at")
                    if when and self._cutoff(when) < cutoff:
                        final_signals += 1
        return {"counts": {name: len(row_ids) for name, row_ids in ids.items()},
                "completed_signal_outcomes": final_signals,
                "open_positions_kept": len(self.get(f"reconciliation:{account}", {}).get("positions", []))}

    def clear_account_history(self, account: str, before: str | datetime) -> dict:
        """Remove old local records for the verified account while preserving live safety state."""
        cutoff = self._cutoff(before)
        with self.engine.begin() as conn:
            active = self.get("active_account", {}, conn)
            expected = f"{active.get('server')}:{active.get('login')}" if active else None
            if account != expected:
                raise ValueError("For safety, select the currently verified MT5 account before clearing its history")
            ids = self._account_history_ids(conn, account, cutoff)
            removed = {}
            for name, row_ids in ids.items():
                if row_ids:
                    result = conn.execute(delete(records[name]).where(records[name].c.id.in_(row_ids)))
                    removed[name] = int(result.rowcount or 0)
                else:
                    removed[name] = 0
            completed = 0
            for row in conn.execute(select(state.c.key, state.c.value).where(
                    state.c.key.startswith(f"signal_outcome:{account}:"))).mappings():
                value = row["value"]
                if value.get("status") in {"CLOSED", "EXPIRED", "BELOW_MINIMUM_VOLUME", "COSTS_UNAVAILABLE", "INVALID_BRACKET"}:
                    when = value.get("signal_time") or value.get("updated_at")
                    if when and self._cutoff(when) < cutoff:
                        conn.execute(delete(state).where(state.c.key == row["key"]))
                        completed += 1
            suppression_key = f"history_hidden_before:{account}"
            current = self.get(suppression_key, None, conn)
            if current:
                cutoff = max(cutoff, self._cutoff(current))
            self.set(suppression_key, cutoff.isoformat(), conn)
            return {"deleted": removed, "completed_signal_outcomes": completed,
                    "open_positions_kept": len(self.get(f"reconciliation:{account}", {}).get("positions", [])),
                    "broker_history_hidden_before": cutoff.isoformat()}

    def set(self, key: str, value: Any, conn: Connection | None = None) -> None:
        if conn is None:
            with self.engine.begin() as c:
                self.set(key, value, c)
                return
        stmt = insert(state).values(key=key, value=clean(value))
        conn.execute(stmt.on_conflict_do_update(index_elements=[state.c.key], set_={"value": stmt.excluded.value}))

    def record(self, name: str, payload: Any, account: str | None = None,
               key: str | None = None, conn: Connection | None = None) -> bool:
        if conn is None:
            with self.engine.begin() as c:
                return self.record(name, payload, account, key, c)
        payload = clean(payload)
        recorded_at = utcnow()
        research_record = name in {"experiments", "backtest_results", "approvals"}
        active_account = self.get("active_account", {}, conn) if account and not research_record else {}
        account_environment: Literal["DEMO", "LIVE", "UNKNOWN"] = (
            active_account.get("environment", "DEMO") if account else "UNKNOWN")
        if account_environment not in ("DEMO", "LIVE"):
            account_environment = "UNKNOWN"
        context = AuditMetadata(environment="BACKTEST" if research_record else account_environment,
                                source=payload.get("source") or (account.rsplit(":", 1)[0] if account else
                                       self.get("research_source", "local", conn) if research_record else "local"),
                                timestamp=recorded_at,
                                configuration_hash=payload.get("config_hash") or payload.get("signal", {}).get("config_hash"),
                                correlation_id=key or digest([name, account, recorded_at.isoformat(), payload]))
        payload["_meta"] = context.model_dump(mode="json")
        result = conn.execute(insert(records[name]).values(timestamp=recorded_at.isoformat(), account=account,
                                                          key=key, payload=payload).on_conflict_do_nothing())
        return result.rowcount == 1

    def list(self, name: str, limit: int = 500, account: str | None = None) -> list[dict]:
        table = {"order_intents": intents, "commands": commands}.get(name, records.get(name))
        if table is None:
            raise KeyError(name)
        stmt = select(table).order_by(table.c.timestamp.desc()).limit(limit)
        if account is not None:
            stmt = stmt.where(table.c.account == account)
        with self.engine.connect() as conn:
            return [dict(row) for row in conn.execute(stmt).mappings()]

    def save_candles(self, frame: pd.DataFrame, source: str, symbol: str, timeframe: str,
                     price_basis="broker_OHLC") -> None:
        rows = []
        for timestamp, row in frame.iterrows():
            if not isinstance(timestamp, pd.Timestamp):
                raise ValueError("Historical data requires timestamp indices")
            rows.append(dict(source=source, symbol=symbol, timeframe=timeframe,
                             timestamp=timestamp.tz_convert("UTC").isoformat(),
                             **{k: float(row[k]) for k in ("open", "high", "low", "close")},
                             **{k: int(row.get(k, 0)) for k in ("tick_volume", "spread", "real_volume")},
                             downloaded_at=utcnow().isoformat(), price_basis=price_basis))
        with self.engine.begin() as conn:
            for offset in range(0, len(rows), 1000):
                if rows[offset:offset + 1000]:
                    conn.execute(insert(candles).on_conflict_do_nothing(), rows[offset:offset + 1000])

    def load_candles(self, source: str, symbol: str, timeframe: str,
                     start: str | None = None, end: str | None = None) -> pd.DataFrame:
        stmt = select(candles).where(candles.c.source == source, candles.c.symbol == symbol,
                                     candles.c.timeframe == timeframe).order_by(candles.c.timestamp)
        if start:
            stmt = stmt.where(candles.c.timestamp >= start)
        if end:
            stmt = stmt.where(candles.c.timestamp < end)
        with self.engine.connect() as conn:
            frame = pd.DataFrame(conn.execute(stmt).mappings().all())
        if frame.empty:
            return frame
        frame.index = pd.DatetimeIndex(pd.to_datetime(frame.pop("timestamp"), utc=True))
        return frame

    def enqueue(self, kind: str, payload: dict | None = None) -> int:
        allowed = {"stop", "halt", "reset-halt", "configure", "configure-broker", "select-strategy",
                   "set-execution-mode", "set-monitored-strategies", "close-position"}
        if kind not in allowed:
            raise ValueError("Unsupported control command")
        payload = dict(payload or {})
        active = self.get("active_account")
        if active and "account" not in payload:
            payload["account"] = f"{active['server']}:{active['login']}"
        with self.engine.begin() as conn:
            result = conn.execute(insert(commands).values(timestamp=utcnow().isoformat(), kind=kind,
                                                          state="PENDING", payload=clean(payload)))
            primary_key = result.inserted_primary_key
            assert primary_key is not None
            return int(primary_key[0])

    def pending_commands(self) -> builtins.list[dict]:
        with self.engine.connect() as conn:
            return [dict(r) for r in conn.execute(select(commands).where(commands.c.state == "PENDING")
                                                .order_by(commands.c.id)).mappings()]

    def finish_command(self, command_id: int, success: bool, payload: dict) -> None:
        with self.engine.begin() as conn:
            conn.execute(update(commands).where(commands.c.id == command_id).values(
                state="DONE" if success else "REJECTED", payload=clean(payload)))
