import pytest

from aiogram.exceptions import TelegramBadRequest

from app.constants import PhotoType, Scenario
from app.models import InspectionPhoto, InspectionSession
from app.publisher import build_summary, publish_to_fp


class FakeMessage:
    def __init__(self, message_id: int):
        self.message_id = message_id


class FakeBot:
    def __init__(self, fail_media_groups: bool = False):
        self.calls = []
        self.fail_media_groups = fail_media_groups

    async def send_photo(self, **kwargs):
        self.calls.append(("send_photo", kwargs))
        return FakeMessage(10)

    async def send_media_group(self, **kwargs):
        self.calls.append(("send_media_group", kwargs))
        if self.fail_media_groups:
            raise TelegramBadRequest(method=None, message="Bad Request: too many messages to send as an album")
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
        plate_normalized="О917НХ797",
        has_damage=False,
        driver_has_remarks=False,
        body_score=4,
        tech_score=5,
        wrap_score=4,
        photos=photos,
    )


def test_summary_transfer_uses_sdal_marker():
    summary = build_summary(make_inspection([]))
    assert summary.startswith("О917НХ797 сдал")


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


def test_summary_supports_mixed_tire_type():
    inspection = make_inspection([])
    inspection.tire_type = "mixed"
    inspection.tire_score = 4
    summary = build_summary(inspection)
    assert "Тип: разная (зима/лето)" in summary


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
    assert message_id == 13
    assert bot.calls[0][0] == "send_media_group"
    assert bot.calls[1][0] == "send_message"
    assert all(call[0] != "forward_message" for call in bot.calls)


@pytest.mark.asyncio
async def test_publish_splits_more_than_ten_photos_into_media_groups():
    inspection = make_inspection(
        [
            InspectionPhoto(
                id=index,
                photo_type=PhotoType.DAMAGE.value,
                telegram_file_id=f"p{index}",
                telegram_file_unique_id=f"pu{index}",
            )
            for index in range(1, 13)
        ]
    )
    bot = FakeBot()
    message_id = await publish_to_fp(bot, inspection, 1001905865504)

    assert message_id == 13
    assert [call[0] for call in bot.calls] == ["send_media_group", "send_media_group", "send_message"]
    assert len(bot.calls[0][1]["media"]) == 10
    assert len(bot.calls[1][1]["media"]) == 2


@pytest.mark.asyncio
async def test_publish_sends_text_even_if_media_group_fails():
    inspection = make_inspection(
        [
            InspectionPhoto(id=1, photo_type=PhotoType.PLATE.value, telegram_file_id="p1", telegram_file_unique_id="pu1"),
            InspectionPhoto(id=2, photo_type=PhotoType.DAMAGE.value, telegram_file_id="p2", telegram_file_unique_id="pu2"),
        ]
    )
    bot = FakeBot(fail_media_groups=True)
    message_id = await publish_to_fp(bot, inspection, 1001905865504)

    assert message_id == 13
    assert [call[0] for call in bot.calls] == ["send_media_group", "send_message"]
