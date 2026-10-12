"""GUI setup persistence for users who do not manage a local .env file."""

import os
from pathlib import Path
from typing import Any

from app.config.settings import Settings
from app.domain import utcnow


def save_user_setup(store, current: Settings, *, mode: str, account_login: int,
                    account_server: str, symbol: str, mt5_path: str | None,
                    settings_changes: dict[str, Any], strategy: str,
                    execution_mode: str, telegram_preferences: dict[str, Any]) -> Settings:
    if mode not in ("DEMO", "LIVE"):
        raise ValueError("Choose a demo or live broker account")
    if account_login < 1 or not account_server.strip() or not symbol.strip():
        raise ValueError("Enter the account number, exact server name, and gold symbol")
    editable = {"cost_mode", "risk_per_trade", "hard_risk_max", "daily_loss_limit", "weekly_loss_limit",
                "drawdown_limit", "portfolio_risk_limit", "max_orders_session", "consecutive_loss_limit",
                "loss_cooldown_minutes", "emergency_close_attempts", "max_spread", "max_slippage_points",
                "commission_per_lot", "slippage_price", "swap_long_per_lot_day", "swap_short_per_lot_day",
                "poll_seconds", "session_file"}
    if set(settings_changes) - editable:
        raise ValueError("Setup includes an unsupported or private setting")
    if not strategy.strip():
        raise ValueError("Choose a strategy")
    broker_values = {"mode": mode, "account_login": account_login, "account_server": account_server.strip(),
                     "symbol": symbol.strip(), "mt5_path": mt5_path.strip() or None if mt5_path else None}
    values = {**current.model_dump(), **broker_values, **settings_changes, "active_strategy": strategy}
    validated = Settings(**values)
    auto_mode = validated.auto_execution_mode
    if execution_mode not in ("SIGNAL_ONLY", auto_mode):
        raise ValueError(f"Choose Signal only or {auto_mode}")

    store.set("broker_override", {**store.get("broker_override", {}), **broker_values})
    store.set("settings_override", {**store.get("settings_override", {}), **settings_changes})
    store.set("selected_strategy", strategy)
    account_key = f"{broker_values['account_server']}:{account_login}"
    store.set(f"execution_mode:{account_key}", execution_mode)
    # Setup only records the user's preferred mode. It never grants order permission.
    store.set(f"armed:{account_key}", {"active": False})
    store.set("telegram_preferences", telegram_preferences)
    store.set("setup_complete", True)
    store.set("setup_completed_at", utcnow().isoformat())
    return validated


def save_telegram_credentials(token: str, chat_id: str, env_path: Path | None = None) -> None:
    """Write Telegram secrets locally without displaying or committing them."""
    if not token.strip() or not chat_id.strip():
        raise ValueError("Enter both the Telegram bot token and destination chat ID")
    if any(character in token + chat_id for character in "\r\n"):
        raise ValueError("Telegram credentials must each fit on one line")
    env_path = env_path or Path(os.environ.get("AURUM_SETTINGS_FILE", ".env"))
    assignments = {"XAU_TELEGRAM_TOKEN": token.strip(), "XAU_TELEGRAM_CHAT_ID": chat_id.strip()}
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    pending = dict(assignments)
    output = []
    for line in lines:
        name, separator, _ = line.partition("=")
        if separator and name.strip() in pending:
            output.append(f'{name.strip()}="{pending.pop(name.strip())}"')
        else:
            output.append(line)
    output.extend(f'{name}="{value}"' for name, value in pending.items())
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("\n".join(output) + "\n", encoding="utf-8")
