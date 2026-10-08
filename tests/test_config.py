from app.config import int_env, optional_int_env


def test_optional_int_env_ignores_username(monkeypatch) -> None:
    monkeypatch.setenv("SUPERVISOR_TELEGRAM_ID", "=Fedos_AV")

    assert optional_int_env("SUPERVISOR_TELEGRAM_ID") is None


def test_optional_int_env_reads_numeric_value(monkeypatch) -> None:
    monkeypatch.setenv("SUPERVISOR_TELEGRAM_ID", "123456789")

    assert optional_int_env("SUPERVISOR_TELEGRAM_ID") == 123456789


def test_int_env_uses_safe_default_for_invalid_or_out_of_range(monkeypatch) -> None:
    monkeypatch.setenv("REMINDER_INTERVAL_MINUTES", "abc")
    assert int_env("REMINDER_INTERVAL_MINUTES", 30, minimum=1) == 30

    monkeypatch.setenv("REMINDER_INTERVAL_MINUTES", "0")
    assert int_env("REMINDER_INTERVAL_MINUTES", 30, minimum=1) == 30

    monkeypatch.setenv("PLATE_AUDIT_HOUR", "24")
    assert int_env("PLATE_AUDIT_HOUR", 4, minimum=0, maximum=23) == 4
