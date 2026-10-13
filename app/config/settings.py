import os
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="XAU_", env_file=os.environ.get("AURUM_SETTINGS_FILE", ".env"),
                                      extra="ignore", env_ignore_empty=True,
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
    active_strategy: str = "ict_sweep_mss_fvg"
    cost_mode: Literal["AUTO", "CUSTOM"] = "AUTO"
    entries_armed: bool = False
    risk_per_trade: float = Field(default=0.0025, gt=0, le=1)
    hard_risk_max: float = Field(default=1.0, gt=0, le=1)
    daily_loss_limit: float = Field(default=0.05, gt=0, le=1)
    weekly_loss_limit: float = Field(default=0.03, gt=0, le=1)
    drawdown_limit: float = Field(default=0.10, gt=0, le=1)
    portfolio_risk_limit: float = Field(default=1.0, gt=0, le=1)
    max_positions: int = Field(default=1, ge=1)
    max_orders_session: int = Field(default=3, ge=1)
    consecutive_loss_limit: int = Field(default=3, ge=1)
    loss_cooldown_minutes: int = Field(default=60, ge=1)
    emergency_close_attempts: int = Field(default=3, ge=1, le=10)
    max_spread: float | None = Field(default=0.50, gt=0)
    max_slippage_points: int | None = Field(default=20, ge=0)
    commission_per_lot: float | None = Field(default=0, ge=0)
    slippage_price: float | None = Field(default=0.20, ge=0)
    swap_long_per_lot_day: float | None = 0
    swap_short_per_lot_day: float | None = 0
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
            raise ValueError("Entries cannot be armed by environment; use the audited account-specific arm command")
        if self.cost_mode == "CUSTOM" and any(value is None for value in (
            self.commission_per_lot, self.slippage_price,
            self.swap_long_per_lot_day, self.swap_short_per_lot_day,
        )):
            raise ValueError("Custom cost mode needs commission, slippage, and both financing values")
        return self

    @property
    def auto_execution_mode(self) -> str:
        if self.mode == "DEMO":
            return "AUTO_DEMO"
        if self.mode == "LIVE":
            return "AUTO_LIVE"
        raise ValueError("BACKTEST mode cannot execute broker orders")

    def require_execution_environment(self) -> None:
        if self.mode not in ("DEMO", "LIVE"):
            raise ValueError("Broker execution requires explicitly configured DEMO or LIVE mode")
        if not self.account_login or not self.account_server:
            raise ValueError("MT5 account number and server are required")

    def require_demo(self) -> None:
        if self.mode != "DEMO":
            raise ValueError("This operation requires DEMO mode")
        self.require_execution_environment()

    def require_costs(self) -> None:
        if any(value is None for value in (
            self.commission_per_lot, self.slippage_price,
            self.swap_long_per_lot_day, self.swap_short_per_lot_day,
        )):
            raise ValueError("Cost values are incomplete. Select Automatic estimates or enter all broker costs in Custom mode.")

    def public(self) -> dict:
        return self.model_dump(mode="json", exclude={"account_password", "telegram_token", "telegram_chat_id"})
