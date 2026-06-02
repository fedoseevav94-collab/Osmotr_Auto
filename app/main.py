from __future__ import annotations

import asyncio
import contextlib
import logging

from aiogram import Bot, Dispatcher

from app.config import Settings
from app.damage_control import damage_control_loop, register_damage_control, setup_damage_control
from app.db import init_db, make_engine, make_sessionmaker
from app.handlers import register_handlers, setup_router
from app.db import session_scope
from app.plate_audit import run_plate_audit_scheduler
from app.vehicle_registry import import_vehicle_registry


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = Settings.from_env()
    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN is empty")

    engine = make_engine(settings.database_url)
    sessionmaker = make_sessionmaker(engine)
    await init_db(engine)
    if settings.vehicle_plates_xlsx and settings.vehicle_plates_xlsx.exists():
        async with session_scope(sessionmaker) as session:
            count = await import_vehicle_registry(session, settings.vehicle_plates_xlsx)
            logging.info("Imported %s vehicle plates from %s", count, settings.vehicle_plates_xlsx)

    bot = Bot(settings.bot_token)
    dp = Dispatcher()
    setup_router(settings, sessionmaker)
    setup_damage_control(settings, sessionmaker)
    register_handlers(dp)
    register_damage_control(dp)
    audit_task = asyncio.create_task(run_plate_audit_scheduler(bot, sessionmaker, settings))
    damage_control_task = asyncio.create_task(damage_control_loop(bot, sessionmaker, settings))
    try:
        await dp.start_polling(bot)
    finally:
        audit_task.cancel()
        damage_control_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await audit_task
        with contextlib.suppress(asyncio.CancelledError):
            await damage_control_task


if __name__ == "__main__":
    asyncio.run(main())
