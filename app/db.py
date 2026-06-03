from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.models import Base


def make_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(database_url, future=True)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_db(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if engine.url.get_backend_name().startswith("sqlite"):
            rows = await conn.execute(text("PRAGMA table_info(inspection_sessions)"))
            existing_columns = {row[1] for row in rows}
            columns = {
                "tire_type": "VARCHAR(32)",
                "tire_score": "INTEGER",
                "tire_comment": "TEXT",
                "driver_has_remarks": "BOOLEAN",
                "driver_remarks_comment": "TEXT",
            }
            for name, sql_type in columns.items():
                if name not in existing_columns:
                    await conn.execute(
                        text(f"ALTER TABLE inspection_sessions ADD COLUMN {name} {sql_type}")
                    )
            rows = await conn.execute(text("PRAGMA table_info(tire_check_campaigns)"))
            campaign_columns = {row[1] for row in rows}
            if campaign_columns and "expires_at" not in campaign_columns:
                await conn.execute(
                    text("ALTER TABLE tire_check_campaigns ADD COLUMN expires_at DATETIME")
                )
            rows = await conn.execute(text("PRAGMA table_info(tire_check_campaign_plates)"))
            plate_columns = {row[1] for row in rows}
            if plate_columns and "completed_at" not in plate_columns:
                await conn.execute(
                    text("ALTER TABLE tire_check_campaign_plates ADD COLUMN completed_at DATETIME")
                )
            if plate_columns and "inspection_id" not in plate_columns:
                await conn.execute(
                    text("ALTER TABLE tire_check_campaign_plates ADD COLUMN inspection_id INTEGER")
                )
            rows = await conn.execute(text("PRAGMA table_info(damage_control_cases)"))
            damage_control_columns = {row[1] for row in rows}
            if damage_control_columns and "service_request_chat_id" not in damage_control_columns:
                await conn.execute(
                    text("ALTER TABLE damage_control_cases ADD COLUMN service_request_chat_id INTEGER")
                )
            if damage_control_columns and "driver_name" not in damage_control_columns:
                await conn.execute(
                    text("ALTER TABLE damage_control_cases ADD COLUMN driver_name VARCHAR(255)")
                )
            if damage_control_columns and "payment_type" not in damage_control_columns:
                await conn.execute(
                    text("ALTER TABLE damage_control_cases ADD COLUMN payment_type VARCHAR(64)")
                )
            if damage_control_columns and "payment_amount" not in damage_control_columns:
                await conn.execute(
                    text("ALTER TABLE damage_control_cases ADD COLUMN payment_amount INTEGER")
                )
            if damage_control_columns and "service_response_text" not in damage_control_columns:
                await conn.execute(
                    text("ALTER TABLE damage_control_cases ADD COLUMN service_response_text TEXT")
                )
            if damage_control_columns and "service_amount" not in damage_control_columns:
                await conn.execute(
                    text("ALTER TABLE damage_control_cases ADD COLUMN service_amount INTEGER")
                )


@asynccontextmanager
async def session_scope(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with sessionmaker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
