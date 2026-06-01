from app.keyboards import reset_confirm_keyboard, staff_reply_keyboard


def test_staff_keyboard_has_reset_back_forward():
    keyboard = staff_reply_keyboard()
    labels = [button.text for row in keyboard.keyboard for button in row]
    assert labels == ["Сбросить осмотр", "Назад", "Вперёд"]


def test_reset_requires_inline_confirmation():
    keyboard = reset_confirm_keyboard()
    labels = [button.text for row in keyboard.inline_keyboard for button in row]
    assert labels == ["Да, сбросить", "Нет, оставить"]
