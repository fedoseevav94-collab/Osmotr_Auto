from datetime import datetime

from app.keyboards import (
    BACK_BUTTON,
    FORWARD_BUTTON,
    RESET_BUTTON,
    START_BUTTON,
    START_COMMAND_BUTTON,
    charge_period_keyboard,
    reset_confirm_keyboard,
    driver_remarks_keyboard,
    export_period_keyboard,
    plate_choices_keyboard,
    problem_period_keyboard,
    scenario_keyboard,
    staff_idle_keyboard,
    staff_menu_keyboard,
    staff_reply_keyboard,
    start_keyboard,
    supervisor_menu_keyboard,
    tire_type_keyboard,
)
from app.handlers import charge_case_choices_keyboard
from app.models import DamageControlCase, InspectionSession


def _inline_labels(keyboard):
    return [button.text for row in keyboard.inline_keyboard for button in row]


def test_start_keyboard_chooses_role():
    assert _inline_labels(start_keyboard()) == ["🧰 Сотрудник осмотра", "👔 Руководитель"]


def test_staff_menu_has_inspection_and_drafts():
    assert _inline_labels(staff_menu_keyboard()) == [START_BUTTON, "📋 Мои черновики"]


def test_scenario_keyboard_has_distinct_icons():
    assert _inline_labels(scenario_keyboard()) == [
        "🏁 Сдача",
        "🔄 Пересадка",
        "🔑 Выдача авто",
        "🧾 Плановый осмотр",
        "🚨 Осмотр после ДТП",
    ]


def test_staff_idle_keyboard_has_start_button():
    keyboard = staff_idle_keyboard()
    labels = [button.text for row in keyboard.keyboard for button in row]
    assert labels == [START_BUTTON, START_COMMAND_BUTTON]


def test_supervisor_menu_has_management_actions_and_staff_mode():
    labels = _inline_labels(supervisor_menu_keyboard())
    assert "📊 Статистика за сегодня" in labels
    assert "📥 Выгрузить оценки" in labels
    assert "⚠️ Проблемные авто" in labels
    assert "💸 Выгрузить списания" in labels
    assert "✏️ Закрыть/исправить списание" in labels
    assert "🔓 Открытые повреждения" in labels
    assert "🛠️ Ждём сервис" in labels
    assert "🛞 Проверка резины" in labels
    assert "📌 Статус кампаний" in labels
    assert "🧰 Перейти в режим сотрудника" in labels


def test_export_period_keyboards_have_year_and_all_time() -> None:
    for keyboard in (export_period_keyboard(), problem_period_keyboard(), charge_period_keyboard()):
        labels = _inline_labels(keyboard)
        assert "📚 Текущий год" in labels
        assert "🗂️ За всё время" in labels


def test_staff_keyboard_hides_forward_by_default():
    keyboard = staff_reply_keyboard()
    labels = [button.text for row in keyboard.keyboard for button in row]
    assert labels == [RESET_BUTTON, BACK_BUTTON, START_COMMAND_BUTTON]


def test_staff_keyboard_shows_forward_when_available():
    keyboard = staff_reply_keyboard(can_forward=True)
    labels = [button.text for row in keyboard.keyboard for button in row]
    assert labels == [RESET_BUTTON, BACK_BUTTON, FORWARD_BUTTON, START_COMMAND_BUTTON]


def test_reset_requires_inline_confirmation():
    keyboard = reset_confirm_keyboard()
    assert _inline_labels(keyboard) == ["🛑 Да, сбросить", "↩️ Нет, оставить"]


def test_driver_remarks_keyboard_has_already_option():
    assert _inline_labels(driver_remarks_keyboard()) == ["✅ Да", "❌ Нет", "↩️ Указал ранее"]


def test_tire_type_keyboard_has_mixed_type():
    assert _inline_labels(tire_type_keyboard()) == ["❄️ Зимняя", "☀️ Летняя", "🔀 Разная (зима/лето)"]


def test_plate_choices_keyboard_hides_duplicate_plates():
    class Plate:
        def __init__(self, plate_normalized: str, brand: str = "Haval", model: str = "F7"):
            self.plate_normalized = plate_normalized
            self.brand = brand
            self.model = model

    labels = _inline_labels(
        plate_choices_keyboard(
            [
                Plate("Т500НТ797"),
                Plate("Т500НТ797"),
            ]
        )
    )

    assert labels == ["🚘 Т500НТ797 · Haval F7", "✍️ Ввести номер вручную"]


def test_charge_case_choices_show_date_scenario_and_status():
    case = DamageControlCase(
        id=25,
        status="CLOSED_INSTALLMENT",
        inspection=InspectionSession(completed_at=datetime(2026, 7, 21, 10, 39), scenario="Сдача"),
    )

    labels = _inline_labels(charge_case_choices_keyboard([case]))

    assert labels == ["#25 · 21.07 10:39 · Сдача · закрыто"]
