from __future__ import annotations

from datetime import UTC, datetime

from aiogram import Bot
from aiogram.types import InputMediaPhoto, Message

from app.constants import DTP_LABELS, PhotoType, SCENARIO_MARKERS, SCORE_FIELDS, TIRE_TYPES, Scenario
from app.keyboards import plate_correction_keyboard
from app.models import InspectionSession
from app.utils import display_plate, user_display


def build_summary(inspection: InspectionSession) -> str:
    scenario = Scenario(inspection.scenario)
    plate = display_plate(inspection.plate_normalized or inspection.plate_raw)
    marker = SCENARIO_MARKERS[scenario]
    dt = inspection.completed_at or inspection.updated_at or inspection.created_at or datetime.now(UTC)
    lines = [
        f"{plate} {marker}",
        "",
        f"Дата осмотра: {dt:%d.%m.%Y %H:%M}",
        f"Осмотр провёл: {user_display(inspection.telegram_username, inspection.telegram_name)}",
    ]
    if scenario == Scenario.ACCIDENT:
        lines += ["", f"ДТП: {DTP_LABELS.get(inspection.dtp_driver_guilty or '', 'требуется уточнение')}"]

    if scenario == Scenario.TIRES:
        lines += [
            "",
            "Резина:",
            f"Тип: {TIRE_TYPES.get(inspection.tire_type or '', inspection.tire_type or '-')}",
        ]
        if inspection.tire_score is not None:
            lines.append(f"Состояние: {inspection.tire_score}/5")
        if inspection.tire_comment:
            lines.append(f"Комментарий: {inspection.tire_comment}")
        return "\n".join(lines)

    score_lines: list[str] = []
    for prefix, title in SCORE_FIELDS:
        score = getattr(inspection, f"{prefix}_score")
        comment = getattr(inspection, f"{prefix}_comment")
        if score is not None:
            score_lines.append(f"{title}: {score}/5")
            if comment:
                score_lines.append(f"Комментарий: {comment}")
    if score_lines:
        lines += ["", "Оценки:", *score_lines]

    if scenario in {Scenario.RETURN, Scenario.TRANSFER}:
        lines += ["", "Замечания водителя:"]
        if inspection.driver_has_remarks:
            lines.append(inspection.driver_remarks_comment or "")
        else:
            lines[-1] = "Замечания водителя: нет"

    if inspection.tire_type or inspection.tire_score is not None:
        lines += [
            "",
            "Резина:",
            f"Тип: {TIRE_TYPES.get(inspection.tire_type or '', inspection.tire_type or '-')}",
        ]
        if inspection.tire_score is not None:
            lines.append(f"Состояние: {inspection.tire_score}/5")
        if inspection.tire_comment:
            lines.append(f"Комментарий: {inspection.tire_comment}")

    lines += ["", "Повреждения:"]
    if inspection.has_damage:
        lines.append(inspection.damage_description or "")
    else:
        lines[-1] = "Повреждения: нет"
    return "\n".join(lines)


async def publish_to_fp(bot: Bot, inspection: InspectionSession, fp_chat_id: int) -> int | None:
    photos = sorted(inspection.photos, key=lambda photo: photo.id)
    summary = build_summary(inspection)
    media_photos = [
        photo
        for photo in photos
        if photo.photo_type
        in {PhotoType.PLATE.value, PhotoType.DASHBOARD.value, PhotoType.DAMAGE.value, PhotoType.TIRE.value}
    ]

    if len(media_photos) == 1:
        await bot.send_photo(
            chat_id=fp_chat_id,
            photo=media_photos[0].telegram_file_id,
        )
    elif len(media_photos) > 1:
        media = [InputMediaPhoto(media=photo.telegram_file_id) for photo in media_photos]
        await bot.send_media_group(chat_id=fp_chat_id, media=media)
    message = await bot.send_message(
        chat_id=fp_chat_id,
        text=summary,
        reply_markup=plate_correction_keyboard(inspection.id),
    )
    return message.message_id if message else None
