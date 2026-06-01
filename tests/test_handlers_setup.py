from app.config import Settings
from app.db import make_engine, make_sessionmaker
from app.handlers import _settings, _start_mode_for_user, setup_router


def test_setup_router_stores_settings_without_router_item_assignment(tmp_path):
    settings = Settings(
        bot_token="token",
        fp_chat_id=-1001905865504,
        database_url="sqlite+aiosqlite:///:memory:",
        supervisor_username="fedos_av",
        inspection_staff_usernames=set(),
        data_dir=tmp_path / "data",
        vehicle_plates_xlsx=None,
    )
    engine = make_engine(settings.database_url)
    setup_router(settings, make_sessionmaker(engine))

    assert _settings() is settings
    assert settings.data_dir.exists()


def test_start_mode_role_selection_only_for_supervisor(tmp_path):
    settings = Settings(
        bot_token="token",
        fp_chat_id=-1001905865504,
        database_url="sqlite+aiosqlite:///:memory:",
        supervisor_username="fedos_av",
        inspection_staff_usernames=set(),
        data_dir=tmp_path / "data",
        vehicle_plates_xlsx=None,
    )
    engine = make_engine(settings.database_url)
    setup_router(settings, make_sessionmaker(engine))

    assert _start_mode_for_user("Fedos_AV") == "supervisor"
    assert _start_mode_for_user("ordinary_staff") == "staff"
    assert _start_mode_for_user(None) == "staff"
