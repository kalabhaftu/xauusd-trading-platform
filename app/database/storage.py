import builtins
import json
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import create_engine, event, select, update
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
        context = AuditMetadata(environment="BACKTEST" if research_record else "DEMO" if account else "UNKNOWN",
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
        allowed = {"stop", "halt", "reset-halt", "configure", "configure-broker", "select-strategy", "close-position"}
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
