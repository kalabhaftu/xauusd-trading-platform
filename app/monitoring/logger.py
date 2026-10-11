import json
import logging
import re
from pathlib import Path

from app.domain import utcnow


class RedactingFormatter(logging.Formatter):
    def __init__(self, secrets: list[str]):
        super().__init__()
        self.secrets = [s for s in secrets if s]

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if record.exc_info:
            message += " " + self.formatException(record.exc_info)
        for secret in self.secrets:
            message = message.replace(secret, "[REDACTED]")
        message = re.sub(r"(?i)(password|token|authorization)([\s=:]+)[^\s,;]+", r"\1\2[REDACTED]", message)
        return json.dumps({"timestamp": utcnow().isoformat(), "level": record.levelname,
                           "logger": record.name, "message": message})


def configure(settings) -> None:
    Path("logs").mkdir(exist_ok=True)
    from logging.handlers import RotatingFileHandler
    handlers: list[logging.Handler] = [logging.StreamHandler(), RotatingFileHandler("logs/platform.jsonl", maxBytes=5_000_000,
                                                            backupCount=5, encoding="utf-8")]
    secrets = [value.get_secret_value() for value in
               (settings.account_password, settings.telegram_token, settings.telegram_chat_id) if value]
    for handler in handlers:
        handler.setFormatter(RedactingFormatter(secrets))
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)
    # HTTP client DEBUG/INFO records can contain Telegram token URLs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
