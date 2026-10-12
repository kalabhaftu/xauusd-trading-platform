from app.config.user_setup import save_user_setup


def test_gui_setup_persists_auto_preference_without_arming(settings, store):
    result = save_user_setup(
        store, settings, mode="DEMO", account_login=12345, account_server="Exness-Demo",
        symbol="XAUUSDm", mt5_path=None,
        settings_changes={"risk_per_trade": .0025, "max_spread": .5,
                          "session_file": None}, strategy="ict_sweep_mss_fvg", execution_mode="AUTO_DEMO",
        telegram_preferences={"enabled": True, "events": ["signal", "position_update"],
                              "position_update_minutes": 15},
    )

    assert result.mode == "DEMO"
    assert store.get("execution_mode:Exness-Demo:12345") == "AUTO_DEMO"
    assert store.get("setup_complete") is True
    assert store.get("armed:Exness-Demo:12345") == {"active": False}
    assert store.get("telegram_preferences")["position_update_minutes"] == 15


def test_gui_setup_requires_matching_auto_mode(settings, store):
    import pytest

    with pytest.raises(ValueError, match="Choose Signal only"):
        save_user_setup(
            store, settings, mode="DEMO", account_login=12345, account_server="Exness-Demo",
            symbol="XAUUSDm", mt5_path=None, settings_changes={}, strategy="ict_sweep_mss_fvg",
            execution_mode="AUTO_LIVE", telegram_preferences={},
        )
    assert store.get("setup_complete") is None
