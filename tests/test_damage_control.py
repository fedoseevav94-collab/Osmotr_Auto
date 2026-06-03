from datetime import UTC, datetime
from pathlib import Path

from app.config import Settings
from app.damage_control import (
    active_manager_mentions,
    classify_close_comment,
    manager_prompt_text,
    parse_payment_amount,
    payment_type_keyboard,
    parse_manager_days_off,
    _first_due_at,
)
from app.models import DamageControlCase, InspectionSession


def make_settings() -> Settings:
    return Settings(
        bot_token="token",
        fp_chat_id=-1001905865504,
        database_url="sqlite+aiosqlite:///bot.db",
        supervisor_username="fedos_av",
        inspection_staff_usernames=set(),
        data_dir=Path("/tmp"),
        vehicle_plates_xlsx=None,
        manager_days_off="pagorodu:thu,fri;Wuggfi:wed,thu;lalalas19:sat,sun;serb_98:sat,sun",
        office_timezone="Europe/Moscow",
    )


def test_active_manager_mentions_excludes_day_off() -> None:
    days_off = parse_manager_days_off("pagorodu:thu,fri;Wuggfi:wed,thu")

    assert days_off["pagorodu"] == {3, 4}
    assert active_manager_mentions("pagorodu:thu,fri;Wuggfi:wed,thu", 3) == "Менеджеры"
    assert active_manager_mentions("pagorodu:thu,fri;Wuggfi:wed,thu", 1) == "@pagorodu @wuggfi"


def test_close_comment_requires_real_action_keyword() -> None:
    assert classify_close_comment("5 тыс") is None
    assert classify_close_comment("ок") is None
    assert classify_close_comment("оплатил 5 тыс наличными") == "CLOSED_PAID_CASH"
    assert classify_close_comment("списали 15000 с баланса") == "CLOSED_BALANCE_CHARGED"
    assert classify_close_comment("поставили рассрочку 30000") == "CLOSED_INSTALLMENT"


def test_payment_amount_parsing() -> None:
    assert parse_payment_amount("5000") == 5000
    assert parse_payment_amount("5 000") == 5000
    assert parse_payment_amount("5.000") == 5000
    assert parse_payment_amount("5 тыс") == 5000
    assert parse_payment_amount("ок") is None


def test_payment_type_keyboard_uses_business_labels() -> None:
    labels = [button.text for row in payment_type_keyboard(1).inline_keyboard for button in row]

    assert labels == [
        "Рассрочка в 1С",
        "Наличка (касса)",
        "КАСКО (Франшиза)",
        "Оплата по QR коду",
        "Оплата по терминалу",
        "Списание с депозита",
    ]


def test_manager_prompt_is_short_and_does_not_duplicate_description() -> None:
    settings = make_settings()
    inspection = InspectionSession(
        id=1,
        telegram_user_id=1,
        status="COMPLETED",
        scenario="Плановый осмотр",
        plate_normalized="B751CX797",
        has_damage=True,
        damage_description="Переднее крыло помято",
    )
    case = DamageControlCase(
        id=2,
        inspection_id=1,
        status="WAITING_MANAGER_ACTION",
        category="DAMAGE_CHARGE_REQUIRED",
        plate_normalized="B751CX797",
        fp_chat_id=-1001905865504,
        fp_message_id=123,
        damage_description="Переднее крыло помято",
    )

    text = manager_prompt_text(case, inspection, settings)

    assert "Найдены повреждения по авто B751CX797" in text
    assert "Авто:" not in text
    assert "Описание и фото" not in text
    assert "Переднее крыло" not in text


def test_manager_prompt_says_service_answer_received() -> None:
    settings = make_settings()
    inspection = InspectionSession(
        id=1,
        telegram_user_id=1,
        status="COMPLETED",
        scenario="Сдача",
        plate_normalized="M671HM797",
        has_damage=True,
        damage_description="Вмятина",
    )
    case = DamageControlCase(
        id=2,
        inspection_id=1,
        status="WAITING_MANAGER_ACTION",
        category="DAMAGE_CHARGE_REQUIRED",
        plate_normalized="M671HM797",
        fp_chat_id=-1001905865504,
        fp_message_id=123,
        damage_description="Вмятина",
        service_received_at=datetime(2026, 6, 3, 6, 52),
        service_amount=10000,
    )

    text = manager_prompt_text(case, inspection, settings, reminder_number=1)

    assert "Оценка/сумма от @Norblacksmith получена: 10000." in text
    assert "уже запрошен" not in text


def test_first_due_moves_late_report_to_next_working_morning() -> None:
    settings = make_settings()
    created_at = datetime(2026, 6, 2, 15, 50, tzinfo=UTC)

    due = _first_due_at(created_at.replace(tzinfo=None), 45, settings)

    assert due == datetime(2026, 6, 3, 6, 45)
