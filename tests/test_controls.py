from app.handlers import (
    dashboard_photo_required,
    inspection_control_button,
    plate_photo_required,
    tire_photo_required,
)


class FakeMessage:
    def __init__(self) -> None:
        self.answers = []

    async def answer(self, *args, **kwargs) -> None:
        self.answers.append((args, kwargs))


async def test_back_button_is_handled_while_waiting_for_required_photos(monkeypatch) -> None:
    calls = []

    async def fake_handle_control_text(message, state):
        calls.append((message, state))
        return True

    monkeypatch.setattr("app.handlers._handle_control_text", fake_handle_control_text)
    state = object()

    for handler in (plate_photo_required, dashboard_photo_required, tire_photo_required):
        message = FakeMessage()
        await handler(message, state)
        assert message.answers == []

    assert len(calls) == 3


async def test_reply_keyboard_controls_are_delegated_first(monkeypatch) -> None:
    calls = []

    async def fake_handle_control_text(message, state):
        calls.append((message, state))
        return True

    monkeypatch.setattr("app.handlers._handle_control_text", fake_handle_control_text)
    message = FakeMessage()
    state = object()

    await inspection_control_button(message, state)

    assert calls == [(message, state)]
    assert message.answers == []
