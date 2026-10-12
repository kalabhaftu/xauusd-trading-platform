from app.execution.journal import trade_journal


def test_journal_joins_deals_and_signal_metadata(store):
    account = "SYNTHETIC_TEST:123"
    store.record("broker_deals", {"ticket": 1, "position_id": 99, "magic": 7, "entry": 0,
                                 "type": 0, "time": 1000, "price": 2000, "volume": .02,
                                 "commission": -1, "symbol": "XAUUSD", "comment": "signal"}, account)
    store.record("broker_deals", {"ticket": 2, "position_id": 99, "magic": 7, "entry": 1,
                                 "type": 1, "time": 1060, "price": 2010, "volume": .02,
                                 "profit": 20, "commission": -1, "swap": -.5}, account)
    from sqlalchemy.dialects.sqlite import insert

    from app.database.models import intents
    with store.engine.begin() as conn:
        conn.execute(insert(intents).values(key="synthetic-intent", account=account, state="FILLED",
                                            timestamp="2024-01-01T00:00:00+00:00",
                                            payload={"comment": "signal", "signal": {"strategy": "test",
                                                     "stop_loss": 1990, "take_profit": 2020,
                                                     "rationale": "synthetic fixture"}}))
    row = trade_journal(store, account, 7)[0]
    assert row["realized_net_pnl"] == 17.5 and row["holding_seconds"] == 60
    assert row["status"] == "CLOSED" and row["strategy"] == "test"
    assert trade_journal(store, account, None, 7)[0]["owner"] == "Bot"


def test_account_journal_groups_manual_trades_and_sums_full_position_costs(store):
    account = "SYNTHETIC_TEST:123"
    store.record("broker_deals", {"ticket": 1, "position_id": 99, "magic": 0, "entry": 0,
                                 "type": 0, "time": 1000, "price": 2000, "volume": .01,
                                 "symbol": "XAUUSD", "comment": ""}, account)
    store.record("broker_deals", {"ticket": 2, "position_id": 99, "magic": 0, "entry": 1,
                                 "type": 1, "time": 1060, "price": 1990, "volume": .01,
                                 "profit": -10, "commission": -.2, "swap": -.1, "fee": 0,
                                 "symbol": "XAUUSD", "comment": ""}, account)
    row = trade_journal(store, account, None)[0]
    assert row["owner"] == "Manual / unrecognized"
    assert row["realized_net_pnl"] == -10.3
    assert row["status"] == "CLOSED"


def test_bot_journal_keeps_manual_close_deal_in_bot_position_lifecycle(store):
    account = "SYNTHETIC_TEST:123"
    store.record("broker_deals", {"ticket": 10, "position_id": 111, "magic": 7, "entry": 0,
                                 "type": 0, "time": 1000, "price": 2000, "volume": .01,
                                 "commission": -.1, "symbol": "XAUUSD", "comment": "bot"}, account)
    store.record("broker_deals", {"ticket": 11, "position_id": 111, "magic": 0, "entry": 1,
                                 "type": 1, "time": 1060, "price": 1990, "volume": .01,
                                 "profit": -10, "commission": -.1, "symbol": "XAUUSD", "comment": ""}, account)

    row = trade_journal(store, account, 7)[0]

    assert row["owner"] == "Bot"
    assert row["realized_net_pnl"] == -10.2
    assert row["status"] == "CLOSED"
