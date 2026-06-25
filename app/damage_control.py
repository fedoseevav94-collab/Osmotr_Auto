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
from app.utils import display_plate

logger = logging.getLogger(__name__)
router = Router()

CHARGE_REQUIRED = "DAMAGE_CHARGE_REQUIRED"
NO_CHARGE_REQUIRED = "DAMAGE_NO_CHARGE_REQUIRED"
WAITING_MANAGER_ACTION = "WAITING_MANAGER_ACTION"
WAITING_CLOSE_COMMENT = "WAITING_CLOSE_COMMENT"
WAITING_DRIVER_NAME = "WAITING_DRIVER_NAME"
WAITING_PAYMENT_TYPE = "WAITING_PAYMENT_TYPE"
WAITING_DISPATCHER_COMMENT = "WAITING_DISPATCHER_COMMENT"
WAITING_PAYMENT_AMOUNT = "WAITING_PAYMENT_AMOUNT"
WAITING_PAYMENT_CORRECTION = "WAITING_PAYMENT_CORRECTION"
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
    "deposit": "Списание с депозита",
    "dispatcher_balance": "С баланса диспетчерской",
    "split_payment": "Раздельная оплата",
}

PAYMENT_TYPE_CLOSE_STATUS = {
    "installment_1c": "CLOSED_INSTALLMENT",
    "cashbox": "CLOSED_PAID_CASH",
    "kasko_franchise": "CLOSED_TRANSFERRED_TO_OFFICE",
    "qr": "CLOSED_PAID_CASH",
    "terminal": "CLOSED_PAID_CASH",
    "deposit": "CLOSED_BALANCE_CHARGED",
    "dispatcher_balance": "CLOSED_BALANCE_CHARGED",
    "split_payment": "CLOSED_PAID_CASH",
}

NO_CHARGE_PAYMENT_TYPE = "Без списания"


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
                await _send_service_private_reminder(bot, session, case, settings)
                service_message = await _send_service_amount_request(bot, session, case, settings)
                case.service_requested_at = now
                if service_message:
                    case.service_request_chat_id = service_message.chat.id
                    case.service_request_message_id = service_message.message_id
                case.service_reminder_due_at = now + timedelta(
                    minutes=settings.service_amount_reminder_interval_minutes
                )
            if case.first_reminder_due_at and case.first_reminder_due_at <= now:
                if case.reminders_sent >= settings.max_reminders:
                    await _escalate(bot, session, case, settings)
                    continue
                case.reminders_sent += 1
                if case.status in {
                    WAITING_CLOSE_COMMENT,
                    WAITING_DRIVER_NAME,
                    WAITING_PAYMENT_TYPE,
                    WAITING_DISPATCHER_COMMENT,
                    WAITING_PAYMENT_AMOUNT,
                    WAITING_PAYMENT_CORRECTION,
                }:
                    await _send_pending_closure_reminder(bot, case, settings, case.reminders_sent)
                else:
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
                if case.reminders_sent >= settings.max_reminders:
                    await _escalate(bot, session, case, settings)


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
        if not case:
            await callback.answer("Осмотр уже закрыт или не найден.", show_alert=True)
            return
        if action == "edit_charge":
            if case.payment_amount is None:
                await callback.answer("В этом осмотре нет суммы списания для исправления.", show_alert=True)
                return
            runtime = settings.settings
            is_supervisor = (callback.from_user.username or "").lstrip("@").lower() == runtime.supervisor_username.lower()
            if case.waiting_comment_user_id and case.waiting_comment_user_id != callback.from_user.id and not is_supervisor:
                await callback.answer("Исправить может руководитель или сотрудник, который закрыл списание.", show_alert=True)
                return
            case.status = WAITING_PAYMENT_CORRECTION
            case.waiting_comment_user_id = callback.from_user.id
            case.waiting_comment_username = callback.from_user.username
            await _ask_payment_correction(callback.bot, case, callback.from_user.username)
            await callback.answer()
            return
        if case.status in FINAL_STATUSES:
            await callback.answer("Осмотр уже закрыт или не найден.", show_alert=True)
            return
        if action == "back":
            await _handle_back(callback, case)
            return
        waiting_error = _waiting_callback_error(case, action, payment_type, callback.from_user.id)
        if waiting_error:
            await callback.answer(waiting_error, show_alert=True)
            return
        if action == "pay":
            case.status = WAITING_DRIVER_NAME
            case.waiting_comment_user_id = callback.from_user.id
            case.waiting_comment_username = callback.from_user.username
            await _ask_driver_name(callback.bot, case, callback.from_user.username)
            with contextlib.suppress(Exception):
                await callback.message.edit_reply_markup(reply_markup=None)
            await callback.answer()
            return
        if action == "paytype" and payment_type in PAYMENT_TYPE_LABELS:
            case.waiting_comment_user_id = callback.from_user.id
            case.waiting_comment_username = callback.from_user.username
            case.payment_type = payment_type
            if payment_type == "dispatcher_balance":
                case.status = WAITING_DISPATCHER_COMMENT
                await _ask_dispatcher_comment(callback.bot, case, callback.from_user.username)
            else:
                case.status = WAITING_PAYMENT_AMOUNT
                await _ask_payment_amount(callback.bot, case, callback.from_user.username, payment_type)
            with contextlib.suppress(Exception):
                await callback.message.edit_reply_markup(reply_markup=None)
            await callback.answer()
            return
        if action == "nocharge":
            if case.category == CHARGE_REQUIRED:
                case.status = WAITING_DRIVER_NAME
                case.waiting_comment_user_id = callback.from_user.id
                case.waiting_comment_username = callback.from_user.username
                case.payment_type = NO_CHARGE_PAYMENT_TYPE
                await _ask_driver_name(callback.bot, case, callback.from_user.username)
                with contextlib.suppress(Exception):
                    await callback.message.edit_reply_markup(reply_markup=None)
                await callback.answer("Жду ФИО водителя.")
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


async def _handle_back(callback: CallbackQuery, case: DamageControlCase) -> None:
    if case.waiting_comment_user_id and case.waiting_comment_user_id != callback.from_user.id:
        await callback.answer("Назад может нажать менеджер, который начал закрытие.", show_alert=True)
        return
    username = callback.from_user.username
    if case.status == WAITING_DRIVER_NAME:
        case.status = WAITING_MANAGER_ACTION
        case.driver_name = None
        case.payment_type = None
        case.payment_amount = None
        case.close_comment = None
        await _send_case_followup(
            callback.bot,
            case,
            "Вернул к выбору действия.",
            reply_markup=damage_control_keyboard(case.id, case.category),
        )
    elif case.status == WAITING_PAYMENT_TYPE:
        case.status = WAITING_DRIVER_NAME
        case.driver_name = None
        case.payment_type = None
        case.payment_amount = None
        case.close_comment = None
        await _ask_driver_name(callback.bot, case, username)
    elif case.status in {WAITING_DISPATCHER_COMMENT, WAITING_PAYMENT_AMOUNT}:
        case.status = WAITING_PAYMENT_TYPE
        case.payment_type = None
        case.payment_amount = None
        case.close_comment = None
        await _ask_payment_type(callback.bot, case, username)
    elif case.status == WAITING_PAYMENT_CORRECTION:
        case.status = case.close_type or PAYMENT_TYPE_CLOSE_STATUS.get(case.payment_type or "", "CLOSED_PAID_CASH")
        case.waiting_comment_user_id = None
        case.waiting_comment_username = None
        await _send_case_followup(callback.bot, case, "Исправление списания отменено.")
    elif case.status == WAITING_CLOSE_COMMENT and case.payment_type == NO_CHARGE_PAYMENT_TYPE:
        case.status = WAITING_MANAGER_ACTION
        case.driver_name = None
        case.payment_type = None
        case.payment_amount = None
        case.close_comment = None
        await _send_case_followup(
            callback.bot,
            case,
            "Вернул к выбору действия.",
            reply_markup=damage_control_keyboard(case.id, case.category),
        )
    else:
        await callback.answer("Сейчас некуда возвращаться.", show_alert=True)
        return
    with contextlib.suppress(Exception):
        await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("Вернул назад.")


def _waiting_callback_error(
    case: DamageControlCase,
    action: str,
    payment_type: str | None,
    user_id: int,
) -> str | None:
    if case.status == WAITING_PAYMENT_TYPE and action == "paytype" and payment_type in PAYMENT_TYPE_LABELS:
        if case.waiting_comment_user_id and case.waiting_comment_user_id != user_id:
            return "Тип списания выбирает менеджер, который начал закрытие."
        return None
    if case.status in {
        WAITING_CLOSE_COMMENT,
        WAITING_DRIVER_NAME,
        WAITING_PAYMENT_TYPE,
        WAITING_DISPATCHER_COMMENT,
        WAITING_PAYMENT_AMOUNT,
        WAITING_PAYMENT_CORRECTION,
    }:
        return "Уже жду данные по закрытию."
    return None


@router.message(F.text)
async def damage_control_message(message: Message) -> None:
    settings = _settings_from_message(message)
    if not _same_chat(message.chat.id, settings.settings.fp_chat_id):
        return
    text = (message.text or "").strip()
    if not text or not message.from_user:
        return
    async with session_scope(settings.sessionmaker) as session:
        if await _record_service_response(session, message, text, settings.settings.service_username):
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
                    DamageControlCase.status == WAITING_DRIVER_NAME,
                    DamageControlCase.waiting_comment_user_id == message.from_user.id,
                )
                .options(selectinload(DamageControlCase.inspection))
                .order_by(DamageControlCase.updated_at.desc(), DamageControlCase.id.desc())
            )
            if case:
                if len(text.split()) < 2:
                    await message.bot.send_message(
                        chat_id=message.chat.id,
                        text="Напишите ФИО водителя полностью, например: Иванов Иван Иванович.",
                        reply_to_message_id=message.message_id,
                        allow_sending_without_reply=True,
                    )
                    return
                case.driver_name = text
                if case.payment_type == NO_CHARGE_PAYMENT_TYPE:
                    case.status = WAITING_CLOSE_COMMENT
                    await _ask_no_charge_comment(message.bot, case, message.from_user.username)
                else:
                    case.status = WAITING_PAYMENT_TYPE
                    await _ask_payment_type(message.bot, case, message.from_user.username)
                return
            case = await session.scalar(
                select(DamageControlCase)
                .where(
                    DamageControlCase.status == WAITING_DISPATCHER_COMMENT,
                    DamageControlCase.waiting_comment_user_id == message.from_user.id,
                )
                .options(selectinload(DamageControlCase.inspection))
                .order_by(DamageControlCase.updated_at.desc(), DamageControlCase.id.desc())
            )
            if case:
                if len(text) < 3:
                    await message.bot.send_message(
                        chat_id=message.chat.id,
                        text="Напишите название диспетчерской.",
                        reply_to_message_id=message.message_id,
                        allow_sending_without_reply=True,
                    )
                    return
                case.close_comment = text
                case.status = WAITING_PAYMENT_AMOUNT
                await _ask_payment_amount(
                    message.bot,
                    case,
                    message.from_user.username,
                    case.payment_type or "dispatcher_balance",
                )
                return
            case = await session.scalar(
                select(DamageControlCase)
                .where(
                    DamageControlCase.status == WAITING_PAYMENT_CORRECTION,
                    DamageControlCase.waiting_comment_user_id == message.from_user.id,
                )
                .options(selectinload(DamageControlCase.inspection))
                .order_by(DamageControlCase.updated_at.desc(), DamageControlCase.id.desc())
            )
            if case:
                payment_type = _payment_type_key(case.payment_type) or "cashbox"
                amount = parse_split_payment_amount(text) if payment_type == "split_payment" else parse_payment_amount(text)
                if amount is None:
                    await message.bot.send_message(
                        chat_id=message.chat.id,
                        text="Не увидел сумму. Напишите сумму и комментарий одним сообщением.",
                        reply_to_message_id=message.message_id,
                        allow_sending_without_reply=True,
                    )
                    return
                case.payment_amount = amount
                case.close_comment = text
                case.status = case.close_type or PAYMENT_TYPE_CLOSE_STATUS.get(payment_type, "CLOSED_PAID_CASH")
                case.closed_at = _utcnow()
                case.waiting_comment_user_id = None
                case.waiting_comment_username = None
                await message.bot.send_message(
                    chat_id=case.fp_chat_id,
                    text=charge_correction_summary_text(case, message.from_user.full_name, message.from_user.username),
                    reply_to_message_id=case.fp_message_id,
                    allow_sending_without_reply=False,
                    reply_markup=edit_charge_keyboard(case.id),
                )
                return
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
            payment_type = case.payment_type or "cashbox"
            amount = parse_split_payment_amount(text) if payment_type == "split_payment" else parse_payment_amount(text)
            if amount is None:
                error_text = (
                    "Не увидел сумму. Для раздельной оплаты напишите разбивку, например:\n"
                    "30000 - общая сумма\n"
                    "15000 - с депозита\n"
                    "15000 - QR"
                    if payment_type == "split_payment"
                    else "Не увидел сумму. Напишите только сумму, например: 5000 или 5 тыс."
                )
                await message.bot.send_message(
                    chat_id=message.chat.id,
                    text=error_text,
                    reply_to_message_id=message.message_id,
                    allow_sending_without_reply=True,
                )
                return
            close_comment = text
            await _close_case(
                message.bot,
                case,
                message.from_user.full_name,
                message.from_user.username,
                PAYMENT_TYPE_CLOSE_STATUS.get(payment_type, "CLOSED_PAID_CASH"),
                close_comment or f"{PAYMENT_TYPE_LABELS.get(payment_type, payment_type)}: {amount}",
                payment_type=PAYMENT_TYPE_LABELS.get(payment_type, payment_type),
                payment_amount=amount,
            )
            return
        if case.payment_type == NO_CHARGE_PAYMENT_TYPE:
            if not valid_no_charge_reason(text):
                await message.bot.send_message(
                    chat_id=message.chat.id,
                    text="Напишите причину без списания подробнее.",
                    reply_to_message_id=message.message_id,
                    allow_sending_without_reply=True,
                )
                return
            await _close_case(
                message.bot,
                case,
                message.from_user.full_name,
                message.from_user.username,
                "CLOSED_NO_CHARGE_WITH_REASON",
                text,
                payment_type=NO_CHARGE_PAYMENT_TYPE,
                payment_amount=0,
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


def valid_no_charge_reason(text: str) -> bool:
    normalized = " ".join(text.lower().replace("ё", "е").split())
    if normalized in {"ок", "увидел", "принял", "посмотрю", "потом", "разберусь", "в работе"}:
        return False
    return len(normalized) >= 8 and len(normalized.split()) >= 2


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


def parse_split_payment_amount(text: str) -> int | None:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    amounts: list[int] = []
    total_markers = ("общ", "итог", "все повреж", "за все", "за всё", "всего")
    for line in lines:
        line_amounts = _parse_payment_amounts(line)
        if not line_amounts:
            continue
        normalized = " ".join(line.lower().replace("ё", "е").split())
        if any(marker in normalized for marker in total_markers):
            return line_amounts[0]
        amounts.extend(line_amounts)
    if not amounts:
        return None
    single_line = len(lines) == 1
    if len(amounts) > 1 and all(amount < 1000 for amount in amounts):
        if single_line and amounts[0] == sum(amounts[1:]):
            return amounts[0] * 1000
        return sum(amounts) * 1000
    if single_line and len(amounts) > 1 and amounts[0] == sum(amounts[1:]):
        return amounts[0]
    return sum(amounts)


def _parse_payment_amounts(text: str) -> list[int]:
    normalized = text.lower().replace("ё", "е").replace(",", ".")
    multiplier = 1000 if re.search(r"\b(тыс|т\.?р|к)\b", normalized) else 1
    amounts: list[int] = []
    for match in re.finditer(r"\d+(?:[\s.]?\d{3})*(?:\.\d+)?|\d+", normalized):
        raw = match.group(0).replace(" ", "")
        if "." in raw and len(raw.rsplit(".", 1)[1]) == 3:
            raw = raw.replace(".", "")
        try:
            value = float(raw)
        except ValueError:
            continue
        amount = int(value * multiplier)
        if amount > 0:
            amounts.append(amount)
    return amounts


def parse_service_estimate_amount(text: str) -> int | None:
    normalized = " ".join(text.lower().replace("ё", "е").strip().split())
    if not normalized or "?" in normalized:
        return None
    if not re.fullmatch(r"\d+(?:[\s.]?\d{3})*(?:[,.]\d+)?\s*(?:тыс|т\.?р|к|р|руб|руб\.)?", normalized):
        return None
    return parse_payment_amount(normalized)


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
        + [[InlineKeyboardButton(text="↩️ Назад", callback_data=f"damage_control:back:{case_id}")]]
    )


def back_keyboard(case_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="↩️ Назад", callback_data=f"damage_control:back:{case_id}")]]
    )


def edit_charge_keyboard(case_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Исправить списание", callback_data=f"damage_control:edit_charge:{case_id}")]
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
    plate = display_plate(case.plate_normalized or inspection.plate_normalized or inspection.plate_raw)
    lines = []
    if mentions:
        lines += [mentions, ""]
    lines.append(f"Найдены повреждения по авто {plate}.")
    if case.category == NO_CHARGE_REQUIRED:
        lines.append("В отчете указано, что водитель не виноват. Подтвердите проверку без списания.")
    else:
        lines.append("Нужно выбрать действие после проверки.")
        service_status = service_status_text(case, settings)
        lines += ["", service_status]
    if reminder_number is not None:
        lines += ["", f"Напоминание {reminder_number}/{settings.max_reminders}."]
    return "\n".join(lines)


def service_status_text(case: DamageControlCase, settings: Settings) -> str:
    if not case.service_received_at:
        return f"@{settings.service_username} уже запрошен для оценки/суммы."
    if case.service_amount:
        return f"Оценка/сумма от @{settings.service_username} получена: {case.service_amount}."
    if case.service_response_text:
        return f"Оценка/сумма от @{settings.service_username} получена: {case.service_response_text}."
    return f"Оценка/сумма от @{settings.service_username} получена."


def service_amount_request_text(case: DamageControlCase) -> str:
    plate = display_plate(case.plate_normalized)
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
        return ["pagorodu", "lalalas19", "serb_98", "Kicket22"]
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


async def _send_service_private_reminder(
    bot: Bot,
    session: AsyncSession,
    case: DamageControlCase,
    settings: Settings,
) -> None:
    service_user_id = await user_id_by_username(session, settings.service_username)
    if not service_user_id:
        logger.info("Cannot send private service reminder: @%s has not started the bot", settings.service_username)
        return
    plate = display_plate(case.plate_normalized or case.inspection.plate_normalized or case.inspection.plate_raw)
    try:
        await bot.send_message(
            chat_id=service_user_id,
            text=(
                f"Нужна оценка/сумма по повреждению авто {plate}.\n"
                "Ответьте в чате ФП reply к сообщению, где я вас отметил."
            ),
        )
    except TelegramAPIError:
        logger.exception("Failed to send private service reminder to @%s", settings.service_username)


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


async def _ask_driver_name(bot: Bot, case: DamageControlCase, username: str | None) -> None:
    mention = f"@{username.lstrip('@')}" if username else "Менеджер"
    await _send_case_followup(
        bot,
        case,
        f"{mention}, напишите ФИО водителя для отчёта по списанию.",
        reply_markup=back_keyboard(case.id),
    )


async def _ask_payment_type(bot: Bot, case: DamageControlCase, username: str | None) -> None:
    mention = f"@{username.lstrip('@')}" if username else "Менеджер"
    driver_line = f"\nВодитель: {case.driver_name}" if case.driver_name else ""
    await _send_case_followup(
        bot,
        case,
        f"{mention}, выберите тип оплаты/списания.{driver_line}",
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
    service_hint = f"\n@Norblacksmith указал: {case.service_amount}." if case.service_amount else ""
    if payment_type == "split_payment":
        text = (
            f"{mention}, тип: {label}.{service_hint}\n"
            "Напишите одним сообщением общую сумму и разбивку по способам.\n\n"
            "Пример:\n"
            "30000 - общая сумма\n"
            "15000 - с депозита\n"
            "15000 - QR / перевод"
        )
    else:
        text = f"{mention}, тип: {label}.{service_hint}\nНапишите сумму списания/оплаты одним сообщением."
    await _send_case_followup(
        bot,
        case,
        text,
        reply_markup=back_keyboard(case.id),
    )


async def _ask_payment_correction(bot: Bot, case: DamageControlCase, username: str | None) -> None:
    mention = f"@{username.lstrip('@')}" if username else "Менеджер"
    current_amount = case.payment_amount if case.payment_amount is not None else "не указана"
    await _send_case_followup(
        bot,
        case,
        (
            f"{mention}, сейчас сумма в базе: {current_amount}.\n"
            "Напишите новую сумму и комментарий одним сообщением.\n\n"
            "Пример:\n"
            "20000 - 17000 перевёл, 3000 доплатит завтра"
        ),
        reply_markup=back_keyboard(case.id),
    )


async def _ask_dispatcher_comment(bot: Bot, case: DamageControlCase, username: str | None) -> None:
    mention = f"@{username.lstrip('@')}" if username else "Менеджер"
    await _send_case_followup(
        bot,
        case,
        f"{mention}, напишите название диспетчерской.",
        reply_markup=back_keyboard(case.id),
    )


async def _send_pending_closure_reminder(
    bot: Bot,
    case: DamageControlCase,
    settings: Settings,
    reminder_number: int,
) -> None:
    mention = f"@{case.waiting_comment_username}" if case.waiting_comment_username else "Менеджер"
    step = "завершите закрытие повреждения"
    if case.status == WAITING_DRIVER_NAME:
        step = "напишите ФИО водителя"
    elif case.status == WAITING_PAYMENT_TYPE:
        step = "выберите тип оплаты/списания"
    elif case.status == WAITING_DISPATCHER_COMMENT:
        step = "напишите название диспетчерской"
    elif case.status == WAITING_PAYMENT_AMOUNT:
        step = (
            "напишите общую сумму и разбивку по способам"
            if case.payment_type == "split_payment"
            else "напишите сумму списания/оплаты"
        )
    elif case.status == WAITING_CLOSE_COMMENT:
        step = "напишите комментарий по закрытию"
    await _send_case_followup(
        bot,
        case,
        f"{mention}, {step}.\nНапоминание {reminder_number}/{settings.max_reminders}.",
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
        reply_markup=back_keyboard(case.id),
    )


async def _record_service_response(
    session: AsyncSession,
    message: Message,
    text: str,
    service_username: str,
) -> bool:
    case: DamageControlCase | None = None
    if message.reply_to_message:
        username = (message.from_user.username or "").lstrip("@").lower() if message.from_user else ""
        service_username = service_username.lstrip("@").lower()
        reply_message_id = message.reply_to_message.message_id
        amount = parse_service_estimate_amount(text)
        service_request_match = (
            (DamageControlCase.service_request_chat_id == message.chat.id)
            & (DamageControlCase.service_request_message_id == reply_message_id)
        )
        original_report_match = (DamageControlCase.fp_chat_id == message.chat.id) & (
            DamageControlCase.fp_message_id == reply_message_id
        )
        if amount is None:
            original_report_match = original_report_match & (username == service_username)
        case = await session.scalar(
            select(DamageControlCase)
            .where(
                service_request_match | original_report_match,
                DamageControlCase.status.not_in(FINAL_STATUSES),
            )
            .order_by(DamageControlCase.created_at.desc())
        )
    if case is None:
        return False
    if (
        message.from_user
        and case.waiting_comment_user_id == message.from_user.id
        and case.status
        in {
            WAITING_CLOSE_COMMENT,
            WAITING_DRIVER_NAME,
            WAITING_PAYMENT_TYPE,
            WAITING_DISPATCHER_COMMENT,
            WAITING_PAYMENT_AMOUNT,
        }
    ):
        return False
    if case.service_received_at:
        return True
    amount = parse_service_estimate_amount(text)
    if amount is None:
        return False
    case.service_received_at = _utcnow()
    case.service_response_text = text
    case.service_amount = amount
    case.service_reminder_due_at = None
    if case.status == WAITING_SERVICE_AMOUNT:
        case.status = SERVICE_AMOUNT_RECEIVED
    amount_text = f": {case.service_amount}" if case.service_amount else ""
    await message.bot.send_message(
        chat_id=case.fp_chat_id,
        text=f"Получил ответ от @{message.from_user.username or 'Norblacksmith'} по оценке/сумме повреждения{amount_text}.",
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
        reply_markup=edit_charge_keyboard(case.id) if payment_amount is not None else None,
    )


def close_summary_text(
    case: DamageControlCase,
    actor_name: str,
    actor_username: str | None,
    comment: str,
) -> str:
    username = f" (@{actor_username})" if actor_username else ""
    plate = display_plate(case.plate_normalized or case.inspection.plate_normalized or case.inspection.plate_raw)
    dt = case.inspection.completed_at or case.inspection.updated_at or case.created_at
    action = "проверку без списания"
    if case.close_type == "CLOSED_NO_CHARGE_WITH_REASON":
        action = "решение без списания"
    elif case.close_type != CLOSED_NO_CHARGE_REQUIRED:
        action = "оплату/списание по повреждениям"
    if case.payment_type and case.payment_amount is not None:
        return (
            f"Сотрудник {actor_name}{username} зафиксировал {action} на авто {plate}.\n"
            f"Дата и время осмотра: {dt:%d.%m.%Y %H:%M}\n"
            f"Водитель: {case.driver_name or 'не указан'}\n"
            f"Тип оплаты: {PAYMENT_TYPE_LABELS.get(case.payment_type, case.payment_type)}\n"
            f"Сумма: {case.payment_amount}\n"
            f"Комментарий: {comment}"
        )
    return (
        f"Сотрудник {actor_name}{username} зафиксировал {action} на авто {plate}.\n"
        f"Дата и время осмотра: {dt:%d.%m.%Y %H:%M}\n"
        f"Комментарий: {comment}"
    )


def charge_correction_summary_text(
    case: DamageControlCase,
    actor_name: str,
    actor_username: str | None,
) -> str:
    username = f" (@{actor_username})" if actor_username else ""
    plate = display_plate(case.plate_normalized or case.inspection.plate_normalized or case.inspection.plate_raw)
    dt = case.inspection.completed_at or case.inspection.updated_at or case.created_at
    return (
        f"Сотрудник {actor_name}{username} исправил списание по повреждениям на авто {plate}.\n"
        f"Дата и время осмотра: {dt:%d.%m.%Y %H:%M}\n"
        f"Водитель: {case.driver_name or 'не указан'}\n"
        f"Тип оплаты: {PAYMENT_TYPE_LABELS.get(case.payment_type, case.payment_type or 'не указан')}\n"
        f"Сумма: {case.payment_amount}\n"
        f"Комментарий: {case.close_comment or '-'}"
    )


def _payment_type_key(value: str | None) -> str | None:
    if not value:
        return None
    if value in PAYMENT_TYPE_LABELS:
        return value
    for key, label in PAYMENT_TYPE_LABELS.items():
        if value == label:
            return key
    return None


async def _escalate(bot: Bot, session: AsyncSession, case: DamageControlCase, settings: Settings) -> None:
    case.status = ESCALATED
    case.escalated_at = _utcnow()
    case.first_reminder_due_at = None
    plate = display_plate(case.plate_normalized or case.inspection.plate_normalized or case.inspection.plate_raw)
    responsible = f"@{case.waiting_comment_username}" if case.waiting_comment_username else active_manager_mentions(
        settings.manager_days_off,
        _local_now(settings).weekday(),
    )
    text = (
        "Повреждение после осмотра не закрыто после 3 напоминаний.\n\n"
        f"Авто: {plate}\n"
        f"Ответственный: {responsible}\n"
        f"Что не сделано: {_pending_step_text(case)}\n"
        f"ФИО водителя: {case.driver_name or 'не указано'}\n"
        f"Тип списания: {case.payment_type or 'не выбран'}\n"
        f"Сумма Нора: {case.service_amount if case.service_amount is not None else 'не указана'}\n"
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


def _pending_step_text(case: DamageControlCase) -> str:
    if case.status == WAITING_DRIVER_NAME:
        return "менеджер не указал ФИО водителя"
    if case.status == WAITING_PAYMENT_TYPE:
        return "менеджер не выбрал тип списания"
    if case.status == WAITING_DISPATCHER_COMMENT:
        return "менеджер не указал название диспетчерской"
    if case.status == WAITING_PAYMENT_AMOUNT:
        return "менеджер не указал сумму списания"
    if case.status == WAITING_CLOSE_COMMENT:
        return "менеджер не написал комментарий"
    if not case.service_received_at:
        return "сервис не дал оценку/сумму или менеджеры не закрыли повреждение"
    return "менеджеры не закрыли повреждение"


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
