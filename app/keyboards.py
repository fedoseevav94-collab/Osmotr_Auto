from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from app.constants import STANDARD_SCENARIOS, Scenario, TIRE_TYPES
from app.utils import display_plate

START_BUTTON = "🚗 Начать осмотр"
START_COMMAND_BUTTON = "/start"
RESET_BUTTON = "🛑 Сбросить осмотр"
BACK_BUTTON = "⬅️ Назад"
FORWARD_BUTTON = "➡️ Вперёд"

SCENARIO_LABELS = {
    Scenario.RETURN: "🏁 Сдача",
    Scenario.TRANSFER: "🔄 Пересадка",
    Scenario.ISSUE: "🔑 Выдача авто",
    Scenario.PLANNED: "🧾 Плановый осмотр",
    Scenario.ACCIDENT: "🚨 Осмотр после ДТП",
}


def start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🧰 Сотрудник осмотра", callback_data="role:staff")],
            [InlineKeyboardButton(text="👔 Руководитель", callback_data="role:supervisor")],
        ]
    )


def staff_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=START_BUTTON, callback_data="new_inspection")],
            [InlineKeyboardButton(text="📋 Мои черновики", callback_data="my_drafts")],
        ]
    )


def staff_idle_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=START_BUTTON)],
            [KeyboardButton(text=START_COMMAND_BUTTON)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def supervisor_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Статистика за сегодня", callback_data="supervisor:stats_today")],
            [InlineKeyboardButton(text="📥 Выгрузить оценки", callback_data="supervisor:export_scores")],
            [InlineKeyboardButton(text="⚠️ Проблемные авто", callback_data="supervisor:export_problems")],
            [InlineKeyboardButton(text="💸 Выгрузить списания", callback_data="supervisor:export_charges")],
            [InlineKeyboardButton(text="🔓 Открытые повреждения", callback_data="supervisor:open_damages")],
            [InlineKeyboardButton(text="🛠️ Ждём сервис", callback_data="supervisor:service_waiting")],
            [InlineKeyboardButton(text="🛞 Проверка резины", callback_data="supervisor:tire_check")],
            [InlineKeyboardButton(text="📌 Статус кампаний", callback_data="supervisor:active_campaigns")],
            [InlineKeyboardButton(text="🧰 Перейти в режим сотрудника", callback_data="role:staff")],
        ]
    )


def staff_reply_keyboard(can_forward: bool = False) -> ReplyKeyboardMarkup:
    buttons = [
        KeyboardButton(text=RESET_BUTTON),
        KeyboardButton(text=BACK_BUTTON),
    ]
    if can_forward:
        buttons.append(KeyboardButton(text=FORWARD_BUTTON))
    return ReplyKeyboardMarkup(
        keyboard=[
            buttons,
            [KeyboardButton(text=START_COMMAND_BUTTON)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def scenario_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=SCENARIO_LABELS[scenario], callback_data=f"scenario:{scenario.value}")]
            for scenario in STANDARD_SCENARIOS
        ]
    )


def dtp_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, водитель виноват", callback_data="dtp:guilty")],
            [InlineKeyboardButton(text="❌ Нет, водитель не виноват", callback_data="dtp:not_guilty")],
            [InlineKeyboardButton(text="❔ Неизвестно / требуется уточнение", callback_data="dtp:unknown")],
        ]
    )


def yes_no_keyboard(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да", callback_data=f"{prefix}:yes"),
                InlineKeyboardButton(text="❌ Нет", callback_data=f"{prefix}:no"),
            ]
        ]
    )


def driver_remarks_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да", callback_data="driver_remarks:yes"),
                InlineKeyboardButton(text="❌ Нет", callback_data="driver_remarks:no"),
            ],
            [InlineKeyboardButton(text="↩️ Указал ранее", callback_data="driver_remarks:already")],
        ]
    )


def damage_photos_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📸 Фото отправлены, дальше", callback_data="damage_photos_done")]
        ]
    )


def draft_keyboard(inspection_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="▶️ Продолжить", callback_data=f"draft_resume:{inspection_id}"),
                InlineKeyboardButton(text="🗑️ Отменить", callback_data=f"draft_cancel:{inspection_id}"),
            ]
        ]
    )


def reset_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🛑 Да, сбросить", callback_data="reset_confirm:yes"),
                InlineKeyboardButton(text="↩️ Нет, оставить", callback_data="reset_confirm:no"),
            ]
        ]
    )


def score_keyboard(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=str(score), callback_data=f"score:{prefix}:{score}")
                for score in range(1, 6)
            ]
        ]
    )


def tire_type_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=("❄️ Зимняя" if code == "winter" else "☀️ Летняя"),
                    callback_data=f"tire_type:{code}",
                )
            ]
            for code in TIRE_TYPES
        ]
    )


def tire_score_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=str(score), callback_data=f"tire_score:{score}")
                for score in range(1, 6)
            ]
        ]
    )


def plate_choices_keyboard(plates) -> InlineKeyboardMarkup:
    rows = []
    for plate in plates:
        details = " ".join(part for part in (plate.brand, plate.model) if part)
        label = f"🚘 {display_plate(plate.plate_normalized)}"
        if details:
            label += f" · {details}"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"plate_select:{plate.plate_normalized}")])
    rows.append([InlineKeyboardButton(text="✍️ Ввести номер вручную", callback_data="plate_select:manual")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def plate_correction_keyboard(inspection_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Исправить госномер", callback_data=f"correct_plate:{inspection_id}")]
        ]
    )


def tire_campaign_mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🌐 По всем проходящим авто", callback_data="tire_campaign:all")],
            [InlineKeyboardButton(text="📄 Загрузить Excel со списком", callback_data="tire_campaign:list")],
        ]
    )


def export_period_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📅 Сегодня", callback_data="export:today")],
            [InlineKeyboardButton(text="↩️ Вчера", callback_data="export:yesterday")],
            [InlineKeyboardButton(text="🗓️ Текущая неделя", callback_data="export:week")],
            [InlineKeyboardButton(text="📆 Текущий месяц", callback_data="export:month")],
            [InlineKeyboardButton(text="📚 Текущий год", callback_data="export:year")],
            [InlineKeyboardButton(text="🗂️ За всё время", callback_data="export:all")],
            [InlineKeyboardButton(text="✍️ Свой период", callback_data="export:custom")],
        ]
    )


def problem_period_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📅 Сегодня", callback_data="problems:today")],
            [InlineKeyboardButton(text="↩️ Вчера", callback_data="problems:yesterday")],
            [InlineKeyboardButton(text="🗓️ Текущая неделя", callback_data="problems:week")],
            [InlineKeyboardButton(text="📆 Текущий месяц", callback_data="problems:month")],
            [InlineKeyboardButton(text="📚 Текущий год", callback_data="problems:year")],
            [InlineKeyboardButton(text="🗂️ За всё время", callback_data="problems:all")],
            [InlineKeyboardButton(text="✍️ Свой период", callback_data="problems:custom")],
        ]
    )


def charge_period_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📅 Сегодня", callback_data="charges:today")],
            [InlineKeyboardButton(text="↩️ Вчера", callback_data="charges:yesterday")],
            [InlineKeyboardButton(text="🗓️ Текущая неделя", callback_data="charges:week")],
            [InlineKeyboardButton(text="📆 Текущий месяц", callback_data="charges:month")],
            [InlineKeyboardButton(text="📚 Текущий год", callback_data="charges:year")],
            [InlineKeyboardButton(text="🗂️ За всё время", callback_data="charges:all")],
            [InlineKeyboardButton(text="✍️ Свой период", callback_data="charges:custom")],
        ]
    )
