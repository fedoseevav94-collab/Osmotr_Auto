from app.handlers import (
    _human_actor_for_finish,
    _set_state,
    _set_inspection_actor,
    back_button,
    dashboard_photo_required,
    forward_button,
    inspection_control_button,
    plate_photo_required,
    tire_photo_required,
)
from app.models import InspectionSession
from app.states import InspectionFlow


class FakeMessage:
    def __init__(self) -> None:
        self.answers = []

    async def answer(self, *args, **kwargs) -> None:
        self.answers.append((args, kwargs))


class FakeState:
    def __init__(self) -> None:
        self.current = None
        self.data = {}

    async def get_state(self):
        return self.current

    async def set_state(self, value) -> None:
        self.current = getattr(value, "state", value)

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kwargs) -> None:
        self.data.update(kwargs)


class FakeUser:
    def __init__(self, user_id: int, username: str | None, full_name: str, is_bot: bool = False) -> None:
        self.id = user_id
        self.username = username
        self.full_name = full_name
        self.is_bot = is_bot


class FakeActorMessage:
    def __init__(self, user: FakeUser) -> None:
        self.from_user = user


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


async def test_back_and_forward_keep_step_history(monkeypatch) -> None:
    rendered = []

    async def fake_render_current_step(message, state, state_value):
        rendered.append(state_value)

    monkeypatch.setattr("app.handlers._render_current_step", fake_render_current_step)
    message = FakeMessage()
    state = FakeState()

    await _set_state(state, InspectionFlow.plate_digits)
    await _set_state(state, InspectionFlow.plate_select)
    await _set_state(state, InspectionFlow.plate_photo)

    await back_button(message, state)
    assert state.current == InspectionFlow.plate_select.state
    assert rendered[-1] == InspectionFlow.plate_select.state

    await back_button(message, state)
    assert state.current == InspectionFlow.plate_digits.state
    assert rendered[-1] == InspectionFlow.plate_digits.state

    await forward_button(message, state)
    assert state.current == InspectionFlow.plate_select.state
    assert rendered[-1] == InspectionFlow.plate_select.state


def test_inspection_actor_replaces_bot_identity() -> None:
    inspection = InspectionSession(
        telegram_user_id=8733424067,
        telegram_username="Osmotr_auto_STAX_bot",
        telegram_name="STAX Осмотр",
        status="DRAFT",
    )

    _set_inspection_actor(inspection, 123, "real_inspector", "Real Inspector")

    assert inspection.telegram_user_id == 123
    assert inspection.telegram_username == "real_inspector"
    assert inspection.telegram_name == "Real Inspector"


def test_finish_actor_does_not_fall_back_to_bot_message_user() -> None:
    inspection = InspectionSession(
        telegram_user_id=123,
        telegram_username=None,
        telegram_name="Андрей",
        status="DRAFT",
    )
    message = FakeActorMessage(FakeUser(999, "Osmotr_auto_STAX_bot", "STAX Осмотр", is_bot=True))

    actor_id, username, full_name = _human_actor_for_finish({}, inspection, message)

    assert actor_id == 123
    assert username is None
    assert full_name == "Андрей"
