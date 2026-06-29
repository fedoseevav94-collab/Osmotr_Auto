from __future__ import annotations

import logging
from datetime import datetime, timedelta
from html import escape
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message, ReplyKeyboardRemove
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.constants import (
    ALWAYS_SCORE_SCENARIO_FIELDS,
    MONTHLY_SCORE_FIELDS,
    PhotoType,
    SCORE_FIELD_TITLES,
    SCORE_FIELDS,
    SCORE_REFRESH_DAYS,
    STANDARD_SCENARIOS,
    SURRENDER_SCENARIOS,
    TIRE_TYPES,
    Scenario,
)
from app.damage_control import FINAL_STATUSES, start_damage_control_for_inspection
from app.db import session_scope
from app.export import period_bounds, write_charge_xlsx, write_history_xlsx, write_problem_xlsx, write_scores_xlsx
from app.keyboards import (
    BACK_BUTTON,
    FORWARD_BUTTON,
    RESET_BUTTON,
    START_BUTTON,
    damage_photos_keyboard,
    draft_keyboard,
    driver_remarks_keyboard,
    dtp_keyboard,
    charge_period_keyboard,
    export_period_keyboard,
    plate_choices_keyboard,
    plate_correction_keyboard,
    problem_period_keyboard,
    reset_confirm_keyboard,
    scenario_keyboard,
    score_keyboard,
    staff_idle_keyboard,
    staff_menu_keyboard,
    start_keyboard,
    staff_reply_keyboard,
    supervisor_menu_keyboard,
    tire_campaign_mode_keyboard,
    tire_score_keyboard,
    tire_type_keyboard,
    yes_no_keyboard,
)
from app.publisher import build_summary, publish_to_fp
from app.repository import InspectionRepository
from app.states import CorrectionFlow, ExportFlow, InspectionFlow, TireCampaignFlow
from app.utils import PLATE_FORMAT_HINT, display_plate, is_supervisor, is_valid_plate, normalize_plate
from app.validation import has_photo, validate_completion
from app.vehicle_registry import plate_hint, read_vehicle_rows

router = Router()
logger = logging.getLogger(__name__)
START_TEXTS = {START_BUTTON, "Начать осмотр"}
RESET_TEXTS = {RESET_BUTTON, "Сбросить осмотр"}
BACK_TEXTS = {BACK_BUTTON, "Назад"}
FORWARD_TEXTS = {FORWARD_BUTTON, "Вперёд"}
CONTROL_TEXTS = START_TEXTS | RESET_TEXTS | BACK_TEXTS | FORWARD_TEXTS
_SETTINGS: Settings | None = None
_SESSIONMAKER: async_sessionmaker | None = None


def register_handlers(dp: Dispatcher) -> None:
    dp.include_router(router)


def _settings() -> Settings:
    if _SETTINGS is None:
        raise RuntimeError("Router settings are not configured")
    return _SETTINGS


def _sessionmaker() -> async_sessionmaker:
    if _SESSIONMAKER is None:
        raise RuntimeError("Router sessionmaker is not configured")
    return _SESSIONMAKER


def _accent(text: str) -> str:
    return f"<b>{escape(text)}</b>"


async def _active_repo():
    return session_scope(_sessionmaker())


def _set_inspection_actor(
    inspection,
    user_id: int,
    username: str | None,
    full_name: str | None,
) -> None:
    inspection.telegram_user_id = user_id
    inspection.telegram_username = username
    inspection.telegram_name = full_name


def _is_bot_username(username: str | None) -> bool:
    return bool(username) and username.lower().endswith("bot")


def _human_actor_for_finish(data: dict, inspection, message: Message) -> tuple[int, str | None, str | None]:
    actor_user_id = data.get("actor_user_id")
    actor_username = data.get("actor_username")
    actor_full_name = data.get("actor_full_name")

    if _is_bot_username(str(actor_username) if actor_username else None):
        actor_username = None
        actor_full_name = None

    if actor_user_id and (actor_username or actor_full_name):
        return int(actor_user_id), str(actor_username) if actor_username else None, str(actor_full_name) if actor_full_name else None

    if inspection.telegram_user_id and not _is_bot_username(inspection.telegram_username):
        return inspection.telegram_user_id, inspection.telegram_username, inspection.telegram_name

    from_user = message.from_user
    if from_user and not from_user.is_bot:
        return from_user.id, from_user.username, from_user.full_name

    return inspection.telegram_user_id, None, inspection.telegram_name


def _state_value(value) -> str | None:
    if value is None:
        return None
    return getattr(value, "state", value)


async def _set_state(state: FSMContext, new_state) -> None:
    current = await state.get_state()
    new_state_value = _state_value(new_state)
    data = await state.get_data()
    back_stack = list(data.get("back_stack", []))
    if current and current != new_state_value:
        back_stack.append(current)
    await state.update_data(
        previous_state=back_stack[-1] if back_stack else None,
        forward_state=None,
        back_stack=back_stack,
        forward_stack=[],
    )
    await state.set_state(new_state)


async def _render_current_step(message: Message, state: FSMContext, state_value: str) -> None:
    data = await state.get_data()
    reply_markup = staff_reply_keyboard(can_forward=bool(data.get("forward_stack")))
    if state_value == InspectionFlow.choosing_scenario.state:
        await message.answer(
            _accent("🚗 Выберите сценарий осмотра:"),
            reply_markup=scenario_keyboard(),
            parse_mode="HTML",
        )
    elif state_value == InspectionFlow.accident_guilt.state:
        await message.answer(_accent("🚨 Водитель виноват?"), reply_markup=dtp_keyboard(), parse_mode="HTML")
    elif state_value == InspectionFlow.plate_digits.state:
        await message.answer(
            _accent("🔢 Сейчас шаг ввода номера.")
            + "\nВведите <b>3 цифры госномера</b>.",
            reply_markup=reply_markup,
            parse_mode="HTML",
        )
    elif state_value == InspectionFlow.plate_select.state:
        digits = data.get("plate_digits")
        if digits:
            async with session_scope(_sessionmaker()) as session:
                repo = InspectionRepository(session)
                matches = await repo.search_known_plates_by_digits(digits)
            await message.answer(
                _accent(f"🚘 Снова показываю номера с цифрами {digits}. Выберите нужный:")
                + "\nМожно также ввести другие 3 цифры новым сообщением.",
                reply_markup=plate_choices_keyboard(matches),
                parse_mode="HTML",
            )
        else:
            await message.answer(
                _accent("🔢 Введите 3 цифры госномера."),
                reply_markup=reply_markup,
                parse_mode="HTML",
            )
    elif state_value == InspectionFlow.plate_text.state:
        await message.answer(_accent("✍️ Введите госномер полностью."), reply_markup=reply_markup, parse_mode="HTML")
    elif state_value == InspectionFlow.plate_photo.state:
        await message.answer(
            _accent("📸 Теперь отправьте фото госномера для отчёта."),
            reply_markup=reply_markup,
            parse_mode="HTML",
        )
    elif state_value == InspectionFlow.dashboard_photo.state:
        await message.answer(
            _accent("⛽ Отправьте фото приборной панели с уровнем топлива."),
            reply_markup=reply_markup,
            parse_mode="HTML",
        )
    elif state_value == InspectionFlow.damage_question.state:
        await message.answer(_accent("⚠️ Есть повреждения?"), reply_markup=yes_no_keyboard("damage"), parse_mode="HTML")
    elif state_value == InspectionFlow.damage_photos.state:
        await message.answer(
            _accent("📸 Отправьте фото повреждений или нажмите кнопку, если фото уже отправлены."),
            reply_markup=damage_photos_keyboard(),
            parse_mode="HTML",
        )
    elif state_value == InspectionFlow.damage_description.state:
        await message.answer(_accent("📝 Опишите повреждения."), reply_markup=reply_markup, parse_mode="HTML")
    elif state_value == InspectionFlow.score.state:
        fields = data.get("score_fields") or [prefix for prefix, _ in SCORE_FIELDS]
        index = data.get("score_index", 0)
        prefix = fields[index]
        title = SCORE_FIELD_TITLES[prefix]
        await message.answer(_accent(f"⭐ Оценка: {title}"), reply_markup=score_keyboard(prefix), parse_mode="HTML")
    elif state_value == InspectionFlow.score_comment.state:
        await message.answer(
            _accent("📝 Оценка ниже 4. Напишите комментарий по этому критерию."),
            reply_markup=reply_markup,
            parse_mode="HTML",
        )
    elif state_value == InspectionFlow.driver_remarks.state:
        await message.answer(
            _accent("💬 Есть ли замечания по авто у водителя?"),
            reply_markup=driver_remarks_keyboard(),
            parse_mode="HTML",
        )
    elif state_value == InspectionFlow.driver_remarks_comment.state:
        await message.answer(
            _accent("📝 Опишите замечания водителя по авто."),
            reply_markup=reply_markup,
            parse_mode="HTML",
        )
    elif state_value == InspectionFlow.tire_type.state:
        await message.answer(_accent("🛞 Какая резина стоит на авто?"), reply_markup=tire_type_keyboard(), parse_mode="HTML")
    elif state_value == InspectionFlow.tire_photo.state:
        await message.answer(_accent("📸 Отправьте фото резины / протектора."), reply_markup=reply_markup, parse_mode="HTML")
    elif state_value == InspectionFlow.tire_score.state:
        await message.answer(_accent("⭐ Оцените состояние резины:"), reply_markup=tire_score_keyboard(), parse_mode="HTML")
    elif state_value == InspectionFlow.tire_comment.state:
        await message.answer(_accent("📝 Напишите комментарий по резине."), reply_markup=reply_markup, parse_mode="HTML")
    else:
        await message.answer(_accent("↩️ Вернулся на предыдущий шаг."), reply_markup=reply_markup, parse_mode="HTML")


def _end_of_today() -> datetime:
    now = datetime.now()
    return now.replace(hour=23, minute=59, second=59, microsecond=0)


def _start_mode_for_user(username: str | None) -> str:
    return "supervisor" if is_supervisor(username, _settings().supervisor_username) else "staff"


def _draft_next_step(inspection):
    if not inspection.scenario:
        return InspectionFlow.choosing_scenario, "Выберите сценарий осмотра:", {}, scenario_keyboard()
    scenario = Scenario(inspection.scenario)
    if scenario == Scenario.ACCIDENT and not inspection.dtp_driver_guilty:
        return InspectionFlow.accident_guilt, "Водитель виноват?", {}, dtp_keyboard()
    if not inspection.plate_normalized:
        return InspectionFlow.plate_digits, "Введите 3 цифры госномера.", {}, None
    if not has_photo(inspection, PhotoType.PLATE):
        return InspectionFlow.plate_photo, "Отправьте фото госномера.", {}, None
    if scenario in SURRENDER_SCENARIOS and not has_photo(inspection, PhotoType.DASHBOARD):
        return InspectionFlow.dashboard_photo, "Отправьте фото приборной панели с уровнем топлива.", {}, None
    if inspection.has_damage is None:
        return InspectionFlow.damage_question, "Есть повреждения?", {}, yes_no_keyboard("damage")
    if inspection.has_damage and not has_photo(inspection, PhotoType.DAMAGE):
        return InspectionFlow.damage_photos, "Отправьте фото повреждений.", {}, damage_photos_keyboard()
    if inspection.has_damage and not inspection.damage_description:
        return InspectionFlow.damage_description, "Опишите повреждения.", {}, None
    for prefix in ALWAYS_SCORE_SCENARIO_FIELDS.get(scenario, ()):
        title = SCORE_FIELD_TITLES[prefix]
        if getattr(inspection, f"{prefix}_score") is None:
            return (
                InspectionFlow.score,
                f"Оценка: {title}",
                {"score_index": 0, "score_fields": [prefix]},
                score_keyboard(prefix),
            )
    for index, (prefix, title) in enumerate(SCORE_FIELDS):
        score = getattr(inspection, f"{prefix}_score")
        comment = getattr(inspection, f"{prefix}_comment")
        if score is not None and score < 4 and not comment:
            return (
                InspectionFlow.score_comment,
                f"Напишите комментарий: {title}",
                {"comment_prefix": prefix, "score_index": index},
                None,
            )
    if scenario in SURRENDER_SCENARIOS and inspection.driver_has_remarks is None:
        return (
            InspectionFlow.driver_remarks,
            "Есть ли замечания по авто у водителя?",
            {},
            driver_remarks_keyboard(),
        )
    if (
        scenario in SURRENDER_SCENARIOS
        and inspection.driver_has_remarks
        and not inspection.driver_remarks_comment
    ):
        return InspectionFlow.driver_remarks_comment, "Опишите замечания водителя по авто.", {}, None
    if scenario in TIRE_REQUIRED_SCENARIOS and inspection.tire_type is None:
        return InspectionFlow.tire_type, "Какая резина стоит на авто?", {}, tire_type_keyboard()
    if inspection.tire_type and not has_photo(inspection, PhotoType.TIRE):
        return InspectionFlow.tire_photo, "Отправьте фото резины / протектора.", {}, None
    if inspection.tire_type and inspection.tire_score is None:
        return InspectionFlow.tire_score, "Оцените состояние резины.", {}, tire_score_keyboard()
    if inspection.tire_score is not None and inspection.tire_score < 4 and not inspection.tire_comment:
        return InspectionFlow.tire_comment, "Напишите комментарий по резине.", {}, None
    return InspectionFlow.publishing, "Черновик почти готов. Продолжите последний шаг или начните заново.", {}, None


def _photo_ids(message: Message) -> tuple[str, str]:
    largest = message.photo[-1]
    return largest.file_id, largest.file_unique_id


async def _handle_control_text(message: Message, state: FSMContext) -> bool:
    if message.text in START_TEXTS:
        await start_new_inspection(message, state)
        return True
    if message.text in RESET_TEXTS:
        await reset_button(message, state)
        return True
    if message.text in BACK_TEXTS:
        await back_button(message, state)
        return True
    if message.text in FORWARD_TEXTS:
        await forward_button(message, state)
        return True
    return False


def _callback_actor(callback: CallbackQuery) -> dict[str, object]:
    return {
        "actor_user_id": callback.from_user.id,
        "actor_username": callback.from_user.username,
        "actor_full_name": callback.from_user.full_name,
    }


async def _remember_callback_actor(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(**_callback_actor(callback))


async def _get_or_create_active(
    message: Message,
    user_id: int | None = None,
    username: str | None = None,
    full_name: str | None = None,
):
    user_id = user_id if user_id is not None else message.from_user.id
    username = username if username is not None else message.from_user.username
    full_name = full_name if full_name is not None else message.from_user.full_name
    async with session_scope(_sessionmaker()) as session:
        repo = InspectionRepository(session)
        inspection = await repo.active_for_user(user_id)
        if inspection is None:
            inspection = await repo.create_session(
                user_id,
                username,
                full_name,
            )
            await repo.log_action(inspection, "CREATE", user_id, username)
        return inspection.id


async def _create_new_session(message: Message) -> int:
    async with session_scope(_sessionmaker()) as session:
        repo = InspectionRepository(session)
        inspection = await repo.create_session(
            message.from_user.id,
            message.from_user.username,
            message.from_user.full_name,
        )
        await repo.log_action(inspection, "CREATE", message.from_user.id, message.from_user.username)
        return inspection.id


@router.message(Command("start", "help"))
async def start(message: Message, state: FSMContext) -> None:
    await _remember_bot_user(message)
    await state.clear()
    if _start_mode_for_user(message.from_user.username) == "staff":
        await show_staff_menu(message, state)
        return
    await message.answer(
        _accent("👋 Привет. Выберите режим работы:"),
        reply_markup=start_keyboard(),
        parse_mode="HTML",
    )


async def _remember_bot_user(message: Message) -> None:
    if not message.from_user:
        return
    async with session_scope(_sessionmaker()) as session:
        repo = InspectionRepository(session)
        await repo.remember_bot_user(
            message.from_user.id,
            message.from_user.username,
            message.from_user.full_name,
        )


@router.callback_query(F.data == "role:staff")
async def role_staff(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await show_staff_menu(callback.message, state)


@router.callback_query(F.data == "role:supervisor")
async def role_supervisor(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await show_supervisor_menu(callback.message, callback.from_user.username, state)


async def show_staff_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(_accent("🧰 Режим сотрудника осмотра."), reply_markup=staff_idle_keyboard(), parse_mode="HTML")
    await message.answer(_accent("👇 Выберите действие:"), reply_markup=staff_menu_keyboard(), parse_mode="HTML")


async def show_supervisor_menu(message: Message, username: str | None, state: FSMContext) -> None:
    if not is_supervisor(username, _settings().supervisor_username):
        await message.answer("Не лезь куда не надо 😄 Тут кнопки только для директора.")
        return
    await state.clear()
    await message.answer(
        _accent("👔 Режим руководителя. Выберите действие:"),
        reply_markup=supervisor_menu_keyboard(),
        parse_mode="HTML",
    )


@router.message(Command("new_inspection"))
async def new_inspection_cmd(message: Message, state: FSMContext) -> None:
    await start_new_inspection(message, state)


@router.message(F.text.in_(START_TEXTS))
async def new_inspection_button(message: Message, state: FSMContext) -> None:
    await start_new_inspection(message, state)


@router.message(F.text.in_(RESET_TEXTS | BACK_TEXTS | FORWARD_TEXTS))
async def inspection_control_button(message: Message, state: FSMContext) -> None:
    await _handle_control_text(message, state)


@router.callback_query(F.data == "new_inspection")
async def new_inspection_cb(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await start_new_inspection(
        callback.message,
        state,
        user_id=callback.from_user.id,
        username=callback.from_user.username,
        full_name=callback.from_user.full_name,
    )


@router.callback_query(F.data == "my_drafts")
async def my_drafts_cb(callback: CallbackQuery) -> None:
    await callback.answer()
    await show_my_drafts(callback.message, callback.from_user.id)


@router.callback_query(F.data.startswith("supervisor:"))
async def supervisor_menu_action(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_supervisor(callback.from_user.username, _settings().supervisor_username):
        await callback.message.answer("Не лезь куда не надо 😄 Тут кнопки только для директора.")
        await callback.answer()
        return
    action = callback.data.split(":", 1)[1]
    await callback.answer()
    if action == "stats_today":
        await send_stats_today(callback.message)
    elif action == "export_scores":
        await callback.message.answer("Выберите период:", reply_markup=export_period_keyboard())
    elif action == "export_problems":
        await callback.message.answer("Выберите период для проблемных авто:", reply_markup=problem_period_keyboard())
    elif action == "export_charges":
        await callback.message.answer("Выберите период для списаний:", reply_markup=charge_period_keyboard())
    elif action == "open_damages":
        await send_open_damages(callback.message)
    elif action == "service_waiting":
        await send_service_waiting(callback.message)
    elif action == "tire_check":
        await start_tire_campaign(
            callback.message,
            state,
            username=callback.from_user.username,
            user_id=callback.from_user.id,
        )
    elif action == "active_campaigns":
        await send_tire_check_status(callback.message, callback.from_user.username)


@router.message(Command("tire_check"))
async def tire_check_cmd(message: Message, state: FSMContext) -> None:
    await start_tire_campaign(message, state)


@router.callback_query(F.data == "new_tire_check")
async def tire_check_cb(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await start_tire_campaign(callback.message, state, username=callback.from_user.username, user_id=callback.from_user.id)


async def start_new_inspection(
    message: Message,
    state: FSMContext,
    user_id: int | None = None,
    username: str | None = None,
    full_name: str | None = None,
) -> None:
    inspection_id = await _get_or_create_active(message, user_id=user_id, username=username, full_name=full_name)
    await _set_state(state, InspectionFlow.choosing_scenario)
    await state.update_data(
        inspection_id=inspection_id,
        actor_user_id=user_id if user_id is not None else message.from_user.id,
        actor_username=username if username is not None else message.from_user.username,
        actor_full_name=full_name if full_name is not None else message.from_user.full_name,
    )
    await message.answer(
        _accent("🧭 Рабочие кнопки осмотра закреплены ниже."),
        reply_markup=staff_reply_keyboard(),
        parse_mode="HTML",
    )
    await message.answer(_accent("🚗 Выберите сценарий осмотра:"), reply_markup=scenario_keyboard(), parse_mode="HTML")


async def start_tire_campaign(
    message: Message,
    state: FSMContext,
    username: str | None = None,
    user_id: int | None = None,
) -> None:
    username = username if username is not None else message.from_user.username
    user_id = user_id if user_id is not None else message.from_user.id
    if not is_supervisor(username, _settings().supervisor_username):
        await message.answer("Не лезь куда не надо 😄 Тут кнопки только для директора.")
        return
    await state.clear()
    await message.answer(
        _accent("🛞 Как включить разовую проверку резины?"),
        reply_markup=tire_campaign_mode_keyboard(),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("tire_campaign:"))
async def tire_campaign_mode(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_supervisor(callback.from_user.username, _settings().supervisor_username):
        await callback.message.answer("Не лезь куда не надо 😄 Тут кнопки только для директора.")
        await callback.answer()
        return
    mode = callback.data.split(":", 1)[1]
    await callback.answer()
    if mode == "all":
        async with session_scope(_sessionmaker()) as session:
            repo = InspectionRepository(session)
            plates = await repo.list_known_plate_values()
            if not plates:
                await callback.message.answer("В базе пока нет номеров авто для проверки резины.")
                return
            campaign = await repo.create_tire_campaign(
                False,
                callback.from_user.id,
                callback.from_user.username,
            )
            for plate in plates:
                await repo.add_tire_campaign_plate(campaign, plate, plate)
        await state.clear()
        await callback.message.answer(
            f"Запустил круг проверки резины по текущей базе: {len(plates)} авто. "
            "Бот будет спрашивать резину только по этим авто, пока круг не завершится."
        )
        return
    await _set_state(state, TireCampaignFlow.waiting_list_file)
    await callback.message.answer("Загрузите Excel со списком машин. Нужна колонка `Номер`.")


@router.message(TireCampaignFlow.waiting_list_file, F.document)
async def tire_campaign_list_file(message: Message, state: FSMContext) -> None:
    if not is_supervisor(message.from_user.username, _settings().supervisor_username):
        await message.answer("Не лезь куда не надо 😄 Тут кнопки только для директора.")
        return
    filename = message.document.file_name or "tire_campaign.xlsx"
    if not filename.lower().endswith(".xlsx"):
        await message.answer("Нужен Excel-файл `.xlsx` с колонкой `Номер`.")
        return
    path = _settings().data_dir / f"tire_campaign_{message.from_user.id}_{message.document.file_unique_id}.xlsx"
    await message.bot.download(message.document, destination=path)
    try:
        rows = read_vehicle_rows(path)
    except Exception:
        await message.answer("Не смог прочитать файл. Проверьте, что там есть колонка `Номер`.")
        return
    if not rows:
        await message.answer("В файле не нашёл ни одного номера в колонке `Номер`.")
        return
    async with session_scope(_sessionmaker()) as session:
        repo = InspectionRepository(session)
        campaign = await repo.create_tire_campaign(
            False,
            message.from_user.id,
            message.from_user.username,
        )
        seen: set[str] = set()
        for row in rows:
            plate_normalized = row["plate_normalized"]
            if not plate_normalized or plate_normalized in seen:
                continue
            seen.add(plate_normalized)
            await repo.add_tire_campaign_plate(campaign, row["plate_raw"], plate_normalized)
    await state.clear()
    await message.answer(
        f"Включил разовую проверку резины по списку: {len(seen)} авто. "
        "Когда эти машины пройдут обычный осмотр, бот добавит критерий резины."
    )


@router.message(Command("tire_check_stop"))
async def tire_check_stop(message: Message, state: FSMContext) -> None:
    if not is_supervisor(message.from_user.username, _settings().supervisor_username):
        await message.answer("Не лезь куда не надо 😄 Тут кнопки только для директора.")
        return
    async with session_scope(_sessionmaker()) as session:
        repo = InspectionRepository(session)
        stopped_id = await repo.finish_active_tire_campaign()
    await state.clear()
    if stopped_id:
        await message.answer("Остановил активную проверку резины.")
    else:
        await message.answer("Активной проверки резины сейчас нет.")


@router.message(Command("tire_check_status"))
async def tire_check_status(message: Message) -> None:
    await send_tire_check_status(message, message.from_user.username)


async def send_tire_check_status(message: Message, username: str | None) -> None:
    if not is_supervisor(username, _settings().supervisor_username):
        await message.answer("Не лезь куда не надо 😄 Тут кнопки только для директора.")
        return
    async with session_scope(_sessionmaker()) as session:
        repo = InspectionRepository(session)
        progress = await repo.tire_campaign_progress()
    if progress is None:
        await message.answer("Активной проверки резины сейчас нет.")
        return
    campaign = progress["campaign"]
    expires = campaign.expires_at.strftime("%d.%m.%Y %H:%M") if campaign.expires_at else "без срока"
    if campaign.applies_to_all:
        await message.answer(f"Проверка резины активна: все проходящие авто\nДействует до: {expires}")
    else:
        await message.answer(
            "Проверка резины активна: список авто\n"
            f"Прогресс: {progress['done']}/{progress['total']}\n"
            f"Действует до: {expires}"
        )


@router.message(Command("active_campaigns"))
async def active_campaigns(message: Message) -> None:
    await tire_check_status(message)


@router.message(Command("stats_today"))
async def stats_today(message: Message) -> None:
    if not is_supervisor(message.from_user.username, _settings().supervisor_username):
        await message.answer("Не лезь куда не надо 😄 Тут кнопки только для директора.")
        return
    await send_stats_today(message)


async def send_stats_today(message: Message) -> None:
    start, end = period_bounds("today")
    async with session_scope(_sessionmaker()) as session:
        repo = InspectionRepository(session)
        stats = await repo.stats_between(start, end)
    await message.answer(
        "Статистика за сегодня:\n"
        f"Осмотров завершено: {stats['total']}\n"
        f"С повреждениями: {stats['with_damage']}\n"
        f"С проверкой резины: {stats['with_tire']}\n"
        f"С оценками ниже 4: {stats['low_scores']}"
    )


@router.callback_query(InspectionFlow.choosing_scenario, F.data.startswith("scenario:"))
async def choose_scenario(callback: CallbackQuery, state: FSMContext) -> None:
    await _remember_callback_actor(callback, state)
    scenario = Scenario(callback.data.split(":", 1)[1])
    data = await state.get_data()
    async with session_scope(_sessionmaker()) as session:
        repo = InspectionRepository(session)
        inspection = await repo.get(data["inspection_id"])
        inspection.scenario = scenario.value
        await repo.log_action(inspection, "SCENARIO", callback.from_user.id, callback.from_user.username, scenario.value)

    await callback.answer()
    if scenario == Scenario.ACCIDENT:
        await _set_state(state, InspectionFlow.accident_guilt)
        await callback.message.answer(
            _accent("🚨 Водитель виноват?"),
            reply_markup=dtp_keyboard(),
            parse_mode="HTML",
        )
    else:
        await ask_plate_digits(callback.message, state)


@router.callback_query(InspectionFlow.accident_guilt, F.data.startswith("dtp:"))
async def accident_guilt(callback: CallbackQuery, state: FSMContext) -> None:
    await _remember_callback_actor(callback, state)
    value = callback.data.split(":", 1)[1]
    data = await state.get_data()
    async with session_scope(_sessionmaker()) as session:
        repo = InspectionRepository(session)
        inspection = await repo.get(data["inspection_id"])
        inspection.dtp_driver_guilty = value
        await repo.log_action(inspection, "DTP_GUILT", callback.from_user.id, callback.from_user.username, value)
    await callback.answer()
    await ask_plate_digits(callback.message, state)


async def ask_plate_digits(message: Message, state: FSMContext) -> None:
    await _set_state(state, InspectionFlow.plate_digits)
    await message.answer(
        _accent("🔢 Введите 3 цифры госномера.")
        + "\nНапример, для <b>О864ОО797</b> введите <b>864</b>.",
        reply_markup=staff_reply_keyboard(),
        parse_mode="HTML",
    )


@router.message(InspectionFlow.plate_digits, F.text)
async def plate_digits(message: Message, state: FSMContext) -> None:
    if await _handle_control_text(message, state):
        return
    digits = "".join(char for char in message.text if char.isdigit())
    if len(digits) != 3:
        await message.answer(_accent("🔢 Нужно ввести ровно 3 цифры номера."), parse_mode="HTML")
        return
    async with session_scope(_sessionmaker()) as session:
        repo = InspectionRepository(session)
        matches = await repo.search_known_plates_by_digits(digits)
    if matches:
        await _set_state(state, InspectionFlow.plate_select)
        await state.update_data(plate_digits=digits)
        await message.answer(
            _accent(f"🚘 Нашёл номера с цифрами {digits}. Выберите нужный:"),
            reply_markup=plate_choices_keyboard(matches),
            parse_mode="HTML",
        )
        return
    await _set_state(state, InspectionFlow.plate_text)
    await message.answer(
        _accent("🚘 В базе нет номера с такими цифрами. Введите госномер полностью."),
        parse_mode="HTML",
    )


@router.message(InspectionFlow.plate_select, F.text)
async def plate_select_text(message: Message, state: FSMContext) -> None:
    if await _handle_control_text(message, state):
        return
    await plate_digits(message, state)


@router.callback_query(InspectionFlow.plate_select, F.data.startswith("plate_select:"))
async def plate_select(callback: CallbackQuery, state: FSMContext) -> None:
    await _remember_callback_actor(callback, state)
    value = callback.data.split(":", 1)[1]
    await callback.answer()
    if value == "manual":
        await _set_state(state, InspectionFlow.plate_text)
        await callback.message.answer(_accent("✍️ Введите госномер полностью."), parse_mode="HTML")
        return
    if not await save_plate(
        callback.message,
        state,
        value,
        user_id=callback.from_user.id,
        username=callback.from_user.username,
    ):
        return
    await ask_plate_photo(callback.message, state)


async def ask_plate_photo(message: Message, state: FSMContext) -> None:
    await _set_state(state, InspectionFlow.plate_photo)
    await message.answer(
        _accent("📸 Теперь отправьте фото госномера для отчёта."),
        reply_markup=staff_reply_keyboard(),
        parse_mode="HTML",
    )


@router.message(InspectionFlow.plate_photo, F.photo)
async def plate_photo(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    file_id, unique_id = _photo_ids(message)
    async with session_scope(_sessionmaker()) as session:
        repo = InspectionRepository(session)
        inspection = await repo.get(data["inspection_id"])
        await repo.add_photo(inspection, PhotoType.PLATE, file_id, unique_id)
        await repo.log_action(inspection, "PHOTO_PLATE", message.from_user.id, message.from_user.username)
    await continue_after_plate_photo(message, state)


@router.message(InspectionFlow.plate_photo)
async def plate_photo_required(message: Message, state: FSMContext) -> None:
    if await _handle_control_text(message, state):
        return
    await message.answer(
        _accent("📸 Нужно именно фото госномера. Отправьте фото."),
        reply_markup=staff_reply_keyboard(),
        parse_mode="HTML",
    )


@router.message(InspectionFlow.plate_text, F.text)
async def plate_text(message: Message, state: FSMContext) -> None:
    if await _handle_control_text(message, state):
        return
    if not await save_plate(message, state, message.text.strip()):
        return
    await ask_plate_photo(message, state)


async def save_plate(
    message: Message,
    state: FSMContext,
    plate_raw: str,
    user_id: int | None = None,
    username: str | None = None,
) -> bool:
    plate_norm = normalize_plate(plate_raw)
    if not is_valid_plate(plate_raw):
        await message.answer(
            _accent("🚫 Номер не похож на госномер.")
            + "\n"
            + escape(PLATE_FORMAT_HINT)
            + "\nПример: <b>О917НХ797</b> или <b>АА77777</b>.",
            parse_mode="HTML",
        )
        return False
    user_id = user_id if user_id is not None else message.from_user.id
    username = username if username is not None else message.from_user.username
    data = await state.get_data()
    async with session_scope(_sessionmaker()) as session:
        repo = InspectionRepository(session)
        inspection = await repo.get(data["inspection_id"])
        inspection.plate_raw = plate_norm
        inspection.plate_normalized = plate_norm
        hint = await plate_hint(session, plate_norm)
        new_plate_requires_tire = hint is None
        if new_plate_requires_tire:
            await repo.upsert_known_plate(plate_norm, plate_norm, source="inspection")
            active_campaign = await repo.active_tire_campaign()
            if active_campaign and not active_campaign.applies_to_all:
                await repo.add_tire_campaign_plate(active_campaign, plate_norm, plate_norm)
        await repo.log_action(inspection, "PLATE", user_id, username, plate_norm)

    if hint is None:
        hint_text = "\nНомер не найден в текущей базе, но я принял его как новый."
    elif hint.exact:
        hint_text = "\nНомер найден в текущей базе машин."
    else:
        hint_text = f"\nНомер принят. Ближайшая подсказка из базы: {display_plate(hint.plate_normalized)}"

    if new_plate_requires_tire:
        await state.update_data(tire_required_for_new_plate=True)
    await message.answer(_accent(f"✅ Номер выбран: {display_plate(plate_norm)}") + escape(hint_text), parse_mode="HTML")
    return True


async def continue_after_plate_photo(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    async with session_scope(_sessionmaker()) as session:
        repo = InspectionRepository(session)
        inspection = await repo.get(data["inspection_id"])
        scenario = Scenario(inspection.scenario)
    if scenario in SURRENDER_SCENARIOS:
        await _set_state(state, InspectionFlow.dashboard_photo)
        await message.answer(
            _accent("⛽ Отправьте фото приборной панели с уровнем топлива."),
            parse_mode="HTML",
        )
    elif scenario == Scenario.TIRES:
        await ask_tire_type(message, state)
    else:
        await ask_damage(message, state)


@router.message(InspectionFlow.dashboard_photo, F.photo)
async def dashboard_photo(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    file_id, unique_id = _photo_ids(message)
    async with session_scope(_sessionmaker()) as session:
        repo = InspectionRepository(session)
        inspection = await repo.get(data["inspection_id"])
        await repo.add_photo(inspection, PhotoType.DASHBOARD, file_id, unique_id)
        await repo.log_action(inspection, "PHOTO_DASHBOARD", message.from_user.id, message.from_user.username)
    await ask_damage(message, state)


@router.message(InspectionFlow.dashboard_photo)
async def dashboard_photo_required(message: Message, state: FSMContext) -> None:
    if await _handle_control_text(message, state):
        return
    await message.answer(_accent("⛽ Нужно фото приборной панели, где виден уровень топлива."), parse_mode="HTML")


async def ask_damage(message: Message, state: FSMContext) -> None:
    await _set_state(state, InspectionFlow.damage_question)
    await message.answer(_accent("⚠️ Есть повреждения?"), reply_markup=yes_no_keyboard("damage"), parse_mode="HTML")


@router.callback_query(InspectionFlow.damage_question, F.data.startswith("damage:"))
async def damage_question(callback: CallbackQuery, state: FSMContext) -> None:
    await _remember_callback_actor(callback, state)
    has_damage_value = callback.data.endswith(":yes")
    data = await state.get_data()
    async with session_scope(_sessionmaker()) as session:
        repo = InspectionRepository(session)
        inspection = await repo.get(data["inspection_id"])
        inspection.has_damage = has_damage_value
        await repo.log_action(
            inspection,
            "DAMAGE_YES" if has_damage_value else "DAMAGE_NO",
            callback.from_user.id,
            callback.from_user.username,
        )
    await callback.answer()
    if has_damage_value:
        await _set_state(state, InspectionFlow.damage_photos)
        await callback.message.answer(
            _accent("📸 Отправьте фото повреждений. Можно несколько."),
            reply_markup=damage_photos_keyboard(),
            parse_mode="HTML",
        )
    else:
        await move_to_scores_or_finish(callback.message, state)


@router.message(InspectionFlow.damage_photos, F.photo)
async def damage_photo(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    file_id, unique_id = _photo_ids(message)
    async with session_scope(_sessionmaker()) as session:
        repo = InspectionRepository(session)
        inspection = await repo.get(data["inspection_id"])
        await repo.add_photo(inspection, PhotoType.DAMAGE, file_id, unique_id)
        await repo.log_action(inspection, "PHOTO_DAMAGE", message.from_user.id, message.from_user.username)
    await message.answer(
        _accent("✅ Фото добавлено. Можно отправить ещё или нажать кнопку."),
        reply_markup=damage_photos_keyboard(),
        parse_mode="HTML",
    )


@router.callback_query(InspectionFlow.damage_photos, F.data == "damage_photos_done")
async def damage_photos_done(callback: CallbackQuery, state: FSMContext) -> None:
    await _remember_callback_actor(callback, state)
    data = await state.get_data()
    async with session_scope(_sessionmaker()) as session:
        repo = InspectionRepository(session)
        inspection = await repo.get(data["inspection_id"])
        if not has_photo(inspection, PhotoType.DAMAGE):
            await callback.answer("Сначала отправьте хотя бы одно фото повреждений.", show_alert=True)
            return
    await callback.answer()
    await _set_state(state, InspectionFlow.damage_description)
    await callback.message.answer(
        _accent("📝 Опишите повреждения. Без описания завершить осмотр нельзя."),
        parse_mode="HTML",
    )


@router.message(InspectionFlow.damage_description, F.text)
async def damage_description(message: Message, state: FSMContext) -> None:
    if await _handle_control_text(message, state):
        return
    text = message.text.strip()
    if not text:
        await message.answer(_accent("📝 Нужно описание повреждений."), parse_mode="HTML")
        return
    data = await state.get_data()
    async with session_scope(_sessionmaker()) as session:
        repo = InspectionRepository(session)
        inspection = await repo.get(data["inspection_id"])
        inspection.damage_description = text
        await repo.log_action(inspection, "DAMAGE_DESCRIPTION", message.from_user.id, message.from_user.username)
    await move_to_scores_or_finish(message, state)


async def move_to_scores_or_finish(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    async with session_scope(_sessionmaker()) as session:
        repo = InspectionRepository(session)
        inspection = await repo.get(data["inspection_id"])
        score_fields = await required_score_fields(repo, inspection)

    if score_fields:
        await state.update_data(score_index=0)
        await state.update_data(score_fields=score_fields)
        await ask_score(message, state)
    else:
        await maybe_ask_tire_or_finish(message, state)


async def required_score_fields(repo: InspectionRepository, inspection) -> list[str]:
    scenario = Scenario(inspection.scenario)
    fields: list[str] = []
    if scenario not in STANDARD_SCENARIOS:
        return fields

    cutoff = datetime.now() - timedelta(days=SCORE_REFRESH_DAYS)
    for prefix in MONTHLY_SCORE_FIELDS:
        if not await repo.has_recent_score_for_plate(inspection.plate_normalized, prefix, cutoff, inspection.id):
            fields.append(prefix)

    for prefix in ALWAYS_SCORE_SCENARIO_FIELDS.get(scenario, ()):
        if prefix not in fields:
            fields.append(prefix)
    return fields


async def ask_score(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    fields = data.get("score_fields") or [prefix for prefix, _ in SCORE_FIELDS]
    index = data.get("score_index", 0)
    prefix = fields[index]
    title = SCORE_FIELD_TITLES[prefix]
    await _set_state(state, InspectionFlow.score)
    await message.answer(_accent(f"⭐ Оценка: {title}"), reply_markup=score_keyboard(prefix), parse_mode="HTML")


@router.callback_query(InspectionFlow.score, F.data.startswith("score:"))
async def score(callback: CallbackQuery, state: FSMContext) -> None:
    await _remember_callback_actor(callback, state)
    _, prefix, raw_score = callback.data.split(":")
    score_value = int(raw_score)
    data = await state.get_data()
    async with session_scope(_sessionmaker()) as session:
        repo = InspectionRepository(session)
        inspection = await repo.get(data["inspection_id"])
        setattr(inspection, f"{prefix}_score", score_value)
        await repo.log_action(
            inspection,
            f"SCORE_{prefix.upper()}",
            callback.from_user.id,
            callback.from_user.username,
            str(score_value),
        )
    await callback.answer()
    if score_value < 4:
        await _set_state(state, InspectionFlow.score_comment)
        await state.update_data(comment_prefix=prefix)
        await callback.message.answer(
            _accent("📝 Оценка ниже 4. Напишите комментарий по этому критерию."),
            parse_mode="HTML",
        )
        return
    await next_score_or_finish(callback.message, state)


@router.message(InspectionFlow.score_comment, F.text)
async def score_comment(message: Message, state: FSMContext) -> None:
    if await _handle_control_text(message, state):
        return
    text = message.text.strip()
    if not text:
        await message.answer(_accent("📝 Комментарий обязателен для оценки ниже 4."), parse_mode="HTML")
        return
    data = await state.get_data()
    prefix = data["comment_prefix"]
    async with session_scope(_sessionmaker()) as session:
        repo = InspectionRepository(session)
        inspection = await repo.get(data["inspection_id"])
        setattr(inspection, f"{prefix}_comment", text)
        await repo.log_action(inspection, f"COMMENT_{prefix.upper()}", message.from_user.id, message.from_user.username)
    await next_score_or_finish(message, state)


async def next_score_or_finish(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    fields = data.get("score_fields") or [prefix for prefix, _ in SCORE_FIELDS]
    next_index = data.get("score_index", 0) + 1
    if next_index < len(fields):
        await state.update_data(score_index=next_index)
        await ask_score(message, state)
    else:
        await maybe_ask_driver_remarks_or_continue(message, state)


async def maybe_ask_driver_remarks_or_continue(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    async with session_scope(_sessionmaker()) as session:
        repo = InspectionRepository(session)
        inspection = await repo.get(data["inspection_id"])
        scenario = Scenario(inspection.scenario)
        should_ask = scenario in SURRENDER_SCENARIOS and inspection.driver_has_remarks is None
    if should_ask:
        await _set_state(state, InspectionFlow.driver_remarks)
        await message.answer(
            _accent("💬 Есть ли замечания по авто у водителя?"),
            reply_markup=driver_remarks_keyboard(),
            parse_mode="HTML",
        )
    else:
        await maybe_ask_tire_or_finish(message, state)


@router.callback_query(InspectionFlow.driver_remarks, F.data.startswith("driver_remarks:"))
async def driver_remarks(callback: CallbackQuery, state: FSMContext) -> None:
    await _remember_callback_actor(callback, state)
    value = callback.data.split(":", 1)[1]
    data = await state.get_data()
    async with session_scope(_sessionmaker()) as session:
        repo = InspectionRepository(session)
        inspection = await repo.get(data["inspection_id"])
        if value == "yes":
            inspection.driver_has_remarks = True
            action_comment = "yes"
        elif value == "already":
            inspection.driver_has_remarks = True
            inspection.driver_remarks_comment = "Указал ранее"
            action_comment = "already"
        else:
            inspection.driver_has_remarks = False
            inspection.driver_remarks_comment = None
            action_comment = "no"
        await repo.log_action(
            inspection,
            "DRIVER_REMARKS",
            callback.from_user.id,
            callback.from_user.username,
            action_comment,
        )
    await callback.answer()
    if value == "yes":
        await _set_state(state, InspectionFlow.driver_remarks_comment)
        await callback.message.answer(_accent("📝 Опишите замечания водителя по авто."), parse_mode="HTML")
        return
    await maybe_ask_tire_or_finish(callback.message, state)


@router.message(InspectionFlow.driver_remarks_comment, F.text)
async def driver_remarks_comment(message: Message, state: FSMContext) -> None:
    if await _handle_control_text(message, state):
        return
    text = message.text.strip()
    if not text:
        await message.answer(_accent("📝 Нужно описание замечаний водителя."), parse_mode="HTML")
        return
    data = await state.get_data()
    async with session_scope(_sessionmaker()) as session:
        repo = InspectionRepository(session)
        inspection = await repo.get(data["inspection_id"])
        inspection.driver_has_remarks = True
        inspection.driver_remarks_comment = text
        await repo.log_action(
            inspection,
            "DRIVER_REMARKS_COMMENT",
            message.from_user.id,
            message.from_user.username,
        )
    await maybe_ask_tire_or_finish(message, state)


async def ask_tire_type(message: Message, state: FSMContext) -> None:
    await _set_state(state, InspectionFlow.tire_type)
    await message.answer(_accent("🛞 Какая резина стоит на авто?"), reply_markup=tire_type_keyboard(), parse_mode="HTML")


async def maybe_ask_tire_or_finish(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    async with session_scope(_sessionmaker()) as session:
        repo = InspectionRepository(session)
        inspection = await repo.get(data["inspection_id"])
        scenario = Scenario(inspection.scenario)
        campaign_applies = await repo.tire_campaign_applies_to_plate(inspection.plate_normalized)
        already_checked = await repo.has_tire_check_for_plate(inspection.plate_normalized, inspection.id)
        should_ask = (
            inspection.tire_score is None
            and (
                scenario == Scenario.TIRES
                or (
                    not already_checked
                    and (
                        scenario in STANDARD_SCENARIOS
                        or campaign_applies
                        or data.get("tire_required_for_new_plate")
                    )
                )
            )
        )
    if should_ask:
        await ask_tire_type(message, state)
    else:
        await finish_inspection(message, state)


@router.callback_query(InspectionFlow.tire_type, F.data.startswith("tire_type:"))
async def tire_type(callback: CallbackQuery, state: FSMContext) -> None:
    await _remember_callback_actor(callback, state)
    value = callback.data.split(":", 1)[1]
    if value not in TIRE_TYPES:
        await callback.answer("Выберите тип резины кнопкой.", show_alert=True)
        return
    data = await state.get_data()
    async with session_scope(_sessionmaker()) as session:
        repo = InspectionRepository(session)
        inspection = await repo.get(data["inspection_id"])
        inspection.tire_type = value
        await repo.log_action(
            inspection,
            "TIRE_TYPE",
            callback.from_user.id,
            callback.from_user.username,
            TIRE_TYPES[value],
        )
    await callback.answer()
    await _set_state(state, InspectionFlow.tire_photo)
    await callback.message.answer(_accent("📸 Отправьте фото резины / протектора."), parse_mode="HTML")


@router.message(InspectionFlow.tire_photo, F.photo)
async def tire_photo(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    file_id, unique_id = _photo_ids(message)
    async with session_scope(_sessionmaker()) as session:
        repo = InspectionRepository(session)
        inspection = await repo.get(data["inspection_id"])
        await repo.add_photo(inspection, PhotoType.TIRE, file_id, unique_id)
        await repo.log_action(inspection, "PHOTO_TIRE", message.from_user.id, message.from_user.username)
    await _set_state(state, InspectionFlow.tire_score)
    await message.answer(_accent("⭐ Оцените состояние резины:"), reply_markup=tire_score_keyboard(), parse_mode="HTML")


@router.message(InspectionFlow.tire_photo)
async def tire_photo_required(message: Message, state: FSMContext) -> None:
    if await _handle_control_text(message, state):
        return
    await message.answer(_accent("📸 Нужно фото резины / протектора."), parse_mode="HTML")


@router.callback_query(InspectionFlow.tire_score, F.data.startswith("tire_score:"))
async def tire_score(callback: CallbackQuery, state: FSMContext) -> None:
    await _remember_callback_actor(callback, state)
    score_value = int(callback.data.split(":", 1)[1])
    data = await state.get_data()
    async with session_scope(_sessionmaker()) as session:
        repo = InspectionRepository(session)
        inspection = await repo.get(data["inspection_id"])
        inspection.tire_score = score_value
        await repo.log_action(
            inspection,
            "TIRE_SCORE",
            callback.from_user.id,
            callback.from_user.username,
            str(score_value),
        )
    await callback.answer()
    if score_value < 4:
        await _set_state(state, InspectionFlow.tire_comment)
        await callback.message.answer(_accent("📝 Оценка ниже 4. Напишите комментарий по резине."), parse_mode="HTML")
        return
    await finish_inspection(callback.message, state)


@router.message(InspectionFlow.tire_comment, F.text)
async def tire_comment(message: Message, state: FSMContext) -> None:
    if await _handle_control_text(message, state):
        return
    text = message.text.strip()
    if not text:
        await message.answer(_accent("📝 Комментарий обязателен для оценки резины ниже 4."), parse_mode="HTML")
        return
    data = await state.get_data()
    async with session_scope(_sessionmaker()) as session:
        repo = InspectionRepository(session)
        inspection = await repo.get(data["inspection_id"])
        inspection.tire_comment = text
        await repo.log_action(inspection, "TIRE_COMMENT", message.from_user.id, message.from_user.username)
    await finish_inspection(message, state)


async def finish_inspection(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.set_state(InspectionFlow.publishing)
    async with session_scope(_sessionmaker()) as session:
        repo = InspectionRepository(session)
        inspection = await repo.get(data["inspection_id"])
        actor_user_id, actor_username, actor_full_name = _human_actor_for_finish(data, inspection, message)
        _set_inspection_actor(
            inspection,
            int(actor_user_id),
            actor_username,
            actor_full_name,
        )
        errors = validate_completion(inspection)
        if errors:
            await message.answer("⛔ <b>Осмотр нельзя завершить:</b>\n" + "\n".join(f"- {escape(error)}" for error in errors), parse_mode="HTML")
            return
        await repo.complete(inspection)
        finished_tire_campaign_id = await repo.mark_tire_campaign_done_for_inspection(inspection)
        await session.flush()
        message_id = await publish_to_fp(message.bot, inspection, _settings().fp_chat_id)
        inspection.fp_chat_id = _settings().fp_chat_id
        inspection.fp_message_id = message_id
        await start_damage_control_for_inspection(message.bot, session, inspection, _settings())
        await repo.log_action(inspection, "PUBLISH_FP", int(actor_user_id), str(actor_username) if actor_username else None)
        if finished_tire_campaign_id:
            await notify_tire_campaign_finished(message.bot, repo, finished_tire_campaign_id)
    await state.clear()
    await message.answer(
        _accent("✅ Готово. Итог осмотра опубликован в ФП."),
        reply_markup=staff_idle_keyboard(),
        parse_mode="HTML",
    )


async def notify_tire_campaign_finished(bot: Bot, repo: InspectionRepository, campaign_id: int) -> None:
    supervisor_id = await repo.latest_user_id_by_username(_settings().supervisor_username)
    if not supervisor_id:
        logger.info("Cannot send tire campaign report: supervisor has not started the bot")
        return
    rows = await repo.tire_campaign_rows(campaign_id)
    path = _settings().data_dir / f"tire_campaign_{campaign_id}_report.xlsx"
    write_scores_xlsx(rows, path)
    try:
        await bot.send_message(
            chat_id=supervisor_id,
            text=f"Круг проверки резины завершён. Проверено авто: {len(rows)}.",
        )
        await bot.send_document(
            chat_id=supervisor_id,
            document=FSInputFile(path),
            caption="Отчёт по завершённому кругу проверки резины.",
        )
    except Exception:
        logger.exception("Failed to send tire campaign report to supervisor")


@router.callback_query(F.data.startswith("correct_plate:"))
async def correct_plate_start(callback: CallbackQuery, state: FSMContext) -> None:
    inspection_id = int(callback.data.split(":", 1)[1])
    async with session_scope(_sessionmaker()) as session:
        repo = InspectionRepository(session)
        inspection = await repo.get(inspection_id)
        if inspection is None:
            await callback.answer("Осмотр не найден.", show_alert=True)
            return
        allowed = callback.from_user.id == inspection.telegram_user_id or is_supervisor(
            callback.from_user.username,
            _settings().supervisor_username,
        )
    if not allowed:
        await callback.answer("Исправить номер может только автор отчёта или руководитель.", show_alert=True)
        return
    await callback.answer()
    await state.clear()
    await state.set_state(CorrectionFlow.plate_text)
    await state.update_data(correct_inspection_id=inspection_id)
    await callback.message.answer(
        _accent("✏️ Введите правильный госномер одним сообщением."),
        parse_mode="HTML",
    )


@router.message(CorrectionFlow.plate_text, F.text)
async def correct_plate_apply(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    inspection_id = data.get("correct_inspection_id")
    plate_raw = message.text.strip()
    plate_norm = normalize_plate(plate_raw)
    if not is_valid_plate(plate_raw):
        await message.answer(
            _accent("🚫 Номер не похож на госномер.")
            + "\n"
            + escape(PLATE_FORMAT_HINT)
            + "\nПример: <b>О917НХ797</b> или <b>АА77777</b>.",
            parse_mode="HTML",
        )
        return
    async with session_scope(_sessionmaker()) as session:
        repo = InspectionRepository(session)
        inspection = await repo.get(inspection_id)
        if inspection is None:
            await message.answer(_accent("Осмотр не найден."), parse_mode="HTML")
            await state.clear()
            return
        allowed = message.from_user.id == inspection.telegram_user_id or is_supervisor(
            message.from_user.username,
            _settings().supervisor_username,
        )
        if not allowed:
            await message.answer("Не лезь куда не надо 😄 Тут кнопки только для директора.")
            await state.clear()
            return
        inspection.plate_raw = plate_norm
        inspection.plate_normalized = plate_norm
        await repo.log_action(inspection, "CORRECT_PLATE", message.from_user.id, message.from_user.username, plate_norm)
        await session.flush()
        summary = build_summary(inspection)
        fp_chat_id = inspection.fp_chat_id
        fp_message_id = inspection.fp_message_id
    if fp_chat_id and fp_message_id:
        try:
            await message.bot.edit_message_text(
                chat_id=fp_chat_id,
                message_id=fp_message_id,
                text=summary,
                reply_markup=plate_correction_keyboard(inspection_id),
            )
        except Exception as exc:
            logger.warning("Failed to edit FP message after plate correction: %s", exc)
    await state.clear()
    await message.answer(_accent(f"✅ Госномер исправлен на {display_plate(plate_norm)}."), parse_mode="HTML")


async def reset_button(message: Message, state: FSMContext) -> None:
    await message.answer(
        _accent("🛑 Вы действительно хотите сбросить текущий осмотр?"),
        reply_markup=reset_confirm_keyboard(),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("reset_confirm:"))
async def reset_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if callback.data.endswith(":no"):
        await callback.message.answer(_accent("↩️ Оставил осмотр как есть."), parse_mode="HTML")
        return
    await cancel(callback.message, state, user_id=callback.from_user.id, username=callback.from_user.username)


async def back_button(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    back_stack = list(data.get("back_stack", []))
    if not back_stack:
        await message.answer(_accent("⬅️ Назад пока некуда."), parse_mode="HTML")
        return
    current = await state.get_state()
    previous = back_stack.pop()
    forward_stack = list(data.get("forward_stack", []))
    if current:
        forward_stack.append(current)
    await state.set_state(previous)
    await state.update_data(
        previous_state=back_stack[-1] if back_stack else None,
        forward_state=forward_stack[-1] if forward_stack else None,
        back_stack=back_stack,
        forward_stack=forward_stack,
    )
    await message.answer(
        _accent("⬅️ Вернулся на предыдущий шаг."),
        reply_markup=staff_reply_keyboard(can_forward=True),
        parse_mode="HTML",
    )
    await _render_current_step(message, state, previous)


async def forward_button(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    forward_stack = list(data.get("forward_stack", []))
    if not forward_stack:
        await message.answer(_accent("➡️ Вперёд пока некуда."), parse_mode="HTML")
        return
    current = await state.get_state()
    forward = forward_stack.pop()
    back_stack = list(data.get("back_stack", []))
    if current:
        back_stack.append(current)
    await state.set_state(forward)
    await state.update_data(
        previous_state=back_stack[-1] if back_stack else None,
        forward_state=forward_stack[-1] if forward_stack else None,
        back_stack=back_stack,
        forward_stack=forward_stack,
    )
    await message.answer(
        _accent("➡️ Вернулся вперёд."),
        reply_markup=staff_reply_keyboard(can_forward=bool(forward_stack)),
        parse_mode="HTML",
    )
    await _render_current_step(message, state, forward)


@router.message(Command("cancel"))
async def cancel_command(message: Message, state: FSMContext) -> None:
    await cancel(message, state)


@router.message(Command("hide", "menu_off", "скрыть"))
async def hide_keyboard(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        _accent("✅ Меню скрыто. Чтобы открыть снова, отправьте /start."),
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="HTML",
    )


async def cancel(
    message: Message,
    state: FSMContext,
    user_id: int | None = None,
    username: str | None = None,
) -> None:
    user_id = user_id if user_id is not None else message.from_user.id
    username = username if username is not None else message.from_user.username
    async with session_scope(_sessionmaker()) as session:
        repo = InspectionRepository(session)
        inspection = await repo.active_for_user(user_id)
        if inspection is None:
            await state.clear()
            await message.answer(
                _accent("ℹ️ Активного осмотра нет. Меню скрыто, открыть снова можно через /start."),
                reply_markup=ReplyKeyboardRemove(),
                parse_mode="HTML",
            )
            return
        await repo.cancel(inspection)
        await repo.log_action(inspection, "CANCEL", user_id, username)
    await state.clear()
    await message.answer(_accent("🛑 Текущий осмотр отменён."), reply_markup=staff_idle_keyboard(), parse_mode="HTML")


@router.message(Command("my_drafts"))
async def my_drafts(message: Message) -> None:
    await show_my_drafts(message, message.from_user.id)


async def show_my_drafts(message: Message, user_id: int) -> None:
    async with session_scope(_sessionmaker()) as session:
        repo = InspectionRepository(session)
        drafts = await repo.drafts_for_user(user_id)
    if not drafts:
        await message.answer("Черновиков нет.")
        return
    for draft in drafts:
        created = draft.created_at.strftime("%d.%m.%Y %H:%M") if draft.created_at else ""
        await message.answer(
            f"Черновик #{draft.id}\n"
            f"Сценарий: {draft.scenario or 'сценарий не выбран'}\n"
            f"Номер: {display_plate(draft.plate_normalized) if draft.plate_normalized else 'номер не введён'}\n"
            f"Создан: {created}",
            reply_markup=draft_keyboard(draft.id),
        )


@router.callback_query(F.data.startswith("draft_resume:"))
async def draft_resume(callback: CallbackQuery, state: FSMContext) -> None:
    inspection_id = int(callback.data.split(":", 1)[1])
    async with session_scope(_sessionmaker()) as session:
        repo = InspectionRepository(session)
        inspection = await repo.get(inspection_id)
        if inspection is None or inspection.telegram_user_id != callback.from_user.id or inspection.status != "DRAFT":
            await callback.answer("Черновик не найден.", show_alert=True)
            return
        next_state, prompt, extra_data, markup = _draft_next_step(inspection)
    await state.update_data(inspection_id=inspection_id, previous_state=None, forward_state=None)
    if extra_data:
        await state.update_data(**extra_data)
    await _set_state(state, next_state)
    await callback.answer()
    await callback.message.answer("Рабочие кнопки осмотра закреплены ниже.", reply_markup=staff_reply_keyboard())
    await callback.message.answer(prompt, reply_markup=markup)


@router.callback_query(F.data.startswith("draft_cancel:"))
async def draft_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    inspection_id = int(callback.data.split(":", 1)[1])
    async with session_scope(_sessionmaker()) as session:
        repo = InspectionRepository(session)
        inspection = await repo.get(inspection_id)
        if inspection is None or inspection.telegram_user_id != callback.from_user.id or inspection.status != "DRAFT":
            await callback.answer("Черновик не найден.", show_alert=True)
            return
        await repo.cancel(inspection)
        await repo.log_action(inspection, "CANCEL_DRAFT", callback.from_user.id, callback.from_user.username)
    await state.clear()
    await callback.answer()
    await callback.message.answer("Черновик отменён.", reply_markup=staff_idle_keyboard())


@router.message(Command("export_scores"))
async def export_scores(message: Message, state: FSMContext) -> None:
    if not is_supervisor(message.from_user.username, _settings().supervisor_username):
        await message.answer("Не лезь куда не надо 😄 Тут кнопки только для директора.")
        return
    await state.clear()
    await message.answer("Выберите период:", reply_markup=export_period_keyboard())


@router.message(Command("export_problems"))
async def export_problems(message: Message, state: FSMContext) -> None:
    if not is_supervisor(message.from_user.username, _settings().supervisor_username):
        await message.answer("Не лезь куда не надо 😄 Тут кнопки только для директора.")
        return
    await state.clear()
    await message.answer("Выберите период для проблемных авто:", reply_markup=problem_period_keyboard())


@router.message(Command("export_charges"))
async def export_charges(message: Message, state: FSMContext) -> None:
    if not is_supervisor(message.from_user.username, _settings().supervisor_username):
        await message.answer("Не лезь куда не надо 😄 Тут кнопки только для директора.")
        return
    await state.clear()
    await message.answer("Выберите период для списаний:", reply_markup=charge_period_keyboard())


@router.message(Command("open_damages"))
async def open_damages(message: Message) -> None:
    if not is_supervisor(message.from_user.username, _settings().supervisor_username):
        await message.answer("Не лезь куда не надо 😄 Тут кнопки только для директора.")
        return
    await send_open_damages(message)


@router.message(Command("service_waiting"))
async def service_waiting(message: Message) -> None:
    if not is_supervisor(message.from_user.username, _settings().supervisor_username):
        await message.answer("Не лезь куда не надо 😄 Тут кнопки только для директора.")
        return
    await send_service_waiting(message)


@router.callback_query(F.data.startswith("export:"))
async def export_scores_period(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_supervisor(callback.from_user.username, _settings().supervisor_username):
        await callback.message.answer("Не лезь куда не надо 😄 Тут кнопки только для директора.")
        await callback.answer()
        return
    period = callback.data.split(":", 1)[1]
    await callback.answer()
    if period == "custom":
        await state.set_state(ExportFlow.custom_period)
        await callback.message.answer("Введите период в формате ДД.ММ.ГГГГ-ДД.ММ.ГГГГ")
        return
    start, end = period_bounds(period)
    await send_scores_export(callback.message, start, end)


@router.callback_query(F.data.startswith("problems:"))
async def export_problems_period(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_supervisor(callback.from_user.username, _settings().supervisor_username):
        await callback.message.answer("Не лезь куда не надо 😄 Тут кнопки только для директора.")
        await callback.answer()
        return
    period = callback.data.split(":", 1)[1]
    await callback.answer()
    if period == "custom":
        await _set_state(state, ExportFlow.problem_custom_period)
        await callback.message.answer("Введите период в формате ДД.ММ.ГГГГ-ДД.ММ.ГГГГ")
        return
    start, end = period_bounds(period)
    await send_problem_export(callback.message, start, end)


@router.callback_query(F.data.startswith("charges:"))
async def export_charges_period(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_supervisor(callback.from_user.username, _settings().supervisor_username):
        await callback.message.answer("Не лезь куда не надо 😄 Тут кнопки только для директора.")
        await callback.answer()
        return
    period = callback.data.split(":", 1)[1]
    await callback.answer()
    if period == "custom":
        await _set_state(state, ExportFlow.charge_custom_period)
        await callback.message.answer("Введите период в формате ДД.ММ.ГГГГ-ДД.ММ.ГГГГ")
        return
    start, end = period_bounds(period)
    await send_charge_export(callback.message, start, end)


@router.message(ExportFlow.custom_period, F.text)
async def export_custom_period(message: Message, state: FSMContext) -> None:
    if await _handle_control_text(message, state):
        return
    if not is_supervisor(message.from_user.username, _settings().supervisor_username):
        await message.answer("Не лезь куда не надо 😄 Тут кнопки только для директора.")
        return
    try:
        raw_start, raw_end = [part.strip() for part in message.text.split("-", 1)]
        start = datetime.strptime(raw_start, "%d.%m.%Y")
        end = datetime.strptime(raw_end, "%d.%m.%Y").replace(hour=23, minute=59, second=59)
    except ValueError:
        await message.answer("Не понял период. Формат: ДД.ММ.ГГГГ-ДД.ММ.ГГГГ")
        return
    await state.clear()
    await send_scores_export(message, start, end)


@router.message(ExportFlow.problem_custom_period, F.text)
async def export_problem_custom_period(message: Message, state: FSMContext) -> None:
    if await _handle_control_text(message, state):
        return
    if not is_supervisor(message.from_user.username, _settings().supervisor_username):
        await message.answer("Не лезь куда не надо 😄 Тут кнопки только для директора.")
        return
    try:
        raw_start, raw_end = [part.strip() for part in message.text.split("-", 1)]
        start = datetime.strptime(raw_start, "%d.%m.%Y")
        end = datetime.strptime(raw_end, "%d.%m.%Y").replace(hour=23, minute=59, second=59)
    except ValueError:
        await message.answer("Не понял период. Формат: ДД.ММ.ГГГГ-ДД.ММ.ГГГГ")
        return
    await state.clear()
    await send_problem_export(message, start, end)


@router.message(ExportFlow.charge_custom_period, F.text)
async def export_charge_custom_period(message: Message, state: FSMContext) -> None:
    if await _handle_control_text(message, state):
        return
    if not is_supervisor(message.from_user.username, _settings().supervisor_username):
        await message.answer("Не лезь куда не надо 😄 Тут кнопки только для директора.")
        return
    try:
        raw_start, raw_end = [part.strip() for part in message.text.split("-", 1)]
        start = datetime.strptime(raw_start, "%d.%m.%Y")
        end = datetime.strptime(raw_end, "%d.%m.%Y").replace(hour=23, minute=59, second=59)
    except ValueError:
        await message.answer("Не понял период. Формат: ДД.ММ.ГГГГ-ДД.ММ.ГГГГ")
        return
    await state.clear()
    await send_charge_export(message, start, end)


async def send_scores_export(message: Message, start: datetime, end: datetime) -> None:
    async with session_scope(_sessionmaker()) as session:
        repo = InspectionRepository(session)
        rows = await repo.score_rows(start, end)
    path = _settings().data_dir / f"scores_{start:%Y%m%d}_{end:%Y%m%d}.xlsx"
    write_scores_xlsx(rows, path)
    await message.answer_document(FSInputFile(path), caption=f"Оценки: {len(rows)} строк")


async def send_problem_export(message: Message, start: datetime, end: datetime) -> None:
    async with session_scope(_sessionmaker()) as session:
        repo = InspectionRepository(session)
        rows = await repo.problem_rows(start, end)
    path = _settings().data_dir / f"problems_{start:%Y%m%d}_{end:%Y%m%d}.xlsx"
    write_problem_xlsx(rows, path)
    await message.answer_document(FSInputFile(path), caption=f"Проблемные авто: {len(rows)} строк")


async def send_charge_export(message: Message, start: datetime, end: datetime) -> None:
    async with session_scope(_sessionmaker()) as session:
        repo = InspectionRepository(session)
        rows = await repo.damage_control_rows(start, end)
    path = _settings().data_dir / f"charges_{start:%Y%m%d}_{end:%Y%m%d}.xlsx"
    write_charge_xlsx(rows, path)
    await message.answer_document(FSInputFile(path), caption=f"Списания/закрытия повреждений: {len(rows)} строк")


async def send_open_damages(message: Message) -> None:
    async with session_scope(_sessionmaker()) as session:
        repo = InspectionRepository(session)
        rows = await repo.open_damage_control_cases(FINAL_STATUSES)
    if not rows:
        await message.answer("Открытых повреждений сейчас нет.")
        return
    lines = ["Открытые повреждения:"]
    for row in rows[:20]:
        inspection = row.inspection
        plate = display_plate(row.plate_normalized or inspection.plate_normalized or inspection.plate_raw)
        date = inspection.completed_at.strftime("%d.%m.%Y %H:%M") if inspection.completed_at else ""
        service = "сервис ответил" if row.service_received_at else "ждём сервис"
        lines.append(
            f"#{row.id} {plate} | {date} | {row.status} | напоминаний {row.reminders_sent} | {service}"
        )
    if len(rows) > 20:
        lines.append(f"...и ещё {len(rows) - 20}")
    await message.answer("\n".join(lines))


async def send_service_waiting(message: Message) -> None:
    async with session_scope(_sessionmaker()) as session:
        repo = InspectionRepository(session)
        rows = await repo.waiting_service_amount_cases(FINAL_STATUSES)
    if not rows:
        await message.answer("Сейчас нет повреждений, где ждём оценку/сумму от сервиса.")
        return
    now = datetime.utcnow()
    lines = ["Ждём оценку/сумму от сервиса:"]
    for row in rows[:20]:
        inspection = row.inspection
        plate = display_plate(row.plate_normalized or inspection.plate_normalized or inspection.plate_raw)
        requested = row.service_requested_at
        minutes = int((now - requested).total_seconds() // 60) if requested else 0
        date = inspection.completed_at.strftime("%d.%m.%Y %H:%M") if inspection.completed_at else ""
        lines.append(f"#{row.id} {plate} | осмотр {date} | ждём {minutes} мин")
    if len(rows) > 20:
        lines.append(f"...и ещё {len(rows) - 20}")
    await message.answer("\n".join(lines))


@router.message(Command("history_auto"))
async def history_auto(message: Message) -> None:
    if not is_supervisor(message.from_user.username, _settings().supervisor_username):
        await message.answer("Не лезь куда не надо 😄 Тут кнопки только для директора.")
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Укажите номер: /history_auto о917нх797")
        return
    plate = normalize_plate(parts[1])
    async with session_scope(_sessionmaker()) as session:
        repo = InspectionRepository(session)
        rows = await repo.history_for_plate(plate)
    if not rows:
        await message.answer(f"История по {plate} не найдена.")
        return
    lines = [f"История по {plate}:"]
    for row in rows[:10]:
        date = row.completed_at.strftime("%d.%m.%Y %H:%M") if row.completed_at else ""
        lines.append(f"{date} | {row.scenario} | кузов {row.body_score or '-'} | тех {row.tech_score or '-'} | оклейка {row.wrap_score or '-'}")
    await message.answer("\n".join(lines))
    path = _settings().data_dir / f"history_{plate}_{datetime.utcnow():%Y%m%d%H%M%S}.xlsx"
    write_history_xlsx(rows, path)
    await message.answer_document(FSInputFile(path), caption=f"История по {plate}")


def setup_router(settings: Settings, sessionmaker: async_sessionmaker) -> None:
    global _SETTINGS, _SESSIONMAKER
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    _SETTINGS = settings
    _SESSIONMAKER = sessionmaker
