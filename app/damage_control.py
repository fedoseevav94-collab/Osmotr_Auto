from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from datetime import UTC, datetime, time, timedelta
from html import escape
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.config import Settings
from app.constants import Scenario
from app.db import session_scope
from app.models import BotUser, DamageControlCase, InspectionSession

logger = logging.getLogger(__name__)
router = Router()

CHARGE_REQUIRED = "DAMAGE_CHARGE_REQUIRED"
NO_CHARGE_REQUIRED = "DAMAGE_NO_CHARGE_REQUIRED"
WAITING_MANAGER_ACTION = "WAITING_MANAGER_ACTION"
WAITING_CLOSE_COMMENT = "WAITING_CLOSE_COMMENT"
WAITING_PAYMENT_AMOUNT = "WAITING_PAYMENT_AMOUNT"
WAITING_SERVICE_AMOUNT = "WAITING_SERVICE_AMOUNT"
SERVICE_AMOUNT_RECEIVED = "SERVICE_AMOUNT_RECEIVED"
CLOSED_NO_CHARGE_REQUIRED = "CLOSED_NO_CHARGE_REQUIRED"
ESCALATED = "ESCALATED_TO_SUPERVISOR"

FINAL_STATUSES = {
    "CLOSED_PAID_CASH",
    "CLOSED_BALANCE_CHARGED",
    "CLOSED_INSTALLMENT",
    "CLOSED_PERIODIC_CHARGES",
    "CLOSED_TRANSFERRED_TO_OFFICE",
    "CLOSED_NO_CHARGE_REQUIRED",
    "CLOSED_NO_CHARGE_WITH_REASON",
    ESCALATED,
}

OFFICE_START = time(9, 30)
OFFICE_END = time(18, 30)
NEXT_DAY_REMINDER_TIME = time(9, 45)

WEEKDAY_ALIASES = {
    "mon": 0,
    "monday": 0,
    "пн": 0,
    "tue": 1,
    "tuesday": 1,
    "вт": 1,
    "wed": 2,
    "wednesday": 2,
    "ср": 2,
    "thu": 3,
    "thursday": 3,
    "чт": 3,
    "fri": 4,
    "friday": 4,
    "пт": 4,
    "sat": 5,
    "saturday": 5,
    "сб": 5,
    "sun": 6,
    "sunday": 6,
    "вс": 6,
}

PAYMENT_TYPE_LABELS = {
    "installment_1c": "Рассрочка в 1С",
    "cashbox": "Наличка (касса)",
    "kasko_franchise": "КАСКО (Франшиза)",
    "qr": "Оплата по QR коду",
    "terminal": "Оплата по терминалу",
}

PAYMENT_TYPE_CLOSE_STATUS = {
    "installment_1c": "CLOSED_INSTALLMENT",
    "cashbox": "CLOSED_PAID_CASH",
    "kasko_franchise": "CLOSED_TRANSFERRED_TO_OFFICE",
    "qr": "CLOSED_PAID_CASH",
    "terminal": "CLOSED_PAID_CASH",
}


def register_damage_control(dp) -> None:
    dp.include_router(router)


async def damage_control_loop(
    bot: Bot,
    sessionmaker: async_sessionmaker,
    settings: Settings,
) -> None:
    while True:
        try:
            await process_due_damage_control(bot, sessionmaker, settings)
        except Exception:
            logger.exception("Damage control loop failed")
        await asyncio.sleep(30)


async def start_damage_control_for_inspection(
    bot: Bot,
    session: AsyncSession,
    inspection: InspectionSession,
    settings: Settings,
) -> DamageControlCase | None:
    if not inspection.has_damage or not inspection.fp_chat_id or not inspection.fp_message_id:
        return None
    if inspection.scenario == Scenario.ISSUE.value:
        return None

    existing = await session.scalar(
        select(DamageControlCase).where(DamageControlCase.inspection_id == inspection.id)
    )
    if existing:
        return existing

    now = _utcnow()
    category = _case_category(inspection)
    delay = _first_delay_minutes(inspection, settings)
    case = DamageControlCase(
        inspection_id=inspection.id,
        status=WAITING_MANAGER_ACTION,
        category=category,
        plate_normalized=inspection.plate_normalized,
        fp_chat_id=inspection.fp_chat_id,
        fp_message_id=inspection.fp_message_id,
        damage_description=inspection.damage_description,
        first_reminder_due_at=_first_due_at(now, delay, settings),
    )
    session.add(case)
    await session.flush()

    if category == CHARGE_REQUIRED:
        service_message = await _send_service_amount_request(bot, session, case, settings)
        case.service_requested_at = now
        if service_message:
            case.service_request_chat_id = service_message.chat.id
            case.service_request_message_id = service_message.message_id
        case.service_reminder_due_at = now + timedelta(
            minutes=settings.service_amount_reminder_interval_minutes
        )

    await _send_manager_prompt(bot, session, case, inspection, settings, reminder_number=None)
    await session.flush()
    return case


async def process_due_damage_control(
    bot: Bot,
    sessionmaker: async_sessionmaker,
    settings: Settings,
) -> None:
    now = _utcnow()
    async with session_scope(sessionmaker) as session:
        due_cases = await session.scalars(
            select(DamageControlCase)
            .where(
                DamageControlCase.status.not_in(FINAL_STATUSES),
                (
                    (DamageControlCase.first_reminder_due_at.is_not(None))
                    & (DamageControlCase.first_reminder_due_at <= now)
                )
                | (
                    (DamageControlCase.service_reminder_due_at.is_not(None))
                    & (DamageControlCase.service_reminder_due_at <= now)
                    & (DamageControlCase.service_received_at.is_(None))
                ),
            )
            .options(selectinload(DamageControlCase.inspection))
        )
        for case in due_cases:
            if case.status in FINAL_STATUSES:
                continue
            if case.service_reminder_due_at and case.service_reminder_due_at <= now and not case.service_received_at:
                service_message = await _send_service_amount_request(bot, session, case, settings)
                case.service_requested_at = now
                if service_message:
                    case.service_request_chat_id = service_message.chat.id
                    case.service_request_message_id = service_message.message_id
                case.service_reminder_due_at = now + timedelta(
                    minutes=settings.service_amount_reminder_interval_minutes
                )
            if (
                case.first_reminder_due_at
                and case.first_reminder_due_at <= now
                and case.status not in {WAITING_CLOSE_COMMENT, WAITING_PAYMENT_AMOUNT}
            ):
                if case.reminders_sent >= settings.max_reminders:
                    await _escalate(bot, session, case, settings)
                    continue
                case.reminders_sent += 1
                await _send_manager_prompt(
                    bot,
                    session,
                    case,
                    case.inspection,
                    settings,
                    reminder_number=case.reminders_sent,
                )
                case.last_reminder_at = now
                case.first_reminder_due_at = now + timedelta(minutes=settings.reminder_interval_minutes)


@router.callback_query(F.data.startswith("damage_control:"))
async def damage_control_callback(callback: CallbackQuery) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) not in {3, 4}:
        await callback.answer("Не понял действие.", show_alert=True)
        return
    action = parts[1]
    payment_type = parts[2] if len(parts) == 4 else None
    case_id_raw = parts[3] if len(parts) == 4 else parts[2]
    if not case_id_raw.isdigit():
        await callback.answer("Не понял действие.", show_alert=True)
        return
    case_id = int(case_id_raw)
    settings = _settings_from_callback(callback)
    async with session_scope(settings.sessionmaker) as session:
        case = await session.scalar(
            select(DamageControlCase)
            .where(DamageControlCase.id == case_id)
            .options(selectinload(DamageControlCase.inspection))
        )
        if not case or case.status in FINAL_STATUSES:
            await callback.answer("Осмотр уже закрыт или не найден.", show_alert=True)
            return
        if case.status in {WAITING_CLOSE_COMMENT, WAITING_PAYMENT_AMOUNT}:
            await callback.answer("Уже жду данные по закрытию.", show_alert=True)
            return
        if action == "pay":
            await _ask_payment_type(callback.bot, case, callback.from_user.username)
            with contextlib.suppress(Exception):
                await callback.message.edit_reply_markup(reply_markup=None)
            await callback.answer()
            return
        if action == "paytype" and payment_type in PAYMENT_TYPE_LABELS:
            case.status = WAITING_PAYMENT_AMOUNT
            case.waiting_comment_user_id = callback.from_user.id
            case.waiting_comment_username = callback.from_user.username
            case.payment_type = payment_type
            await _ask_payment_amount(callback.bot, case, callback.from_user.username, payment_type)
            with contextlib.suppress(Exception):
                await callback.message.edit_reply_markup(reply_markup=None)
            await callback.answer()
            return
        if action == "nocharge":
            if case.category == CHARGE_REQUIRED:
                case.status = WAITING_CLOSE_COMMENT
                case.waiting_comment_user_id = callback.from_user.id
                case.waiting_comment_username = callback.from_user.username
                await _ask_no_charge_comment(callback.bot, case, callback.from_user.username)
                with contextlib.suppress(Exception):
                    await callback.message.edit_reply_markup(reply_markup=None)
                await callback.answer("Жду причину без списания.")
                return
            await _close_case(
                callback.bot,
                case,
                callback.from_user.full_name,
                callback.from_user.username,
                CLOSED_NO_CHARGE_REQUIRED,
                "Проверено, списание не требуется",
            )
            with contextlib.suppress(Exception):
                await callback.message.edit_reply_markup(reply_markup=None)
            await callback.answer("Закрыто.")
            return
    await callback.answer("Не понял действие.", show_alert=True)


@router.message(F.text)
async def damage_control_message(message: Message) -> None:
    settings = _settings_from_message(message)
    if not _same_chat(message.chat.id, settings.settings.fp_chat_id):
        return
    text = (message.text or "").strip()
    if not text or not message.from_user:
        return
    username = (message.from_user.username or "").lstrip("@").lower()

    async with session_scope(settings.sessionmaker) as session:
        if username == settings.settings.service_username.lstrip("@").lower():
            if await _record_service_response(session, message, text):
                return

        case = await session.scalar(
            select(DamageControlCase)
            .where(
                DamageControlCase.status == WAITING_CLOSE_COMMENT,
                DamageControlCase.waiting_comment_user_id == message.from_user.id,
            )
            .options(selectinload(DamageControlCase.inspection))
            .order_by(DamageControlCase.updated_at.desc(), DamageControlCase.id.desc())
        )
        if not case:
            case = await session.scalar(
                select(DamageControlCase)
                .where(
                    DamageControlCase.status == WAITING_PAYMENT_AMOUNT,
                    DamageControlCase.waiting_comment_user_id == message.from_user.id,
                )
                .options(selectinload(DamageControlCase.inspection))
                .order_by(DamageControlCase.updated_at.desc(), DamageControlCase.id.desc())
            )
            if not case:
                return
            amount = parse_payment_amount(text)
            if amount is None:
                await message.bot.send_message(
                    chat_id=message.chat.id,
                    text="Не увидел сумму. Напишите только сумму, например: 5000 или 5 тыс.",
                    reply_to_message_id=message.message_id,
                    allow_sending_without_reply=True,
                )
                return
            payment_type = case.payment_type or "cashbox"
            await _close_case(
                message.bot,
                case,
                message.from_user.full_name,
                message.from_user.username,
                PAYMENT_TYPE_CLOSE_STATUS.get(payment_type, "CLOSED_PAID_CASH"),
                f"{PAYMENT_TYPE_LABELS.get(payment_type, payment_type)}: {amount}",
                payment_type=PAYMENT_TYPE_LABELS.get(payment_type, payment_type),
                payment_amount=amount,
            )
            return
        close_type = classify_close_comment(text)
        if not close_type:
            await message.bot.send_message(
                chat_id=message.chat.id,
                text=(
                    "Комментарий не закрывает осмотр. Нужна оплата/списание/рассрочка/офис/"
                    "причина без списания."
                ),
                reply_to_message_id=message.message_id,
                allow_sending_without_reply=True,
            )
            return
        await _close_case(
            message.bot,
            case,
            message.from_user.full_name,
            message.from_user.username,
            close_type,
            text,
        )


def classify_close_comment(text: str) -> str | None:
    normalized = " ".join(text.lower().replace("ё", "е").split())
    if normalized in {"ок", "увидел", "принял", "посмотрю", "потом", "разберусь", "в работе"}:
        return None
    if any(marker in normalized for marker in ("рассроч", "поставили рассрочку", "рассрочка")):
        return "CLOSED_INSTALLMENT"
    if "периодичес" in normalized and "спис" in normalized:
        return "CLOSED_PERIODIC_CHARGES"
    if "передано в офис" in normalized or "передал в офис" in normalized:
        return "CLOSED_TRANSFERRED_TO_OFFICE"
    if "списание не требуется" in normalized or "без списания" in normalized:
        return "CLOSED_NO_CHARGE_WITH_REASON"
    if any(marker in normalized for marker in ("списал", "списали", "удержал", "удержали", "баланс")):
        return "CLOSED_BALANCE_CHARGED"
    if any(marker in normalized for marker in ("оплатил", "оплачено", "налич", "перевод", "через qr", " qr")):
        return "CLOSED_PAID_CASH"
    return None


def parse_payment_amount(text: str) -> int | None:
    normalized = text.lower().replace("ё", "е").replace(",", ".")
    multiplier = 1000 if re.search(r"\b(тыс|т\.?р|к)\b", normalized) else 1
    match = re.search(r"\d+(?:[\s.]?\d{3})*(?:\.\d+)?|\d+", normalized)
    if not match:
        return None
    raw = match.group(0).replace(" ", "")
    if "." in raw and len(raw.rsplit(".", 1)[1]) == 3:
        raw = raw.replace(".", "")
    try:
        value = float(raw)
    except ValueError:
        return None
    amount = int(value * multiplier)
    return amount if amount > 0 else None


def damage_control_keyboard(case_id: int, category: str) -> InlineKeyboardMarkup:
    if category == NO_CHARGE_REQUIRED:
        rows = [
            [
                InlineKeyboardButton(
                    text="🔎 Проверено без списания",
                    callback_data=f"damage_control:nocharge:{case_id}",
                )
            ]
        ]
    else:
        rows = [
            [InlineKeyboardButton(text="💰 Оплата / списание", callback_data=f"damage_control:pay:{case_id}")],
            [InlineKeyboardButton(text="☑️ Без списания", callback_data=f"damage_control:nocharge:{case_id}")],
        ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def payment_type_keyboard(case_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"damage_control:paytype:{key}:{case_id}",
                )
            ]
            for key, label in PAYMENT_TYPE_LABELS.items()
        ]
    )


def manager_prompt_text(
    case: DamageControlCase,
    inspection: InspectionSession,
    settings: Settings,
    reminder_number: int | None = None,
    mention_override: str | None = None,
) -> str:
    mentions = (
        mention_override
        if mention_override is not None
        else active_manager_mentions(settings.manager_days_off, _local_now(settings).weekday())
    )
    plate = case.plate_normalized or inspection.plate_normalized or inspection.plate_raw or "без номера"
    lines = []
    if mentions:
        lines += [mentions, ""]
    lines.append(f"Найдены повреждения по авто {plate}.")
    if case.category == NO_CHARGE_REQUIRED:
        lines.append("В отчете указано, что водитель не виноват. Подтвердите проверку без списания.")
    else:
        lines.append("Нужно выбрать действие после проверки.")
        lines += ["", f"@{settings.service_username} уже запрошен для оценки/суммы."]
    if reminder_number is not None:
        lines += ["", f"Напоминание {reminder_number}/{settings.max_reminders}."]
    return "\n".join(lines)


def service_amount_request_text(case: DamageControlCase) -> str:
    plate = case.plate_normalized or "без номера"
    return (
        f"Нужна оценка/сумма по повреждению авто {plate}.\n"
        "Ответьте reply к этому сообщению."
    )


def active_manager_mentions(raw_days_off: str, weekday: int) -> str:
    usernames = active_manager_usernames(raw_days_off, weekday)
    return " ".join(f"@{username}" for username in usernames) if usernames else "Менеджеры"


def active_manager_usernames(raw_days_off: str, weekday: int) -> list[str]:
    days_off = parse_manager_days_off(raw_days_off)
    if not days_off:
        return ["pagorodu", "Wuggfi", "lalalas19", "serb_98"]
    return [username for username, off_days in days_off.items() if weekday not in off_days]


def parse_manager_days_off(raw: str) -> dict[str, set[int]]:
    result: dict[str, set[int]] = {}
    for block in raw.split(";"):
        if not block.strip() or ":" not in block:
            continue
        username, days = block.split(":", 1)
        parsed_days = {
            WEEKDAY_ALIASES[item.strip().lower()]
            for item in days.split(",")
            if item.strip().lower() in WEEKDAY_ALIASES
        }
        result[username.strip().lstrip("@").lower()] = parsed_days
    return result


def _case_category(inspection: InspectionSession) -> str:
    if inspection.scenario == Scenario.ACCIDENT.value and inspection.dtp_driver_guilty == "not_guilty":
        return NO_CHARGE_REQUIRED
    return CHARGE_REQUIRED


def _first_delay_minutes(inspection: InspectionSession, settings: Settings) -> int:
    if inspection.scenario in {Scenario.RETURN.value, Scenario.TRANSFER.value}:
        return settings.reminder_first_delay_minutes
    return settings.fp_manager_response_delay_minutes


def _first_due_at(created_at: datetime, delay_minutes: int, settings: Settings) -> datetime:
    tz = ZoneInfo(settings.office_timezone)
    local_created = created_at.replace(tzinfo=UTC).astimezone(tz)
    regular_due = local_created + timedelta(minutes=delay_minutes)
    if OFFICE_START <= local_created.time() <= OFFICE_END and regular_due.time() <= OFFICE_END:
        return regular_due.astimezone(UTC).replace(tzinfo=None)
    current_date = local_created.date() + timedelta(days=1)
    for _ in range(14):
        candidate = datetime.combine(current_date, NEXT_DAY_REMINDER_TIME, tzinfo=tz)
        mentions = active_manager_mentions(settings.manager_days_off, candidate.weekday())
        if mentions and mentions != "Менеджеры":
            return candidate.astimezone(UTC).replace(tzinfo=None)
        current_date += timedelta(days=1)
    return datetime.combine(local_created.date() + timedelta(days=1), NEXT_DAY_REMINDER_TIME, tzinfo=tz).astimezone(
        UTC
    ).replace(tzinfo=None)


async def _send_service_amount_request(
    bot: Bot,
    session: AsyncSession,
    case: DamageControlCase,
    settings: Settings,
):
    return await _send_fp_fallback(
        bot,
        case,
        f"@{settings.service_username} нужна оценка/сумма по повреждению. Ответьте reply к этому сообщению.",
    )


async def _send_manager_prompt(
    bot: Bot,
    session: AsyncSession,
    case: DamageControlCase,
    inspection: InspectionSession,
    settings: Settings,
    reminder_number: int | None,
) -> None:
    await _send_fp_fallback(
        bot,
        case,
        manager_prompt_text(case, inspection, settings, reminder_number),
        reply_markup=damage_control_keyboard(case.id, case.category),
    )


async def _ask_close_comment(bot: Bot, case: DamageControlCase, username: str | None) -> None:
    mention = f"@{username.lstrip('@')}" if username else "Менеджер"
    await _send_case_followup(
        bot,
        case,
        (
            f"{mention} выбрал оплату/списание.\n"
            "Напишите комментарий по закрытию.\n\n"
            "Примеры:\n"
            "— оплатил 20000 наличными\n"
            "— списали 15000 с баланса\n"
            "— поставили рассрочку 30000\n"
            "— передано в офис\n"
            "— списание не требуется, причина"
        ),
    )


async def _ask_payment_type(bot: Bot, case: DamageControlCase, username: str | None) -> None:
    mention = f"@{username.lstrip('@')}" if username else "Менеджер"
    await _send_case_followup(
        bot,
        case,
        f"{mention}, выберите тип оплаты/списания.",
        reply_markup=payment_type_keyboard(case.id),
    )


async def _ask_payment_amount(
    bot: Bot,
    case: DamageControlCase,
    username: str | None,
    payment_type: str,
) -> None:
    mention = f"@{username.lstrip('@')}" if username else "Менеджер"
    label = PAYMENT_TYPE_LABELS.get(payment_type, payment_type)
    await _send_case_followup(
        bot,
        case,
        f"{mention}, тип: {label}.\nНапишите сумму списания/оплаты одним сообщением.",
    )


async def _ask_no_charge_comment(bot: Bot, case: DamageControlCase, username: str | None) -> None:
    mention = f"@{username.lstrip('@')}" if username else "Менеджер"
    await _send_case_followup(
        bot,
        case,
        (
            f"{mention} выбрал закрытие без списания.\n"
            "Напишите причину без списания.\n\n"
            "Примеры:\n"
            "— списание не требуется, причина: повреждение старое\n"
            "— без списания, причина: повреждение уже было\n"
            "— передано в офис"
        ),
    )


async def _record_service_response(session: AsyncSession, message: Message, text: str) -> bool:
    case: DamageControlCase | None = None
    if message.reply_to_message:
        case = await session.scalar(
            select(DamageControlCase)
            .where(
                (
                    (DamageControlCase.fp_chat_id == message.chat.id)
                    | (DamageControlCase.service_request_chat_id == message.chat.id)
                ),
                (
                    (DamageControlCase.fp_message_id == message.reply_to_message.message_id)
                    | (DamageControlCase.service_request_message_id == message.reply_to_message.message_id)
                ),
                DamageControlCase.status.not_in(FINAL_STATUSES),
            )
            .order_by(DamageControlCase.created_at.desc())
        )
    if case is None:
        return False
    if case.service_received_at:
        return True
    case.service_received_at = _utcnow()
    if case.status == WAITING_SERVICE_AMOUNT:
        case.status = SERVICE_AMOUNT_RECEIVED
    await message.bot.send_message(
        chat_id=case.fp_chat_id,
        text=f"Получил ответ от @{message.from_user.username or 'Norblacksmith'} по оценке/сумме повреждения.",
        reply_to_message_id=case.fp_message_id,
        allow_sending_without_reply=False,
    )
    logger.info("Service amount response for inspection damage case %s: %s", case.id, text)
    return True


async def user_id_by_username(session: AsyncSession, username: str | None) -> int | None:
    normalized = (username or "").strip().lstrip("@").lower()
    if not normalized:
        return None
    return await session.scalar(
        select(BotUser.telegram_user_id)
        .where(func.lower(BotUser.telegram_username) == normalized)
        .order_by(BotUser.updated_at.desc(), BotUser.id.desc())
        .limit(1)
    )


async def _send_fp_fallback(bot: Bot, case: DamageControlCase, text: str, reply_markup=None):
    return await bot.send_message(
        chat_id=case.fp_chat_id,
        text=text,
        reply_markup=reply_markup,
        reply_to_message_id=case.fp_message_id,
        allow_sending_without_reply=False,
    )


async def _send_case_followup(bot: Bot, case: DamageControlCase, text: str, reply_markup=None):
    return await _send_fp_fallback(bot, case, text, reply_markup=reply_markup)


async def _close_case(
    bot: Bot,
    case: DamageControlCase,
    actor_name: str,
    actor_username: str | None,
    close_type: str,
    comment: str,
    payment_type: str | None = None,
    payment_amount: int | None = None,
) -> None:
    now = _utcnow()
    case.status = close_type
    case.close_type = close_type
    case.close_comment = comment
    case.payment_type = payment_type
    case.payment_amount = payment_amount
    case.closed_at = now
    case.first_reminder_due_at = None
    case.service_reminder_due_at = None
    await bot.send_message(
        chat_id=case.fp_chat_id,
        text=close_summary_text(case, actor_name, actor_username, comment),
        reply_to_message_id=case.fp_message_id,
        allow_sending_without_reply=False,
    )


def close_summary_text(
    case: DamageControlCase,
    actor_name: str,
    actor_username: str | None,
    comment: str,
) -> str:
    username = f" (@{actor_username})" if actor_username else ""
    plate = case.plate_normalized or case.inspection.plate_normalized or case.inspection.plate_raw or "без номера"
    dt = case.inspection.completed_at or case.inspection.updated_at or case.created_at
    action = "проверку без списания"
    if case.close_type != CLOSED_NO_CHARGE_REQUIRED:
        action = "оплату/списание по повреждениям"
    if case.payment_type and case.payment_amount:
        return (
            f"Сотрудник {actor_name}{username} зафиксировал {action} на авто {plate}.\n"
            f"Дата и время осмотра: {dt:%d.%m.%Y %H:%M}\n"
            f"Тип оплаты: {PAYMENT_TYPE_LABELS.get(case.payment_type, case.payment_type)}\n"
            f"Сумма: {case.payment_amount}"
        )
    return (
        f"Сотрудник {actor_name}{username} зафиксировал {action} на авто {plate}.\n"
        f"Дата и время осмотра: {dt:%d.%m.%Y %H:%M}\n"
        f"Комментарий: {comment}"
    )


async def _escalate(bot: Bot, session: AsyncSession, case: DamageControlCase, settings: Settings) -> None:
    case.status = ESCALATED
    case.escalated_at = _utcnow()
    case.first_reminder_due_at = None
    plate = case.plate_normalized or case.inspection.plate_normalized or case.inspection.plate_raw or "без номера"
    text = (
        "Повреждение после осмотра не закрыто после 3 напоминаний.\n\n"
        f"Авто: {plate}\n"
        f"Статус: {case.status}"
    )
    supervisor_id = await user_id_by_username(session, settings.supervisor_username)
    if supervisor_id:
        try:
            await bot.send_message(chat_id=supervisor_id, text=text)
            return
        except TelegramAPIError:
            logger.exception("Failed to send escalation to @%s", settings.supervisor_username)
    await bot.send_message(
        chat_id=case.fp_chat_id,
        text=f"@{settings.supervisor_username}\n\n{text}",
        reply_to_message_id=case.fp_message_id,
        allow_sending_without_reply=False,
    )


def _same_chat(first: int, second: int) -> bool:
    values = {str(first), str(second)}
    normalized = {value[1:] if value.startswith("-") else value for value in values}
    return len(normalized) == 1


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _local_now(settings: Settings) -> datetime:
    return datetime.now(ZoneInfo(settings.office_timezone))


class _RuntimeSettings:
    def __init__(self, settings: Settings, sessionmaker: async_sessionmaker) -> None:
        self.settings = settings
        self.sessionmaker = sessionmaker


_RUNTIME: _RuntimeSettings | None = None


def setup_damage_control(settings: Settings, sessionmaker: async_sessionmaker) -> None:
    global _RUNTIME
    _RUNTIME = _RuntimeSettings(settings, sessionmaker)


def _settings_from_callback(callback: CallbackQuery) -> _RuntimeSettings:
    if _RUNTIME is None:
        raise RuntimeError("Damage control is not configured")
    return _RUNTIME


def _settings_from_message(message: Message) -> _RuntimeSettings:
    if _RUNTIME is None:
        raise RuntimeError("Damage control is not configured")
    return _RUNTIME
