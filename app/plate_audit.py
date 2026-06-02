from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.constants import PhotoType
from app.db import session_scope
from app.models import InspectionSession
from app.ocr import recognize_plate_from_image
from app.repository import InspectionRepository

MOSCOW_TZ = ZoneInfo("Europe/Moscow")
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlateAuditItem:
    inspection_id: int
    plate: str
    recognized: str | None
    status: str


async def run_plate_audit_scheduler(
    bot: Bot,
    sessionmaker: async_sessionmaker,
    settings: Settings,
) -> None:
    if not settings.plate_audit_enabled:
        logger.info("Daily plate photo audit is disabled")
        return
    while True:
        await asyncio.sleep(_seconds_until_next_run(settings.plate_audit_hour))
        try:
            await run_daily_plate_audit(bot, sessionmaker, settings)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Daily plate photo audit failed: %s", exc)


async def run_daily_plate_audit(
    bot: Bot,
    sessionmaker: async_sessionmaker,
    settings: Settings,
    now: datetime | None = None,
) -> list[PlateAuditItem]:
    start, end = _previous_moscow_day_bounds(now)
    items = await audit_plate_photos(bot, sessionmaker, settings.data_dir / "plate_audit", start, end)
    await bot.send_message(
        chat_id=settings.plate_audit_chat_id or settings.fp_chat_id,
        text=build_plate_audit_report(items, start, end),
    )
    return items


async def audit_plate_photos(
    bot: Bot,
    sessionmaker: async_sessionmaker,
    output_dir: Path,
    start: datetime,
    end: datetime,
) -> list[PlateAuditItem]:
    output_dir.mkdir(parents=True, exist_ok=True)
    async with session_scope(sessionmaker) as session:
        repo = InspectionRepository(session)
        inspections = await repo.completed_with_photos_between(start, end)

    result: list[PlateAuditItem] = []
    for inspection in inspections:
        plate = inspection.plate_normalized or inspection.plate_raw or ""
        photo = next((item for item in inspection.photos if item.photo_type == PhotoType.PLATE.value), None)
        if photo is None:
            result.append(PlateAuditItem(inspection.id, plate or "без номера", None, "no_photo"))
            continue
        path = output_dir / f"inspection_{inspection.id}_plate.jpg"
        path.unlink(missing_ok=True)
        with contextlib.suppress(Exception):
            await bot.download(photo.telegram_file_id, destination=path)
        if not path.exists():
            result.append(PlateAuditItem(inspection.id, plate or "без номера", None, "download_failed"))
            continue
        recognized = recognize_plate_from_image(path)
        if recognized is None:
            result.append(PlateAuditItem(inspection.id, plate or "без номера", None, "unrecognized"))
        elif recognized == plate:
            result.append(PlateAuditItem(inspection.id, plate, recognized, "match"))
        else:
            result.append(PlateAuditItem(inspection.id, plate or "без номера", recognized, "mismatch"))
    return result


def build_plate_audit_report(items: list[PlateAuditItem], start: datetime, end: datetime) -> str:
    title = f"🔎 Сверка госномеров по фото за {start:%d.%m.%Y}"
    if not items:
        return f"{title}\nОсмотров за период не найдено."

    matched = sum(1 for item in items if item.status == "match")
    mismatches = [item for item in items if item.status == "mismatch"]
    unresolved = [item for item in items if item.status not in {"match", "mismatch"}]
    lines = [
        title,
        f"Период: {start:%d.%m.%Y %H:%M} - {end:%d.%m.%Y %H:%M}",
        f"Проверено: {len(items)}",
        f"Совпало: {matched}",
        f"Расхождения: {len(mismatches)}",
        f"Не удалось проверить: {len(unresolved)}",
    ]
    if mismatches:
        lines.append("")
        lines.append("Расхождения:")
        for item in mismatches[:20]:
            lines.append(f"#{item.inspection_id}: указано {item.plate}, на фото похоже {item.recognized}")
    if unresolved:
        labels = {
            "no_photo": "нет фото госномера",
            "download_failed": "не удалось скачать фото",
            "unrecognized": "номер на фото не распознан",
        }
        lines.append("")
        lines.append("Не удалось проверить:")
        for item in unresolved[:20]:
            lines.append(f"#{item.inspection_id}: {item.plate} — {labels.get(item.status, item.status)}")
    return "\n".join(lines)


def _previous_moscow_day_bounds(now: datetime | None = None) -> tuple[datetime, datetime]:
    current = now or datetime.now(MOSCOW_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=MOSCOW_TZ)
    today = current.astimezone(MOSCOW_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    start = today - timedelta(days=1)
    return start.replace(tzinfo=None), today.replace(tzinfo=None)


def _seconds_until_next_run(hour: int) -> float:
    now = datetime.now(MOSCOW_TZ)
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return max((target - now).total_seconds(), 1)
