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
    problem_period_keyboard,
    scenario_keyboard,
    staff_idle_keyboard,
    staff_menu_keyboard,
    staff_reply_keyboard,
    start_keyboard,
    supervisor_menu_keyboard,
)


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
