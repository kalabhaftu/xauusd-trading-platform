from scripts.launcher import _worker_environment


def test_packaged_workers_get_independent_pyinstaller_runtime(monkeypatch):
    monkeypatch.delenv("PYINSTALLER_RESET_ENVIRONMENT", raising=False)

    assert _worker_environment(True)["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert "PYINSTALLER_RESET_ENVIRONMENT" not in _worker_environment(False)
