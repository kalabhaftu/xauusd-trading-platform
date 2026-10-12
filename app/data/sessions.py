import json
from datetime import UTC, datetime, timedelta
from pathlib import Path


class SessionSchedule:
    """Read-only exporter timestamps are UTC; broker session seconds use its observed offset."""
    def __init__(self, path: Path | None, symbol: str, server: str):
        if path is None or not path.is_file():
            raise ValueError("A fresh broker session export is required for automation")
        self.data = json.loads(path.read_text(encoding="utf-8-sig"))
        if self.data["symbol"] != symbol or self.data["server"] != server:
            raise ValueError("Session export does not match broker/symbol")
        self.generated = datetime.fromtimestamp(self.data["generated_utc"], UTC)

    def is_open(self, now: datetime) -> bool:
        age = (now - self.generated).total_seconds()
        if age < -60 or age > 86400:
            raise ValueError("Broker session export stale; re-export after offset/schedule changes")
        local = now + timedelta(seconds=self.data["server_offset_seconds"])
        weekday = (local.weekday() + 1) % 7  # MQL Sunday=0
        second = local.hour * 3600 + local.minute * 60 + local.second
        for session in self.data["sessions"]:
            beginning, ending = session["start"], session["end"]
            if ending <= beginning:
                ending += 86400
            if session["day"] == weekday and beginning <= second < ending:
                return True
            if session["day"] == (weekday - 1) % 7 and beginning <= second + 86400 < ending:
                return True
        return False
