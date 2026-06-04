from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.db import session_scope
from app.models import InspectionSession
from app.repository import InspectionRepository

MOSCOW_TZ = ZoneInfo("Europe/Moscow")
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlateAuditItem:
    inspection_id: int
    plate: str
    recognized: str | None
    status: str
    link: str


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
    logger.info("Plate photo recognition report is disabled by business rule")
    return []


async def audit_plate_photos(
    bot: Bot,
    sessionmaker: async_sessionmaker,
    start: datetime,
    end: datetime,
) -> list[PlateAuditItem]:
    logger.info("Plate photo recognition audit is disabled by business rule")
    return []


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
            lines.append(
                f"#{item.inspection_id}: указано {item.plate}, на фото похоже {item.recognized}"
                f"{_link_suffix(item)}"
            )
    if unresolved:
        labels = {
            "no_photo": "нет фото госномера",
            "download_failed": "не удалось скачать фото",
            "unrecognized": "номер на фото не распознан",
        }
        lines.append("")
        lines.append("Не удалось проверить:")
        for item in unresolved[:20]:
            lines.append(f"#{item.inspection_id}: {item.plate} — {labels.get(item.status, item.status)}{_link_suffix(item)}")
    return "\n".join(lines)


async def _audit_recipient_chat_id(sessionmaker: async_sessionmaker, settings: Settings) -> int | None:
    if settings.supervisor_telegram_id:
        return settings.supervisor_telegram_id
    async with session_scope(sessionmaker) as session:
        repo = InspectionRepository(session)
        return await repo.latest_user_id_by_username(settings.supervisor_username)


def _inspection_link(inspection: InspectionSession) -> str:
    if not inspection.fp_chat_id or not inspection.fp_message_id:
        return ""
    chat = str(inspection.fp_chat_id)
    if chat.startswith("-100"):
        chat = chat[4:]
    elif chat.startswith("100"):
        chat = chat[3:]
    return f"https://t.me/c/{chat}/{inspection.fp_message_id}"


def _link_suffix(item: PlateAuditItem) -> str:
    return f"\n   Осмотр: {item.link}" if item.link else ""


def _split_telegram_message(text: str, limit: int = 3800) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in text.splitlines():
        line_len = len(line) + 1
        if current and current_len + line_len > limit:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += line_len
    if current:
        chunks.append("\n".join(current))
    return chunks or [text]


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
