from datetime import datetime

import pytest

from app.db import init_db, make_engine, make_sessionmaker, session_scope
from app.models import DamageControlCase, InspectionSession
from app.repository import InspectionRepository


@pytest.mark.asyncio
async def test_problem_rows_include_damage_and_low_tire_score():
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    maker = make_sessionmaker(engine)
    await init_db(engine)

    async with session_scope(maker) as session:
        session.add_all(
            [
                InspectionSession(
                    telegram_user_id=1,
                    status="COMPLETED",
                    plate_normalized="А111АА797",
                    completed_at=datetime(2026, 6, 2, 10),
                    has_damage=True,
                ),
                InspectionSession(
                    telegram_user_id=1,
                    status="COMPLETED",
                    plate_normalized="В222ВВ797",
                    completed_at=datetime(2026, 6, 2, 11),
                    has_damage=False,
                    tire_score=3,
                ),
                InspectionSession(
                    telegram_user_id=1,
                    status="COMPLETED",
                    plate_normalized="С333СС797",
                    completed_at=datetime(2026, 6, 2, 12),
                    has_damage=False,
                    body_score=5,
                    tech_score=5,
                    wrap_score=5,
                    tire_score=5,
                ),
            ]
        )

    async with session_scope(maker) as session:
        repo = InspectionRepository(session)
        rows = await repo.problem_rows(datetime(2026, 6, 2), datetime(2026, 6, 3))
        assert {row.plate_normalized for row in rows} == {"А111АА797", "В222ВВ797"}


@pytest.mark.asyncio
async def test_charge_rows_use_close_date_and_keep_legacy_closed_case():
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    maker = make_sessionmaker(engine)
    await init_db(engine)

    async with session_scope(maker) as session:
        closed_inspection = InspectionSession(
            telegram_user_id=1,
            status="COMPLETED",
            completed_at=datetime(2026, 5, 31, 23, 0),
        )
        open_inspection = InspectionSession(
            telegram_user_id=1,
            status="COMPLETED",
            completed_at=datetime(2026, 6, 2, 10, 0),
        )
        session.add_all([closed_inspection, open_inspection])
        await session.flush()
        session.add_all(
            [
                DamageControlCase(
                    inspection_id=closed_inspection.id,
                    status="CLOSED_PAID_CASH",
                    category="DAMAGE_CHARGE_REQUIRED",
                    fp_chat_id=-1001,
                    fp_message_id=1,
                    closed_at=datetime(2026, 6, 2, 11, 0),
                    payment_amount=None,
                ),
                DamageControlCase(
                    inspection_id=open_inspection.id,
                    status="WAITING_MANAGER_ACTION",
                    category="DAMAGE_CHARGE_REQUIRED",
                    fp_chat_id=-1001,
                    fp_message_id=2,
                    closed_at=None,
                    payment_amount=5000,
                ),
            ]
        )

    async with session_scope(maker) as session:
        rows = await InspectionRepository(session).damage_control_rows(
            datetime(2026, 6, 1), datetime(2026, 7, 1)
        )
        assert len(rows) == 1
        assert rows[0].closed_at == datetime(2026, 6, 2, 11, 0)
        assert rows[0].payment_amount is None
