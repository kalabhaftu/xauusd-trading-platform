import os
from datetime import UTC, datetime

import pandas as pd
from sqlalchemy.dialects.sqlite import insert

from app.database.models import intents


def test_candle_cleanup_is_limited_to_selected_source_symbol_and_timeframe(store):
    index = pd.DatetimeIndex([pd.Timestamp("2024-01-01", tz="UTC"), pd.Timestamp("2025-01-01", tz="UTC")])
    frame = pd.DataFrame({"open": [1, 2], "high": [2, 3], "low": [0, 1], "close": [1, 2]}, index=index)
    store.save_candles(frame, "demo-source", "XAUUSD", "M15")
    store.save_candles(frame, "demo-source", "XAUUSD", "H1")
    store.save_candles(frame, "other-source", "XAUUSD", "M15")

    cutoff = "2025-01-01T00:00:00+00:00"
    assert store.count_candles("demo-source", "XAUUSD", ["M15"], cutoff) == 1
    assert store.clear_candles("demo-source", "XAUUSD", ["M15"], cutoff) == 1
    assert len(store.load_candles("demo-source", "XAUUSD", "M15")) == 1
    assert len(store.load_candles("demo-source", "XAUUSD", "H1")) == 2
    assert len(store.load_candles("other-source", "XAUUSD", "M15")) == 2


def test_research_cleanup_keeps_market_data_and_holdout_guard(store, tmp_path):
    store.record("backtest_results", {"summary_report": str(tmp_path / "reports" / "run")})
    store.record("experiments", {"campaign": "synthetic"})
    store.record("approvals", {"approved": False})
    store.set("approval:old-config", {"approved": True})
    store.set("holdout_consumed:dataset-hash", {"campaign": "keep"})

    preview = store.research_cleanup_preview()
    result = store.clear_research_history()

    assert sum(preview["counts"].values()) == 3
    assert result["report_paths"] == preview["report_paths"]
    assert sum(len(store.list(table, 100)) for table in ("backtest_results", "experiments", "approvals")) == 0
    assert store.get("approval:old-config") is None
    assert store.get("holdout_consumed:dataset-hash") == {"campaign": "keep"}


def test_data_cleanup_detects_running_research_and_retires_crashed_worker(store):
    store.set(f"research_job:{os.getpid()}", {"state": "RUNNING", "pid": os.getpid(), "command": "fixture"})
    store.set("research_job:99999999", {"state": "RUNNING", "pid": 99999999, "command": "crashed fixture"})

    active = store.active_research_jobs()

    assert [job["pid"] for job in active] == [os.getpid()]
    assert store.get("research_job:99999999")["state"] == "FAILED"


def test_account_history_cleanup_preserves_open_exposure_intents_and_halt(store):
    account = "Exness-Demo:123"
    store.set("active_account", {"server": "Exness-Demo", "login": 123, "environment": "DEMO"})
    store.set(f"reconciliation:{account}", {"positions": [{"ticket": 88}], "safe": False})
    store.set(f"halt:{account}", {"active": True, "reason": "fixture"})
    store.set(f"armed:{account}", {"active": True})
    store.record("broker_deals", {"ticket": 1, "position_id": 77, "time": 1704067200,
                                  "entry": 0, "profit": 0}, account)
    store.record("broker_deals", {"ticket": 2, "position_id": 77, "time": 1704067260,
                                  "entry": 1, "profit": -3}, account)
    store.record("broker_deals", {"ticket": 3, "position_id": 88, "time": 1704067200,
                                  "entry": 0, "profit": 0}, account)
    store.record("broker_deals", {"ticket": 4, "position_id": 99, "time": 1791748457,
                                  "entry": 0, "profit": 1000}, account)
    store.set(f"signal_outcome:{account}:closed", {"status": "CLOSED", "signal_time": "2024-01-01T00:00:00+00:00"})
    store.set(f"signal_outcome:{account}:open", {"status": "OPEN", "signal_time": "2024-01-01T00:00:00+00:00"})
    with store.engine.begin() as conn:
        conn.execute(insert(intents).values(key="unresolved", account=account, state="UNKNOWN",
            timestamp="2024-01-01T00:00:00+00:00", payload={"synthetic": True}))

    cutoff = datetime(2025, 1, 1, tzinfo=UTC)
    preview = store.account_history_cleanup_preview(account, cutoff)
    result = store.clear_account_history(account, cutoff)

    remaining = [row["payload"]["ticket"] for row in store.list("broker_deals", 100, account)]
    assert preview["counts"]["broker_deals"] == 2
    assert set(remaining) == {3, 4}
    assert result["open_positions_kept"] == 1
    assert store.get(f"signal_outcome:{account}:closed") is None
    assert store.get(f"signal_outcome:{account}:open")["status"] == "OPEN"
    assert store.get(f"halt:{account}")["active"]
    assert store.get(f"armed:{account}")["active"]
    assert store.list("order_intents", 10, account)[0]["state"] == "UNKNOWN"
    assert store.get(f"history_hidden_before:{account}") == cutoff.isoformat()


def test_account_history_cleanup_refuses_non_current_account(store):
    store.set("active_account", {"server": "Exness-Demo", "login": 123})
    try:
        store.clear_account_history("Other-Demo:456", "2025-01-01T00:00:00+00:00")
    except ValueError as exc:
        assert "currently verified MT5 account" in str(exc)
    else:
        raise AssertionError("cleanup must remain account scoped")
