import pytest

from app.broker.mt5_client import BrokerError, MT5Broker


@pytest.mark.parametrize("path", [None, r"C:\Program Files\MetaTrader 5\terminal64.exe"])
def test_mt5_initialize_omits_empty_path_and_reports_invalid_parameters(
    settings, monkeypatch, path
):
    class FakeMT5:
        def __init__(self):
            self.calls = []

        def initialize(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return False

        @staticmethod
        def last_error():
            return (-2, "Invalid parameters")

    fake = FakeMT5()
    monkeypatch.setattr("app.broker.mt5_client.importlib.import_module", lambda _: fake)
    broker = MT5Broker(settings.model_copy(update={"mt5_path": path}))

    with pytest.raises(BrokerError, match="invalid parameters"):
        broker.connect()

    args, kwargs = fake.calls[0]
    if path is None:
        assert args == ()
    else:
        assert args == (path,)
    assert "path" not in kwargs
    assert kwargs["login"] == settings.account_login
    assert kwargs["server"] == settings.account_server
