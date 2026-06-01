from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.constants import (
    PhotoType,
    SCORE_FIELDS,
    SCORE_SCENARIOS,
    SURRENDER_SCENARIOS,
    TIRE_TYPES,
    Scenario,
)
from app.db import session_scope
from app.export import period_bounds, write_history_xlsx, write_problem_xlsx, write_scores_xlsx
from app.keyboards import (
    damage_photos_keyboard,
    draft_keyboard,
    driver_remarks_keyboard,
    dtp_keyboard,
    export_period_keyboard,
    problem_period_keyboard,
    reset_confirm_keyboard,
    scenario_keyboard,
    score_keyboard,
    staff_menu_keyboard,
    start_keyboard,
    staff_reply_keyboard,
    supervisor_menu_keyboard,
    ocr_confirm_keyboard,
    tire_campaign_mode_keyboard,
    tire_score_keyboard,
    tire_type_keyboard,
    yes_no_keyboard,
)
from app.ocr import recognize_plate_from_image
from app.publisher import publish_to_fp
from app.repository import InspectionRepository
from app.states import ExportFlow, InspectionFlow, TireCampaignFlow
from app.utils import is_supervisor, normalize_plate
from app.validation import has_photo, validate_completion
from app.vehicle_registry import plate_hint, read_vehicle_rows

router = Router()
CONTROL_TEXTS = {"Сбросить осмотр", "Назад", "Вперёд"}
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


async def _active_repo():
    return session_scope(_sessionmaker())


async def _set_state(state: FSMContext, new_state) -> None:
    current = await state.get_state()
    await state.update_data(previous_state=current, forward_state=None)
    await state.set_state(new_state)


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
    if not has_photo(inspection, PhotoType.PLATE):
        return InspectionFlow.plate_photo, "Отправьте фото госномера.", {}, None
    if not inspection.plate_normalized:
        return InspectionFlow.plate_text, "Введите госномер вручную.", {}, None
    if scenario in SURRENDER_SCENARIOS and not has_photo(inspection, PhotoType.DASHBOARD):
        return InspectionFlow.dashboard_photo, "Отправьте фото приборной панели с уровнем топлива.", {}, None
    if inspection.has_damage is None:
        return InspectionFlow.damage_question, "Есть повреждения?", {}, yes_no_keyboard("damage")
    if inspection.has_damage and not has_photo(inspection, PhotoType.DAMAGE):
        return InspectionFlow.damage_photos, "Отправьте фото повреждений.", {}, damage_photos_keyboard()
    if inspection.has_damage and not inspection.damage_description:
        return InspectionFlow.damage_description, "Опишите повреждения.", {}, None
    if scenario in SCORE_SCENARIOS:
        for index, (prefix, title) in enumerate(SCORE_FIELDS):
            score = getattr(inspection, f"{prefix}_score")
            comment = getattr(inspection, f"{prefix}_comment")
            if score is None:
                return InspectionFlow.score, f"Оценка: {title}", {"score_index": index}, score_keyboard(prefix)
            if score < 4 and not comment:
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
    if message.text == "Сбросить осмотр":
        await reset_button(message, state)
        return True
    if message.text == "Назад":
        await back_button(message, state)
        return True
    if message.text == "Вперёд":
        await forward_button(message, state)
        return True
    return False


async def _get_or_create_active(message: Message):
    async with session_scope(_sessionmaker()) as session:
        repo = InspectionRepository(session)
        inspection = await repo.active_for_user(message.from_user.id)
        if inspection is None:
            inspection = await repo.create_session(
                message.from_user.id,
                message.from_user.username,
                message.from_user.full_name,
            )
            await repo.log_action(inspection, "CREATE", message.from_user.id, message.from_user.username)
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
    await state.clear()
    if _start_mode_for_user(message.from_user.username) == "staff":
        await show_staff_menu(message, state)
        return
    await message.answer(
        "Привет. Выберите режим работы:",
        reply_markup=start_keyboard(),
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
    await message.answer("Режим сотрудника осмотра.", reply_markup=staff_reply_keyboard())
    await message.answer("Выберите действие:", reply_markup=staff_menu_keyboard())


async def show_supervisor_menu(message: Message, username: str | None, state: FSMContext) -> None:
    if not is_supervisor(username, _settings().supervisor_username):
        await message.answer("Не лезь куда не надо 😄 Тут кнопки только для директора.")
        return
    await state.clear()
    await message.answer("Режим руководителя. Выберите действие:", reply_markup=supervisor_menu_keyboard())


@router.message(Command("new_inspection"))
async def new_inspection_cmd(message: Message, state: FSMContext) -> None:
    await start_new_inspection(message, state)


@router.callback_query(F.data == "new_inspection")
async def new_inspection_cb(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await start_new_inspection(callback.message, state)


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


async def start_new_inspection(message: Message, state: FSMContext) -> None:
    inspection_id = await _get_or_create_active(message)
    await _set_state(state, InspectionFlow.choosing_scenario)
    await state.update_data(inspection_id=inspection_id)
    await message.answer("Выберите сценарий осмотра:", reply_markup=scenario_keyboard())


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
    await message.answer("Как включить разовую проверку резины?", reply_markup=tire_campaign_mode_keyboard())


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
            await repo.create_tire_campaign(
                True,
                callback.from_user.id,
                callback.from_user.username,
                expires_at=_end_of_today(),
            )
        await state.clear()
        await callback.message.answer(
            "Включил проверку резины до конца дня для всех проходящих авто. "
            "В обычном осмотре бот спросит тип резины и оценку состояния."
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
            expires_at=_end_of_today(),
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
        f"Включил проверку резины по списку до конца дня: {len(seen)} авто. "
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
        await callback.message.answer("Водитель виноват?", reply_markup=dtp_keyboard())
    else:
        await _set_state(state, InspectionFlow.plate_photo)
        await callback.message.answer("Отправьте фото госномера.")


@router.callback_query(InspectionFlow.accident_guilt, F.data.startswith("dtp:"))
async def accident_guilt(callback: CallbackQuery, state: FSMContext) -> None:
    value = callback.data.split(":", 1)[1]
    data = await state.get_data()
    async with session_scope(_sessionmaker()) as session:
        repo = InspectionRepository(session)
        inspection = await repo.get(data["inspection_id"])
        inspection.dtp_driver_guilty = value
        await repo.log_action(inspection, "DTP_GUILT", callback.from_user.id, callback.from_user.username, value)
    await callback.answer()
    await _set_state(state, InspectionFlow.plate_photo)
    await callback.message.answer("Отправьте фото госномера.")


@router.message(InspectionFlow.plate_photo, F.photo)
async def plate_photo(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    file_id, unique_id = _photo_ids(message)
    async with session_scope(_sessionmaker()) as session:
        repo = InspectionRepository(session)
        inspection = await repo.get(data["inspection_id"])
        await repo.add_photo(inspection, PhotoType.PLATE, file_id, unique_id)
        await repo.log_action(inspection, "PHOTO_PLATE", message.from_user.id, message.from_user.username)
    photo_path = _settings().data_dir / "ocr" / f"plate_{message.from_user.id}_{unique_id}.jpg"
    photo_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        await message.bot.download(message.photo[-1], destination=photo_path)
    except Exception:
        photo_path = None
    recognized = recognize_plate_from_image(photo_path) if photo_path else None
    if recognized:
        await _set_state(state, InspectionFlow.plate_confirm)
        await state.update_data(ocr_plate=recognized)
        await message.answer(f"Похоже, номер: {recognized}", reply_markup=ocr_confirm_keyboard())
        return
    await _set_state(state, InspectionFlow.plate_text)
    await message.answer("Введите госномер вручную.")


@router.message(InspectionFlow.plate_photo)
async def plate_photo_required(message: Message) -> None:
    await message.answer("Нужно именно фото госномера. Отправьте фото.")


@router.callback_query(InspectionFlow.plate_confirm, F.data.startswith("ocr_plate:"))
async def plate_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await callback.answer()
    if callback.data.endswith(":yes"):
        await save_plate_and_continue(callback.message, state, data["ocr_plate"])
        return
    await _set_state(state, InspectionFlow.plate_text)
    await callback.message.answer("Введите госномер вручную.")


@router.message(InspectionFlow.plate_text, F.text)
async def plate_text(message: Message, state: FSMContext) -> None:
    if await _handle_control_text(message, state):
        return
    await save_plate_and_continue(message, state, message.text.strip())


async def save_plate_and_continue(message: Message, state: FSMContext, plate_raw: str) -> None:
    plate_norm = normalize_plate(plate_raw)
    data = await state.get_data()
    async with session_scope(_sessionmaker()) as session:
        repo = InspectionRepository(session)
        inspection = await repo.get(data["inspection_id"])
        inspection.plate_raw = plate_raw
        inspection.plate_normalized = plate_norm
        scenario = Scenario(inspection.scenario)
        hint = await plate_hint(session, plate_norm)
        await repo.log_action(inspection, "PLATE", message.from_user.id, message.from_user.username, plate_norm)

    if hint is None:
        hint_text = "\nНомер не найден в текущей базе, но я принял его как новый."
    elif hint.exact:
        hint_text = "\nНомер найден в текущей базе машин."
    else:
        hint_text = f"\nНомер принят. Ближайшая подсказка из базы: {hint.plate_normalized}"

    if scenario in SURRENDER_SCENARIOS:
        await _set_state(state, InspectionFlow.dashboard_photo)
        await message.answer(
            f"Номер сохранён: {plate_norm}{hint_text}\nОтправьте фото приборной панели с уровнем топлива."
        )
    elif scenario == Scenario.TIRES:
        await message.answer(f"Номер сохранён: {plate_norm}{hint_text}")
        await ask_tire_type(message, state)
    else:
        await message.answer(f"Номер сохранён: {plate_norm}{hint_text}")
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
async def dashboard_photo_required(message: Message) -> None:
    await message.answer("Нужно фото приборной панели, где виден уровень топлива.")


async def ask_damage(message: Message, state: FSMContext) -> None:
    await _set_state(state, InspectionFlow.damage_question)
    await message.answer("Есть повреждения?", reply_markup=yes_no_keyboard("damage"))


@router.callback_query(InspectionFlow.damage_question, F.data.startswith("damage:"))
async def damage_question(callback: CallbackQuery, state: FSMContext) -> None:
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
            "Отправьте фото повреждений. Можно несколько.",
            reply_markup=damage_photos_keyboard(),
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
    await message.answer("Фото добавлено. Можно отправить ещё или нажать кнопку.", reply_markup=damage_photos_keyboard())


@router.callback_query(InspectionFlow.damage_photos, F.data == "damage_photos_done")
async def damage_photos_done(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    async with session_scope(_sessionmaker()) as session:
        repo = InspectionRepository(session)
        inspection = await repo.get(data["inspection_id"])
        if not has_photo(inspection, PhotoType.DAMAGE):
            await callback.answer("Сначала отправьте хотя бы одно фото повреждений.", show_alert=True)
            return
    await callback.answer()
    await _set_state(state, InspectionFlow.damage_description)
    await callback.message.answer("Опишите повреждения. Без описания завершить осмотр нельзя.")


@router.message(InspectionFlow.damage_description, F.text)
async def damage_description(message: Message, state: FSMContext) -> None:
    if await _handle_control_text(message, state):
        return
    text = message.text.strip()
    if not text:
        await message.answer("Нужно описание повреждений.")
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
        scenario = Scenario(inspection.scenario)

    if scenario in SCORE_SCENARIOS:
        await state.update_data(score_index=0)
        await ask_score(message, state)
    else:
        await maybe_ask_tire_or_finish(message, state)


async def ask_score(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    index = data.get("score_index", 0)
    prefix, title = SCORE_FIELDS[index]
    await _set_state(state, InspectionFlow.score)
    await message.answer(f"Оценка: {title}", reply_markup=score_keyboard(prefix))


@router.callback_query(InspectionFlow.score, F.data.startswith("score:"))
async def score(callback: CallbackQuery, state: FSMContext) -> None:
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
        await callback.message.answer("Оценка ниже 4. Напишите комментарий по этому критерию.")
        return
    await next_score_or_finish(callback.message, state)


@router.message(InspectionFlow.score_comment, F.text)
async def score_comment(message: Message, state: FSMContext) -> None:
    if await _handle_control_text(message, state):
        return
    text = message.text.strip()
    if not text:
        await message.answer("Комментарий обязателен для оценки ниже 4.")
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
    next_index = data.get("score_index", 0) + 1
    if next_index < len(SCORE_FIELDS):
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
            "Есть ли замечания по авто у водителя?",
            reply_markup=driver_remarks_keyboard(),
        )
    else:
        await maybe_ask_tire_or_finish(message, state)


@router.callback_query(InspectionFlow.driver_remarks, F.data.startswith("driver_remarks:"))
async def driver_remarks(callback: CallbackQuery, state: FSMContext) -> None:
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
        await callback.message.answer("Опишите замечания водителя по авто.")
        return
    await maybe_ask_tire_or_finish(callback.message, state)


@router.message(InspectionFlow.driver_remarks_comment, F.text)
async def driver_remarks_comment(message: Message, state: FSMContext) -> None:
    if await _handle_control_text(message, state):
        return
    text = message.text.strip()
    if not text:
        await message.answer("Нужно описание замечаний водителя.")
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
    await message.answer("Какая резина стоит на авто?", reply_markup=tire_type_keyboard())


async def maybe_ask_tire_or_finish(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    async with session_scope(_sessionmaker()) as session:
        repo = InspectionRepository(session)
        inspection = await repo.get(data["inspection_id"])
        should_ask = (
            inspection.tire_score is None
            and await repo.tire_campaign_applies_to_plate(inspection.plate_normalized)
        )
    if should_ask:
        await ask_tire_type(message, state)
    else:
        await finish_inspection(message, state)


@router.callback_query(InspectionFlow.tire_type, F.data.startswith("tire_type:"))
async def tire_type(callback: CallbackQuery, state: FSMContext) -> None:
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
    await callback.message.answer("Отправьте фото резины / протектора.")


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
    await message.answer("Оцените состояние резины:", reply_markup=tire_score_keyboard())


@router.message(InspectionFlow.tire_photo)
async def tire_photo_required(message: Message) -> None:
    await message.answer("Нужно фото резины / протектора.")


@router.callback_query(InspectionFlow.tire_score, F.data.startswith("tire_score:"))
async def tire_score(callback: CallbackQuery, state: FSMContext) -> None:
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
        await callback.message.answer("Оценка ниже 4. Напишите комментарий по резине.")
        return
    await finish_inspection(callback.message, state)


@router.message(InspectionFlow.tire_comment, F.text)
async def tire_comment(message: Message, state: FSMContext) -> None:
    if await _handle_control_text(message, state):
        return
    text = message.text.strip()
    if not text:
        await message.answer("Комментарий обязателен для оценки резины ниже 4.")
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
        errors = validate_completion(inspection)
        if errors:
            await message.answer("Осмотр нельзя завершить:\n" + "\n".join(f"- {error}" for error in errors))
            return
        await repo.complete(inspection)
        await repo.mark_tire_campaign_done_for_inspection(inspection)
        await session.flush()
        message_id = await publish_to_fp(message.bot, inspection, _settings().fp_chat_id)
        inspection.fp_chat_id = _settings().fp_chat_id
        inspection.fp_message_id = message_id
        await repo.log_action(inspection, "PUBLISH_FP", message.from_user.id, message.from_user.username)
    await state.clear()
    await message.answer("Готово. Итог осмотра опубликован в ФП.")


@router.message(F.text == "Сбросить осмотр")
async def reset_button(message: Message, state: FSMContext) -> None:
    await message.answer(
        "Вы действительно хотите сбросить текущий осмотр?",
        reply_markup=reset_confirm_keyboard(),
    )


@router.callback_query(F.data.startswith("reset_confirm:"))
async def reset_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if callback.data.endswith(":no"):
        await callback.message.answer("Оставил осмотр как есть.")
        return
    await cancel(callback.message, state, user_id=callback.from_user.id, username=callback.from_user.username)


@router.message(F.text == "Назад")
async def back_button(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    previous = data.get("previous_state")
    if not previous:
        await message.answer("Назад пока некуда.")
        return
    current = await state.get_state()
    await state.set_state(previous)
    await state.update_data(previous_state=None, forward_state=current)
    await message.answer("Вернулся на предыдущий шаг.")


@router.message(F.text == "Вперёд")
async def forward_button(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    forward = data.get("forward_state")
    if not forward:
        await message.answer("Вперёд пока некуда.")
        return
    current = await state.get_state()
    await state.set_state(forward)
    await state.update_data(previous_state=current, forward_state=None)
    await message.answer("Вернулся вперёд.")


@router.message(Command("cancel"))
async def cancel_command(message: Message, state: FSMContext) -> None:
    await cancel(message, state)


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
            await message.answer("Активного осмотра нет.")
            return
        await repo.cancel(inspection)
        await repo.log_action(inspection, "CANCEL", user_id, username)
    await state.clear()
    await message.answer("Текущий осмотр отменён.")


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
            f"Номер: {draft.plate_normalized or 'номер не введён'}\n"
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
    await callback.message.answer("Черновик отменён.")


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
