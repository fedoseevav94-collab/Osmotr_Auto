from app.config import optional_int_env


def test_optional_int_env_ignores_username(monkeypatch) -> None:
    monkeypatch.setenv("SUPERVISOR_TELEGRAM_ID", "=Fedos_AV")

    assert optional_int_env("SUPERVISOR_TELEGRAM_ID") is None


def test_optional_int_env_reads_numeric_value(monkeypatch) -> None:
    monkeypatch.setenv("SUPERVISOR_TELEGRAM_ID", "123456789")

    assert optional_int_env("SUPERVISOR_TELEGRAM_ID") == 123456789
