import pytest

from app.constants import PhotoType, Scenario
from app.models import InspectionPhoto, InspectionSession
from app.publisher import build_summary, publish_to_fp


class FakeMessage:
    def __init__(self, message_id: int):
        self.message_id = message_id


class FakeBot:
    def __init__(self):
        self.calls = []

    async def send_photo(self, **kwargs):
        self.calls.append(("send_photo", kwargs))
        return FakeMessage(10)

    async def send_media_group(self, **kwargs):
        self.calls.append(("send_media_group", kwargs))
        return [FakeMessage(11), FakeMessage(12)]

    async def send_message(self, **kwargs):
        self.calls.append(("send_message", kwargs))
        return FakeMessage(13)

    async def forward_message(self, **kwargs):  # pragma: no cover
        raise AssertionError("forward_message must not be used")


def make_inspection(photos: list[InspectionPhoto]) -> InspectionSession:
    return InspectionSession(
        id=1,
        telegram_user_id=1,
        telegram_username="inspector",
        scenario=Scenario.TRANSFER.value,
        status="COMPLETED",
        plate_normalized="O917HX797",
        has_damage=False,
        driver_has_remarks=False,
        body_score=4,
        tech_score=5,
        wrap_score=4,
        photos=photos,
    )


def test_summary_transfer_uses_sdal_marker():
    summary = build_summary(make_inspection([]))
    assert summary.startswith("O917HX797 сдал")


def test_summary_includes_tire_block_when_present():
    inspection = make_inspection([])
    inspection.tire_type = "winter"
    inspection.tire_score = 3
    inspection.tire_comment = "Износ протектора"
    summary = build_summary(inspection)
    assert "Резина:" in summary
    assert "Тип: зимняя" in summary
    assert "Состояние: 3/5" in summary
    assert "Комментарий: Износ протектора" in summary


def test_summary_includes_driver_remarks_for_return_scenarios():
    inspection = make_inspection([])
    inspection.driver_has_remarks = True
    inspection.driver_remarks_comment = "Водитель сообщил о шуме"
    summary = build_summary(inspection)
    assert "Замечания водителя:" in summary
    assert "Водитель сообщил о шуме" in summary


@pytest.mark.asyncio
async def test_publish_uses_send_methods_not_forward():
    inspection = make_inspection(
        [
            InspectionPhoto(id=1, photo_type=PhotoType.PLATE.value, telegram_file_id="p", telegram_file_unique_id="pu"),
            InspectionPhoto(id=2, photo_type=PhotoType.DASHBOARD.value, telegram_file_id="d", telegram_file_unique_id="du"),
        ]
    )
    bot = FakeBot()
    message_id = await publish_to_fp(bot, inspection, 1001905865504)
    assert message_id == 11
    assert bot.calls[0][0] == "send_media_group"
    assert all(call[0] != "forward_message" for call in bot.calls)
