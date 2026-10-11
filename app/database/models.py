from sqlalchemy import JSON, Column, Float, Integer, MetaData, String, Table, UniqueConstraint

metadata = MetaData()
state = Table("state", metadata, Column("key", String, primary_key=True), Column("value", JSON, nullable=False))
candles = Table(
    "candles", metadata,
    Column("id", Integer, primary_key=True), Column("source", String, nullable=False),
    Column("symbol", String, nullable=False), Column("timeframe", String, nullable=False),
    Column("timestamp", String, nullable=False, index=True),
    Column("open", Float), Column("high", Float), Column("low", Float), Column("close", Float),
    Column("tick_volume", Integer), Column("spread", Integer), Column("real_volume", Integer),
    Column("downloaded_at", String), Column("price_basis", String),
    UniqueConstraint("source", "symbol", "timeframe", "timestamp"),
)
intents = Table(
    "order_intents", metadata, Column("key", String, primary_key=True),
    Column("account", String, nullable=False, index=True), Column("state", String, nullable=False),
    Column("timestamp", String, nullable=False), Column("payload", JSON, nullable=False),
)
commands = Table(
    "commands", metadata, Column("id", Integer, primary_key=True),
    Column("timestamp", String, nullable=False), Column("kind", String, nullable=False),
    Column("state", String, nullable=False), Column("payload", JSON, nullable=False),
)
RECORD_NAMES = (
    "signals", "risk_decisions", "broker_orders", "broker_deals", "position_snapshots", "equity_snapshots",
    "circuit_breaker_events", "connection_events", "errors", "activity", "strategy_configurations",
    "experiments", "backtest_results", "approvals", "data_quality", "notifications", "service_events",
)
records = {
    name: Table(
        name, metadata, Column("id", Integer, primary_key=True), Column("timestamp", String, nullable=False),
        Column("account", String, index=True), Column("key", String, unique=True),
        Column("payload", JSON, nullable=False),
    ) for name in RECORD_NAMES
}
