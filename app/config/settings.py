from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="XAU_", env_file=".env", extra="ignore", env_ignore_empty=True,
                                      allow_inf_nan=False)

    mode: Literal["BACKTEST", "DEMO", "LIVE"] = "DEMO"
    database_url: str = "sqlite:///data/platform.db"
    mt5_path: str | None = None
    account_login: int | None = None
    account_server: str | None = None
    expected_company: str = "Exness"
    account_password: SecretStr | None = None
    symbol: str | None = None
    session_file: Path | None = None
    active_strategy: str = "trend_pullback"
    entries_armed: bool = False
    risk_per_trade: float = Field(default=0.0025, gt=0, le=0.005)
    hard_risk_max: float = Field(default=0.005, gt=0, le=0.005)
    daily_loss_limit: float = Field(default=0.01, gt=0, le=0.1)
    weekly_loss_limit: float = Field(default=0.03, gt=0, le=0.2)
    drawdown_limit: float = Field(default=0.10, gt=0, le=0.5)
    portfolio_risk_limit: float = Field(default=0.005, gt=0, le=0.02)
    max_positions: int = Field(default=1, ge=1, le=1)
    max_orders_session: int = Field(default=3, ge=1)
    consecutive_loss_limit: int = Field(default=3, ge=1)
    loss_cooldown_minutes: int = Field(default=60, ge=1)
    emergency_close_attempts: int = Field(default=3, ge=1, le=10)
    max_spread: float | None = Field(default=0.50, gt=0)
    max_slippage_points: int | None = Field(default=20, ge=0)
    commission_per_lot: float | None = Field(default=None, ge=0)
    slippage_price: float | None = Field(default=0.20, ge=0)
    swap_long_per_lot_day: float | None = None
    swap_short_per_lot_day: float | None = None
    quote_max_age_seconds: int = Field(default=10, ge=1)
    poll_seconds: float = Field(default=5, ge=1)
    magic: int = Field(default=260611, gt=0)
    report_dir: Path = Path("reports")
    telegram_token: SecretStr | None = None
    telegram_chat_id: SecretStr | None = None
    retention_days: int = Field(default=365, ge=30)

    @model_validator(mode="after")
    def validate_risk(self) -> "Settings":
        if self.risk_per_trade > self.hard_risk_max:
            raise ValueError("risk_per_trade exceeds hard_risk_max")
        if self.entries_armed:
            raise ValueError("Entries cannot be armed by environment; use the audited arm-demo command")
        return self

    def require_demo(self) -> None:
        if self.mode != "DEMO":
            raise ValueError("Broker execution requires DEMO; LIVE execution is unavailable")
        if not self.account_login or not self.account_server or not self.mt5_path:
            raise ValueError("Explicit MT5 path, expected login, and server are required")

    def require_costs(self) -> None:
        if any(value is None for value in (
            self.commission_per_lot, self.slippage_price,
            self.swap_long_per_lot_day, self.swap_short_per_lot_day,
        )):
            raise ValueError("Explicit commission, slippage, and long/short financing inputs are required")

    def public(self) -> dict:
        return self.model_dump(mode="json", exclude={"account_password", "telegram_token", "telegram_chat_id"})
