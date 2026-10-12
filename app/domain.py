import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

TradingEnvironment = Literal["DEMO", "LIVE", "CONTEST"]
ExecutionState = Literal["FILLED", "PARTIAL", "REJECTED", "UNKNOWN", "ACCEPTED"]


def utcnow() -> datetime:
    return datetime.now(UTC)


def digest(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


class Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class Direction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class AuditMetadata(Model):
    environment: Literal["DEMO", "LIVE", "BACKTEST", "UNKNOWN"]
    source: str
    timestamp: datetime = Field(default_factory=utcnow)
    configuration_version: str = "1"
    configuration_hash: str | None = None
    correlation_id: str


class Account(Model):
    login: int
    server: str
    company: str
    environment: TradingEnvironment
    currency: str
    balance: float
    equity: float
    margin: float
    free_margin: float
    trade_allowed: bool
    expert_allowed: bool
    terminal_trade_allowed: bool = True
    external_api_allowed: bool = True
    timestamp: datetime = Field(default_factory=utcnow)

    @property
    def key(self) -> str:
        return f"{self.server}:{self.login}"


class SymbolSpec(Model):
    symbol: str
    point: float = Field(gt=0)
    digits: int
    tick_size: float = Field(gt=0)
    tick_value: float = Field(ge=0)
    contract_size: float = Field(gt=0)
    volume_min: float = Field(gt=0)
    volume_max: float = Field(gt=0)
    volume_step: float = Field(gt=0)
    stops_level: int = Field(ge=0)
    freeze_level: int = Field(default=0, ge=0)
    filling_mode: int = 0
    trade_mode: int = 4
    currency_profit: str = "USD"
    currency_margin: str = "USD"
    price_basis: Literal["bid", "last", "unknown"] = "unknown"
    timestamp: datetime = Field(default_factory=utcnow)


class Quote(Model):
    symbol: str
    bid: float = Field(gt=0)
    ask: float = Field(gt=0)
    timestamp: datetime

    @field_validator("timestamp")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Quote timestamp must be timezone aware")
        return value.astimezone(UTC)

    @property
    def spread(self) -> float:
        return self.ask - self.bid


class Signal(Model):
    symbol: str
    strategy: str
    config_hash: str
    direction: Direction
    timestamp: datetime
    entry: float = Field(gt=0)
    stop_loss: float = Field(gt=0)
    take_profit: float = Field(gt=0)
    rationale: str
    entry_condition: str = "next available market quote after candle close"
    regime: str
    indicators: dict[str, float]
    session: str = "continuous"
    expires_at: datetime | None = None
    exit_at: datetime | None = None

    @field_validator("timestamp", "expires_at", "exit_at")
    @classmethod
    def aware_time(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("Signal times must be timezone aware")
        return value.astimezone(UTC) if value else None

    def key(self, account: str) -> str:
        return digest([account, self.strategy, self.config_hash, self.symbol, self.timestamp.isoformat()])


class Position(Model):
    ticket: int
    symbol: str
    direction: Direction
    volume: float
    entry: float
    stop_loss: float
    take_profit: float
    profit: float
    magic: int
    timestamp: datetime
    comment: str = ""


class ExecutionResult(Model):
    state: ExecutionState
    retcode: int | None = None
    order: int | None = None
    deal: int | None = None
    volume: float = 0
    price: float = 0
    message: str = ""


class RiskDecision(Model):
    approved: bool
    reason: str
    volume: float = 0
    entry: float = 0
    risk_amount: float = 0
    margin: float = 0


class Reconciliation(Model):
    safe: bool
    reasons: list[str]
    positions: list[Position]


class Candle(Model):
    source: str
    symbol: str
    timeframe: Literal["M1", "M5", "M15", "M30", "H1", "H4", "D1"]
    timestamp: datetime
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    tick_volume: int = Field(ge=0)
    spread: int = Field(ge=0)
    real_volume: int = Field(ge=0)
    price_basis: str = "broker_OHLC"
    downloaded_at: datetime = Field(default_factory=utcnow)


class OrderIntent(Model):
    key: str
    account: str
    environment: Literal["DEMO", "LIVE"] = "DEMO"
    state: str
    timestamp: datetime
    signal: Signal
    decision: RiskDecision
    request: dict[str, Any]
    correlation_id: str
