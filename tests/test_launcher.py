from scripts import launcher


def test_packaged_workers_get_independent_pyinstaller_runtime(monkeypatch):
    monkeypatch.delenv("PYINSTALLER_RESET_ENVIRONMENT", raising=False)

    assert launcher._worker_environment(True)["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert "PYINSTALLER_RESET_ENVIRONMENT" not in launcher._worker_environment(False)


def test_packaged_setup_is_isolated_from_development_environment(monkeypatch, tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    profile = tmp_path / "profile"
    monkeypatch.setattr(launcher, "is_packaged", lambda: True)
    monkeypatch.setattr(launcher, "runtime_root", lambda: bundle)
    monkeypatch.setenv("LOCALAPPDATA", str(profile))
    monkeypatch.setenv("AURUM_SETTINGS_FILE", str(tmp_path / "project.env"))
    monkeypatch.setenv("XAU_ACCOUNT_LOGIN", "12345")
    monkeypatch.setenv("XAU_TELEGRAM_TOKEN", "must-not-leak")
    monkeypatch.setenv("XAU_DATABASE_URL", "sqlite:///project/data.db")
    monkeypatch.setenv("STREAMLIT_GLOBAL_DEVELOPMENT_MODE", "true")
    monkeypatch.setenv("STREAMLIT_SERVER_PORT", "8501")

    root, data = launcher.configure_packaged_paths()

    assert root == bundle
    assert data == profile / "Aurum"
    assert launcher.os.environ["AURUM_SETTINGS_FILE"] == str(data / "settings.env")
    assert launcher.os.environ["XAU_DATABASE_URL"] == f"sqlite:///{(data / 'data' / 'platform.db').as_posix()}"
    assert "XAU_ACCOUNT_LOGIN" not in launcher.os.environ
    assert "XAU_TELEGRAM_TOKEN" not in launcher.os.environ
    assert "STREAMLIT_GLOBAL_DEVELOPMENT_MODE" not in launcher.os.environ
    assert "STREAMLIT_SERVER_PORT" not in launcher.os.environ


def test_dashboard_ports_are_separate_for_packaged_and_development_runs():
    assert launcher.dashboard_port(True) == 8502
    assert launcher.dashboard_port(False) == 8501


def test_streamlit_runs_in_production_mode_and_on_its_isolated_port(tmp_path):
    packaged = launcher.dashboard_arguments(tmp_path, True)
    development = launcher.dashboard_arguments(tmp_path, False)

    assert packaged[packaged.index("--global.developmentMode") + 1] == "false"
    assert packaged[packaged.index("--server.port") + 1] == "8502"
    assert development[development.index("--server.port") + 1] == "8501"


def test_launcher_routes_unconfigured_user_to_setup():
    assert not launcher.has_saved_account(None)
    assert not launcher.has_saved_account({"configured_account": None})
    assert not launcher.has_saved_account({"configured_account": {"login": 123, "server": "  "}})
    assert launcher.has_saved_account({"configured_account": {"login": 123, "server": "Exness-Demo"}})
